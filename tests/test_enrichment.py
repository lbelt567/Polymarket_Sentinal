from __future__ import annotations

import asyncio

from sentinel.ingestion.models import PriceTick
from sentinel.processing.alerts import ShiftAlert, ShiftSummary, SignalQuality
from sentinel.processing.enrichment import BeforeStateEnricher
from sentinel.storage.sqlite_store import SQLiteStore


def test_before_state_enricher_adds_pre_alert_context() -> None:
    async def runner() -> None:
        store = SQLiteStore(":memory:")
        await store.initialize()
        asset_id = "asset-1"
        prices = [
            (0, 0.5000),
            (600_000, 0.5010),
            (1_200_000, 0.4990),
            (1_800_000, 0.5000),
            (2_400_000, 0.5005),
            (3_000_000, 0.5008),
            (3_360_000, 0.5010),
            (3_480_000, 0.5015),
            (3_540_000, 0.5020),
        ]
        for ts_ms, price in prices:
            tick = PriceTick(
                asset_id=asset_id,
                price=price,
                best_bid=price - 0.001,
                best_ask=price + 0.001,
                spread=0.002,
                signal_source="midpoint_change",
                quote_ts_ms=ts_ms,
                timestamp_ms=ts_ms,
            )
            await store.insert_price_tick(tick, market_slug="market-slug")

        alert = ShiftAlert(
            alert_id="abc123",
            alert_level="confirmation",
            alert_level_label="Confirmed Shift",
            timestamp_iso="2026-04-01T14:32:00+00:00",
            timestamp_ms=3_600_000,
            market={
                "asset_id": asset_id,
                "market_slug": "market-slug",
                "question": "Question?",
                "category": "Economics",
                "event_slug": "event-slug",
                "liquidity": 100_000,
            },
            shift=ShiftSummary(
                direction="up",
                delta_pct=6.0,
                signed_delta_pct=6.0,
                price_start=0.50,
                price_end=0.53,
                price_current=0.53,
                window_sec=180,
                ticks_in_window=12,
            ),
            signal_quality=SignalQuality(
                signal_source="midpoint_change",
                best_bid=0.529,
                best_ask=0.531,
                spread=0.002,
                quote_age_sec=1.0,
                liquidity_bucket="high",
                confidence="moderate",
            ),
        )

        enricher = BeforeStateEnricher(store)
        enriched = await enricher.enrich(alert)

        assert enriched.before_state is not None
        assert enriched.before_state.lookback_30m.tick_count == 6
        assert enriched.before_state.lookback_30m.price_start == 0.5
        assert enriched.before_state.lookback_30m.price_end == 0.502
        assert enriched.before_state.activity.last_5m_tick_count == 3
        assert enriched.before_state.activity.prior_30m_avg_5m_tick_count == 0.5
        assert enriched.before_state.activity.tick_activity_ratio == 6.0
        assert enriched.before_state.regime_label == "quiet_breakout"

    asyncio.run(runner())
