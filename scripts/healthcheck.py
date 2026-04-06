from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Polymarket Sentinel runtime health.")
    parser.add_argument("--sqlite-path", default="data/sentinel.db")
    parser.add_argument("--max-tick-age-sec", type=int, default=300)
    parser.add_argument("--max-gatekeeper-age-sec", type=int, default=900)
    parser.add_argument("--min-active-markets", type=int, default=25)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.sqlite_path)
    if not db_path.exists():
        print(json.dumps({"status": "error", "reason": "missing_sqlite", "path": str(db_path)}))
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        now_ms = int(time.time() * 1000)
        latest_tick_ms = conn.execute("SELECT MAX(ts_ms) FROM price_ticks").fetchone()[0]
        latest_gatekeeper_ms = conn.execute("SELECT MAX(last_update) FROM tracked_markets").fetchone()[0]
        active_markets = conn.execute("SELECT COUNT(*) FROM tracked_markets WHERE active=1").fetchone()[0]
    finally:
        conn.close()

    failures: list[str] = []
    if active_markets < args.min_active_markets:
        failures.append("too_few_active_markets")
    if latest_tick_ms is None:
        failures.append("no_price_ticks")
    elif now_ms - int(latest_tick_ms) > args.max_tick_age_sec * 1000:
        failures.append("stale_price_ticks")
    if latest_gatekeeper_ms is None:
        failures.append("no_gatekeeper_update")
    elif now_ms - int(latest_gatekeeper_ms) > args.max_gatekeeper_age_sec * 1000:
        failures.append("stale_gatekeeper_update")

    payload = {
        "status": "ok" if not failures else "error",
        "active_markets": active_markets,
        "latest_tick_age_sec": None if latest_tick_ms is None else round((now_ms - int(latest_tick_ms)) / 1000, 1),
        "latest_gatekeeper_age_sec": None
        if latest_gatekeeper_ms is None
        else round((now_ms - int(latest_gatekeeper_ms)) / 1000, 1),
        "failures": failures,
    }
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
