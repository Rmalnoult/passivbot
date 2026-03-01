#!/usr/bin/env python3
"""
Signal Sidecar: signal-aggregator SQLite → passivbot approved_coins.json.

Polls signal-aggregator's score_history every 5 minutes.
Score ≥70 → add coin to approved_coins long list.
Score ≤30 → remove coin (passivbot auto-graceful-stops it).
NEUTRAL → no change (preserves current state).
Writes atomically via tmp + os.replace.
Passivbot hot-reloads the file every ~30s.
"""
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SIGNAL_DB = os.getenv(
    "SIGNAL_DB",
    str(Path.home() / "crypto-bots/signal-aggregator/data/signal_aggregator.db"),
)
APPROVED_COINS_FILE = os.getenv(
    "APPROVED_COINS_FILE",
    str(Path.home() / "crypto-bots/passivbot/configs/approved_coins.json"),
)
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))  # 5 minutes
BUY_THRESHOLD = float(os.getenv("BUY_THRESHOLD", "70"))
SELL_THRESHOLD = float(os.getenv("SELL_THRESHOLD", "30"))

# signal-aggregator uses USDC pairs; passivbot uses coin symbols only
PAIR_TO_COIN = {
    "BTCUSDC": "BTC",
    "ETHUSDC": "ETH",
    "SOLUSDC": "SOL",
    "BNBUSDC": "BNB",
    "XRPUSDC": "XRP",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("signal-sidecar")


def get_latest_scores(db_path: str) -> dict[str, float]:
    """Return {pair: score} for the most recent cycle."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    try:
        rows = conn.execute(
            """
            SELECT pair, total_score
            FROM score_history
            WHERE timestamp = (SELECT MAX(timestamp) FROM score_history)
            """
        ).fetchall()
        return {row[0]: row[1] for row in rows}
    finally:
        conn.close()


def read_approved_coins(path: str) -> dict:
    """Read current approved_coins.json. Returns {"long": [...], "short": [...]}."""
    try:
        with open(path) as f:
            data = json.load(f)
        # Normalize: if it's a plain list, convert to dict format
        if isinstance(data, list):
            return {"long": data, "short": []}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"long": [], "short": []}


def write_approved_coins(path: str, data: dict) -> None:
    """Atomically write approved_coins.json."""
    dir_name = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main():
    log.info(
        "Starting signal sidecar: DB=%s  output=%s  interval=%ds",
        SIGNAL_DB,
        APPROVED_COINS_FILE,
        POLL_INTERVAL,
    )
    log.info("Thresholds: ADD≥%.0f  REMOVE≤%.0f", BUY_THRESHOLD, SELL_THRESHOLD)

    while True:
        try:
            scores = get_latest_scores(SIGNAL_DB)
            if not scores:
                log.warning("No scores found in %s", SIGNAL_DB)
                time.sleep(POLL_INTERVAL)
                continue

            current = read_approved_coins(APPROVED_COINS_FILE)
            long_set = set(current.get("long", []))
            changed = False

            for pair, score in scores.items():
                coin = PAIR_TO_COIN.get(pair)
                if not coin:
                    continue

                if score >= BUY_THRESHOLD and coin not in long_set:
                    long_set.add(coin)
                    changed = True
                    log.info("ADD %s (score=%.1f ≥ %.0f)", coin, score, BUY_THRESHOLD)
                elif score <= SELL_THRESHOLD and coin in long_set:
                    long_set.discard(coin)
                    changed = True
                    log.info(
                        "REMOVE %s (score=%.1f ≤ %.0f)", coin, score, SELL_THRESHOLD
                    )

            if changed:
                new_data = {"long": sorted(long_set), "short": []}
                write_approved_coins(APPROVED_COINS_FILE, new_data)
                log.info("Updated approved_coins: %s", new_data["long"])
            else:
                log.debug("No changes to approved_coins")

        except Exception:
            log.exception("Error in poll cycle")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
