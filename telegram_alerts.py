#!/usr/bin/env python3
"""Passivbot Telegram Alerts Sidecar.

Monitors passivbot journald logs and sends Telegram notifications for:
- Fills (entries, closes with PnL)
- Health summaries (every 30 min)
- Balance changes
- Errors and restarts
- Approved coins changes (from sidecar)
"""

import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import json
import logging
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("telegram_alerts")

# --- Config ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# How often to forward health summaries (seconds). Default: every 30 min.
HEALTH_INTERVAL = int(os.environ.get("HEALTH_INTERVAL", "1800"))

if not BOT_TOKEN or not CHAT_ID:
    log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    sys.exit(1)


def send_telegram(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        log.warning("Telegram send failed: %s", e)
        return False


# --- Log line matchers ---

# [fill] BTC long entry +0.001 @ 100000.00 id=abc123
# [fill] BTC long close -0.001 @ 100500.00, pnl=+5.50 USDT id=abc123
FILL_RE = re.compile(
    r"\[fill\]\s+(?:[\dT:-]+\s+)?(\S+)\s+(\S+)\s+(\S+)\s+([+-][\d.]+)\s+@\s+([\d.]+)"
    r"(?:,\s*pnl=([+-]?[\d.]+)\s+USDT)?"
)

# [fill] 5 fills, pnl=+12.3 USDT  (bulk summary)
FILL_BULK_RE = re.compile(r"\[fill\]\s+(\d+)\s+fills,\s*pnl=([+-]?[\d.]+)\s+USDT")

# [health] uptime=2h30m | loop=3.5s | positions=1 long, 0 short | balance=200.00 USDC | ...
HEALTH_RE = re.compile(r"\[health\]\s+(.*)")

# [balance] 0.0 -> 200.0 equity: 200.0000 source: REST
BALANCE_RE = re.compile(r"\[balance\]\s+([\d.]+)\s+->\s+([\d.]+)\s+equity:\s+([\d.]+)")

# [balance] too low
BALANCE_LOW_RE = re.compile(r"\[balance\]\s+too low")

# error count: N of M
ERROR_COUNT_RE = re.compile(r"error count:\s+(\d+)\s+of\s+(\d+)")

# Bot will restart
RESTART_RE = re.compile(r"Bot will restart|RestartBotException")

# Sidecar: Updated approved_coins
APPROVED_RE = re.compile(r"Updated approved_coins:\s*(.*)")


def format_fill(m: re.Match) -> str:
    coin, pside, order_type, qty, price = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    pnl = m.group(6)

    is_entry = "entry" in order_type
    is_close = "close" in order_type

    if is_entry:
        icon = "\U0001F7E2"  # green circle
        action = "ENTRY"
    elif is_close:
        icon = "\U0001F534" if pnl and float(pnl) < 0 else "\U0001F7E2"  # red/green
        action = "CLOSE"
    else:
        icon = "\u26AA"  # white circle
        action = order_type.upper()

    msg = f"{icon} <b>{action}</b> {coin} {pside}\n"
    msg += f"   Qty: {qty} @ ${price}"
    if pnl:
        pnl_f = float(pnl)
        pnl_icon = "\u2705" if pnl_f >= 0 else "\u274C"
        msg += f"\n   PnL: {pnl_icon} <b>{pnl} USDT</b>"
    return msg


def format_fill_bulk(m: re.Match) -> str:
    count, pnl = m.group(1), m.group(2)
    pnl_f = float(pnl)
    icon = "\u2705" if pnl_f >= 0 else "\u274C"
    return f"{icon} <b>{count} fills</b> | PnL: <b>{pnl} USDT</b>"


def format_health(m: re.Match) -> str:
    raw = m.group(1)
    parts = [p.strip() for p in raw.split("|")]
    lines = ["<b>PASSIVBOT STATUS</b>"]
    for part in parts:
        lines.append(f"  {part}")
    return "\n".join(lines)


def format_balance_change(m: re.Match) -> str:
    old, new, equity = m.group(1), m.group(2), m.group(3)
    return f"\U0001F4B0 Balance: {old} -> <b>{new} USDC</b> (equity: {equity})"


def format_approved(m: re.Match) -> str:
    coins = m.group(1)
    if coins.strip() in ("[]", ""):
        return "\U0001F6D1 <b>Approved coins: NONE</b> (bot will idle)"
    return f"\U0001F4CB <b>Approved coins:</b> {coins}"


def main():
    log.info("Starting Telegram alerts sidecar")
    send_telegram("\U0001F680 <b>Passivbot alerts started</b>")

    last_health_sent = 0

    # Tail both passivbot and sidecar journals
    proc = subprocess.Popen(
        [
            "journalctl",
            "-u", "passivbot.service",
            "-u", "passivbot-sidecar.service",
            "-f", "--no-pager",
            "-o", "cat",  # plain message, no metadata
            "-n", "0",    # don't replay old logs
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            # --- Fills ---
            m = FILL_RE.search(line)
            if m:
                send_telegram(format_fill(m))
                continue

            m = FILL_BULK_RE.search(line)
            if m:
                send_telegram(format_fill_bulk(m))
                continue

            # --- Health (throttled) ---
            m = HEALTH_RE.search(line)
            if m:
                now = time.time()
                if now - last_health_sent >= HEALTH_INTERVAL:
                    send_telegram(format_health(m))
                    last_health_sent = now
                continue

            # --- Balance changes ---
            if BALANCE_LOW_RE.search(line):
                send_telegram("\u26A0\uFE0F <b>Balance too low!</b> Not creating orders.")
                continue

            m = BALANCE_RE.search(line)
            if m:
                old_val, new_val = float(m.group(1)), float(m.group(2))
                # Only alert on meaningful changes (not the initial 0->X on startup)
                if old_val > 0 and abs(new_val - old_val) > 0.01:
                    send_telegram(format_balance_change(m))
                continue

            # --- Errors ---
            if RESTART_RE.search(line):
                send_telegram("\u26A0\uFE0F <b>Passivbot restarting</b> due to errors")
                continue

            m = ERROR_COUNT_RE.search(line)
            if m:
                current, maximum = int(m.group(1)), int(m.group(2))
                if current >= maximum - 1:
                    send_telegram(
                        f"\u26A0\uFE0F <b>Error budget critical:</b> {current}/{maximum}"
                    )
                continue

            # --- Approved coins ---
            m = APPROVED_RE.search(line)
            if m:
                send_telegram(format_approved(m))
                continue

    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        proc.terminate()
        send_telegram("\U0001F6D1 <b>Passivbot alerts stopped</b>")


if __name__ == "__main__":
    main()
