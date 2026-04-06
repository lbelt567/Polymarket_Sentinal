from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
from pathlib import Path

from sentinel.config import load_config
from sentinel.ingestion.models import MarketMetadata
from sentinel.notifications.base import AgentAlertPolicy, NotificationDispatcher
from sentinel.processing.alerts import AlertLevel
from sentinel.processing.detector import ShiftDetector
from sentinel.processing.stream import ReplayStream, VirtualClock, default_parquet_glob
from sentinel.storage.sqlite_store import SQLiteStore
from sentinel.utils.logging import configure_logging
from sentinel.utils.metrics import Metrics


class PrintNotifier:
    async def send(self, alert) -> None:
        print(alert.to_json())


def default_metadata_glob(archive_dir: str | Path) -> str:
    return str(Path(archive_dir) / "**" / "*.markets.json")


async def _seed_market_metadata(store: SQLiteStore, metadata_glob: str) -> int:
    latest_by_asset: dict[str, MarketMetadata] = {}
    for metadata_path in sorted(glob.glob(metadata_glob, recursive=True)):
        payload = json.loads(Path(metadata_path).read_text())
        for item in payload:
            market = MarketMetadata(**item)
            latest_by_asset[market.asset_id] = market
    if latest_by_asset:
        await store.upsert_tracked_markets(list(latest_by_asset.values()), now_ms=0)
    return len(latest_by_asset)


async def _run_backtest(parquet_glob: str, metadata_glob: str, speed: float) -> None:
    config = load_config()
    configure_logging(config.logging)
    store = SQLiteStore(":memory:")
    await store.initialize()
    metrics = Metrics()
    clock = VirtualClock()
    detector = ShiftDetector(
        config.detector,
        store,
        NotificationDispatcher(
            notifiers=[PrintNotifier()],
            min_severity=AlertLevel.SCOUT,
            policy=AgentAlertPolicy(
                allowed_levels={AlertLevel.SCOUT, AlertLevel.CONFIRMATION, AlertLevel.TREND},
                excluded_confidences=set(),
                excluded_signal_sources=set(),
                min_price=0.0,
                max_price=1.0,
                min_liquidity=0.0,
                min_abs_move=0.0,
                event_dedup_sec=0,
            ),
        ),
        metrics,
        clock.now_ms,
    )
    metadata_count = await _seed_market_metadata(store, metadata_glob)
    stream = ReplayStream(parquet_glob, clock=clock, speed=speed)
    logger = logging.getLogger(__name__)
    seen_assets: set[str] = set()
    if metadata_count == 0:
        logger.warning("backtest_metadata_missing", extra={"extra_data": {"metadata_glob": metadata_glob}})
    async for tick in stream.ticks():
        if tick.asset_id not in seen_assets and store.get_tracked_market(tick.asset_id) is not None:
            await store.set_market_active([tick.asset_id], active=True, now_ms=tick.timestamp_ms)
            seen_assets.add(tick.asset_id)
        await detector.ingest_tick(tick)
        alerts = await detector.scan_once()
        if alerts:
            logger.info("backtest_alerts", extra={"extra_data": {"count": len(alerts)}})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parquet-glob", default=default_parquet_glob("data/archive"))
    parser.add_argument("--metadata-glob", default=default_metadata_glob("data/archive"))
    parser.add_argument("--speed", type=float, default=0.0)
    args = parser.parse_args()
    asyncio.run(_run_backtest(args.parquet_glob, args.metadata_glob, args.speed))


if __name__ == "__main__":
    main()
