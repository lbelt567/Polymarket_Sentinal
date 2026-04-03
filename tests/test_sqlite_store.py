from __future__ import annotations

import asyncio

from sentinel.ingestion.models import MarketMetadata, PriceTick
from sentinel.processing.alerts import ShiftAlert, ShiftSummary, SignalQuality
from sentinel.storage.sqlite_store import SQLiteStore


def test_sqlite_store_round_trip() -> None:
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
            sibling_asset_ids=["asset-2"],
            sibling_market_slugs=["market-2"],
            outcome="Yes",
            end_date="2026-06-15T18:00:00Z",
            volume_24h=25_000,
            liquidity=15_000,
            polymarket_url="https://polymarket.com/event/event-slug",
        )
        await store.upsert_tracked_markets([market], now_ms=1000)
        await store.set_market_active(["asset-1"], active=True, now_ms=2000)

        tick = PriceTick(
            asset_id="asset-1",
            price=0.55,
            best_bid=0.54,
            best_ask=0.56,
            spread=0.02,
            signal_source="midpoint_book",
            quote_ts_ms=2000,
            timestamp_ms=2000,
        )
        await store.insert_price_tick(tick, market_slug="market-slug")
        await store.update_last_tick("asset-1", 2000)

        alert = ShiftAlert(
            alert_id="abc123",
            alert_level="trend",
            alert_level_label="Decisive Shift",
            timestamp_iso="2026-04-01T14:32:00+00:00",
            timestamp_ms=3000,
            market={
                "asset_id": "asset-1",
                "market_slug": "market-slug",
                "question": "Question?",
                "category": "Economics",
            },
            shift=ShiftSummary(
                direction="up",
                delta_pct=10.0,
                signed_delta_pct=10.0,
                price_start=0.50,
                price_end=0.55,
                price_current=0.55,
                window_sec=300,
                ticks_in_window=12,
            ),
            signal_quality=SignalQuality(
                signal_source="midpoint_book",
                best_bid=0.54,
                best_ask=0.56,
                spread=0.02,
                quote_age_sec=1.0,
                liquidity_bucket="high",
                confidence="strong",
            ),
        )
        await store.insert_shift_event(alert)

        cached = store.get_tracked_market("asset-1")
        rows = await store.fetch_price_ticks_between(0, 10_000)

        assert cached is not None
        assert cached.sibling_market_slugs == ["market-2"]
        assert cached.last_tick_ms == 2000
        assert len(rows) == 1
        assert rows[0]["signal_source"] == "midpoint_book"

    asyncio.run(runner())
