from __future__ import annotations

import asyncio
from collections import deque

from sentinel.config import DetectorConfig, ThresholdConfig
from sentinel.ingestion.models import MarketMetadata, PriceTick
from sentinel.processing.detector import SecondBar, ShiftDetector
from sentinel.storage.sqlite_store import SQLiteStore
from sentinel.utils.metrics import Metrics


class DummyDispatcher:
    def __init__(self) -> None:
        self.alerts = []

    async def dispatch(self, alert) -> None:
        self.alerts.append(alert)

    async def dispatch_many(self, alerts) -> list:
        self.alerts.extend(alerts)
        return list(alerts)


class FakeClock:
    def __init__(self, now_ms: int = 0) -> None:
        self._now_ms = now_ms

    def now_ms(self) -> int:
        return self._now_ms

    def set(self, now_ms: int) -> None:
        self._now_ms = now_ms


def _detector_config() -> DetectorConfig:
    return DetectorConfig(
        check_interval_sec=1,
        buffer_max_age_sec=360,
        bar_interval_sec=1,
        cooldown_sec=300,
        stale_threshold_sec=120,
        max_spread_for_detection=0.15,
        max_quote_age_sec=15,
        warmup_grace_sec=60,
        thresholds={
            "scout": ThresholdConfig(window_sec=60, delta_pct=5.0, min_ticks=2),
            "confirmation": ThresholdConfig(window_sec=180, delta_pct=50.0, min_ticks=5),
            "trend": ThresholdConfig(window_sec=300, delta_pct=50.0, min_ticks=10),
        },
    )


def test_compute_delta_uses_price_open() -> None:
    async def runner() -> None:
        store = SQLiteStore(":memory:")
        await store.initialize()
        detector = ShiftDetector(_detector_config(), store, DummyDispatcher(), Metrics(), FakeClock().now_ms)
        bars = deque(
            [
                SecondBar(second_ts_ms=0, price_open=0.50, price_last=0.53, tick_count=3, spread_last=None, quote_ts_ms=None),
                SecondBar(second_ts_ms=60_000, price_open=0.58, price_last=0.58, tick_count=1, spread_last=None, quote_ts_ms=None),
            ]
        )

        result = detector._compute_delta(bars, _detector_config().thresholds["scout"])

        assert result is not None
        delta_pct, _, start_price, _, _ = result
        assert start_price == 0.50
        assert round(delta_pct, 2) == 16.00

    asyncio.run(runner())


def test_warmup_allows_weak_last_trade_alert_and_respects_cooldown() -> None:
    async def runner() -> None:
        store = SQLiteStore(":memory:")
        await store.initialize()
        clock = FakeClock(0)
        dispatcher = DummyDispatcher()
        detector = ShiftDetector(_detector_config(), store, dispatcher, Metrics(), clock.now_ms)
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
        await store.set_market_active(["asset-1"], active=True, now_ms=0)

        await detector.ingest_tick(
            PriceTick(
                asset_id="asset-1",
                price=0.50,
                best_bid=None,
                best_ask=None,
                spread=None,
                signal_source="last_trade",
                quote_ts_ms=None,
                timestamp_ms=0,
            )
        )
        clock.set(60_000)
        await detector.ingest_tick(
            PriceTick(
                asset_id="asset-1",
                price=0.55,
                best_bid=None,
                best_ask=None,
                spread=None,
                signal_source="last_trade",
                quote_ts_ms=None,
                timestamp_ms=60_000,
            )
        )

        alerts = await detector.scan_once()

        assert len(alerts) == 1
        assert alerts[0].signal_quality.confidence == "weak"
        assert len(dispatcher.alerts) == 1

        clock.set(61_000)
        alerts = await detector.scan_once()

        assert alerts == []

    asyncio.run(runner())
