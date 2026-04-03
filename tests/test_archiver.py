from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scripts.backtest import _seed_market_metadata
from sentinel.ingestion.models import MarketMetadata
from sentinel.config import StorageConfig
from sentinel.ingestion.models import PriceTick
from sentinel.processing.stream import ReplayStream, VirtualClock
from sentinel.storage.archiver import Archiver
from sentinel.storage.sqlite_store import SQLiteStore
from sentinel.utils.metrics import Metrics


def test_archiver_writes_parquet_and_replay_updates_virtual_clock(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("duckdb")

    async def runner() -> None:
        store = SQLiteStore(":memory:")
        await store.initialize()
        market = MarketMetadata(
            asset_id="asset-1",
            market_id="market-1",
            condition_id="condition-1",
            market_slug="market-slug",
            question="Question?",
            description="Description",
            category="Economics",
            tags=["fed"],
            event_id="event-1",
            event_title="Event",
            event_slug="event-slug",
            sibling_asset_ids=[],
            sibling_market_slugs=[],
            outcome="Yes",
            end_date="",
            volume_24h=25_000,
            liquidity=15_000,
            polymarket_url="https://polymarket.com/event/event-slug",
        )
        await store.upsert_tracked_markets([market], now_ms=0)
        tick = PriceTick(
            asset_id="asset-1",
            price=0.55,
            best_bid=0.54,
            best_ask=0.56,
            spread=0.02,
            signal_source="midpoint_book",
            quote_ts_ms=1_000,
            timestamp_ms=1_000,
        )
        await store.insert_price_tick(tick, market_slug="market-slug")
        clock_ms = 7_200_000
        archiver = Archiver(
            StorageConfig(
                sqlite_path=":memory:",
                hot_retention_hours=48,
                archive_dir=str(tmp_path),
                archive_interval_sec=3600,
            ),
            store,
            Metrics(),
            time_fn=lambda: clock_ms,
        )

        await archiver.archive_once()

        parquet_files = list(tmp_path.rglob("*.parquet"))
        metadata_files = list(tmp_path.rglob("*.markets.json"))
        assert len(parquet_files) == 1
        assert len(metadata_files) == 1

        seed_store = SQLiteStore(":memory:")
        await seed_store.initialize()
        count = await _seed_market_metadata(seed_store, str(tmp_path / "**" / "*.markets.json"))
        assert count == 1
        assert seed_store.get_tracked_market("asset-1") is not None

        replay_clock = VirtualClock()
        stream = ReplayStream(str(tmp_path / "**" / "*.parquet"), clock=replay_clock, speed=0.0)
        ticks = []
        async for replay_tick in stream.ticks():
            ticks.append(replay_tick)

        assert len(ticks) == 1
        assert replay_clock.now_ms() == 1_000

    asyncio.run(runner())
