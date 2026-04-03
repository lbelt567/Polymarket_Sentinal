from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable

from sentinel.config import DetectorConfig, ThresholdConfig
from sentinel.ingestion.models import MarketMetadata, PriceTick
from sentinel.notifications.base import NotificationDispatcher
from sentinel.processing.alerts import AlertLevel, LEVEL_LABELS, LEVEL_ORDER, ShiftAlert, ShiftSummary, SignalQuality, new_alert_timestamp
from sentinel.storage.sqlite_store import SQLiteStore
from sentinel.utils.metrics import Metrics


@dataclass(slots=True)
class SecondBar:
    second_ts_ms: int
    price_open: float
    price_last: float
    tick_count: int
    spread_last: float | None
    quote_ts_ms: int | None


class ShiftDetector:
    def __init__(
        self,
        config: DetectorConfig,
        store: SQLiteStore,
        dispatcher: NotificationDispatcher,
        metrics: Metrics,
        time_fn: Callable[[], int],
    ) -> None:
        self._config = config
        self._store = store
        self._dispatcher = dispatcher
        self._metrics = metrics
        self._time_fn = time_fn
        self._logger = logging.getLogger(__name__)
        self._bars_by_asset: dict[str, deque[SecondBar]] = {}
        self._latest_tick_by_asset: dict[str, PriceTick] = {}
        self._cooldowns: dict[tuple[str, AlertLevel], int] = {}

    async def ingest_loop(self, stream) -> None:
        async for tick in stream.ticks():
            await self.ingest_tick(tick)

    async def scan_loop(self) -> None:
        while True:
            await self.scan_once()
            await asyncio.sleep(self._config.check_interval_sec)

    async def ingest_tick(self, tick: PriceTick) -> None:
        bars = self._bars_by_asset.setdefault(tick.asset_id, deque())
        self._upsert_bar(bars, tick)
        self._prune_bars(bars)
        self._latest_tick_by_asset[tick.asset_id] = tick
        self._metrics.record_tick(tick.timestamp_ms)
        self._metrics.add_signal_source(tick.signal_source)
        market = self._store.get_tracked_market(tick.asset_id)
        if market is not None:
            await self._store.insert_price_tick(tick, market.market_slug)
            await self._store.update_last_tick(tick.asset_id, tick.timestamp_ms)

    async def scan_once(self) -> list[ShiftAlert]:
        alerts: list[ShiftAlert] = []
        for asset_id, bars in list(self._bars_by_asset.items()):
            if not bars:
                continue
            self._prune_bars(bars)
            if not bars:
                self._bars_by_asset.pop(asset_id, None)
                self._latest_tick_by_asset.pop(asset_id, None)
                continue
            if self._should_skip(asset_id, bars):
                self._metrics.increment("alerts_skipped_quality")
                continue
            for level in (AlertLevel.SCOUT, AlertLevel.CONFIRMATION, AlertLevel.TREND):
                alert = await self._maybe_create_alert(asset_id, bars, level)
                if alert is None:
                    continue
                alerts.append(alert)
        await self._dispatcher.dispatch_many(alerts)
        return alerts

    def remove_asset(self, asset_id: str) -> None:
        self._bars_by_asset.pop(asset_id, None)
        self._latest_tick_by_asset.pop(asset_id, None)
        self._cooldowns = {key: value for key, value in self._cooldowns.items() if key[0] != asset_id}

    def _upsert_bar(self, bars: deque[SecondBar], tick: PriceTick) -> None:
        bucket_ms = (tick.timestamp_ms // 1000) * 1000
        if bars and bars[-1].second_ts_ms == bucket_ms:
            bar = bars[-1]
            bar.price_last = tick.price
            bar.tick_count += 1
            if tick.spread is not None:
                bar.spread_last = tick.spread
                bar.quote_ts_ms = tick.quote_ts_ms
        else:
            bars.append(
                SecondBar(
                    second_ts_ms=bucket_ms,
                    price_open=tick.price,
                    price_last=tick.price,
                    tick_count=1,
                    spread_last=tick.spread,
                    quote_ts_ms=tick.quote_ts_ms,
                )
            )

    def _prune_bars(self, bars: deque[SecondBar]) -> None:
        cutoff_ms = self._time_fn() - (self._config.buffer_max_age_sec * 1000)
        while bars and bars[0].second_ts_ms < cutoff_ms:
            bars.popleft()

    def _should_skip(self, asset_id: str, bars: deque[SecondBar]) -> bool:
        now_ms = self._time_fn()
        if now_ms - bars[-1].second_ts_ms > self._config.stale_threshold_sec * 1000:
            self._metrics.increment("ticks_dropped_stale")
            return True
        latest_with_quote = next((bar for bar in reversed(bars) if bar.spread_last is not None), None)
        market = self._store.get_tracked_market(asset_id)
        if latest_with_quote is None:
            if market and now_ms - market.subscribed_at_ms <= self._config.warmup_grace_sec * 1000:
                return False
            self._metrics.increment("ticks_dropped_quote_stale")
            return True
        quote_age_ms = now_ms - (latest_with_quote.quote_ts_ms or 0)
        if quote_age_ms > self._config.max_quote_age_sec * 1000:
            self._metrics.increment("ticks_dropped_quote_stale")
            return True
        if (latest_with_quote.spread_last or 0.0) > self._config.max_spread_for_detection:
            return True
        if market is not None and not market.active:
            return True
        return False

    async def _maybe_create_alert(self, asset_id: str, bars: deque[SecondBar], level: AlertLevel) -> ShiftAlert | None:
        threshold = self._config.thresholds[level.value]
        delta = self._compute_delta(bars, threshold)
        if delta is None:
            return None
        delta_pct, signed_delta_pct, start_price, end_price, tick_count = delta
        if delta_pct < threshold.delta_pct:
            return None
        now_ms = self._time_fn()
        cooldown_key = (asset_id, level)
        cooldown_until = self._cooldowns.get(cooldown_key, 0)
        if cooldown_until > now_ms:
            self._metrics.increment("alerts_skipped_cooldown")
            return None

        market = self._store.get_tracked_market(asset_id)
        latest_tick = self._latest_tick_by_asset.get(asset_id)
        if market is None or latest_tick is None:
            return None

        quote_age_sec = None
        if latest_tick.quote_ts_ms is not None:
            quote_age_sec = max((now_ms - latest_tick.quote_ts_ms) / 1000, 0.0)
        is_warmup = latest_tick.signal_source == "last_trade" and now_ms - market.subscribed_at_ms <= self._config.warmup_grace_sec * 1000
        signal_quality = SignalQuality(
            signal_source=latest_tick.signal_source,
            best_bid=latest_tick.best_bid,
            best_ask=latest_tick.best_ask,
            spread=latest_tick.spread,
            quote_age_sec=quote_age_sec,
            liquidity_bucket=self._liquidity_bucket(market.liquidity),
            confidence=self._build_confidence(latest_tick, market, tick_count, quote_age_sec, is_warmup),
        )
        alert = ShiftAlert(
            alert_id=self._make_alert_id(asset_id, level, now_ms),
            alert_level=level.value,
            alert_level_label=LEVEL_LABELS[level],
            timestamp_iso=new_alert_timestamp(now_ms),
            timestamp_ms=now_ms,
            market=self._market_payload(market),
            shift=ShiftSummary(
                direction="up" if signed_delta_pct >= 0 else "down",
                delta_pct=round(delta_pct, 4),
                signed_delta_pct=round(signed_delta_pct, 4),
                price_start=round(start_price, 6),
                price_end=round(end_price, 6),
                price_current=round(latest_tick.price, 6),
                window_sec=threshold.window_sec,
                ticks_in_window=tick_count,
            ),
            signal_quality=signal_quality,
        )
        self._cooldowns[cooldown_key] = now_ms + (self._config.cooldown_sec * 1000)
        await self._store.insert_shift_event(alert)
        self._metrics.increment(f"alerts_{level.value}")
        self._logger.info("shift_alert", extra={"extra_data": alert.to_dict()})
        return alert

    def _compute_delta(
        self,
        bars: deque[SecondBar],
        threshold: ThresholdConfig,
    ) -> tuple[float, float, float, float, int] | None:
        if len(bars) < 2:
            return None
        now_ms = bars[-1].second_ts_ms
        cutoff_ms = now_ms - (threshold.window_sec * 1000)
        start_price = None
        tick_count = 0
        for bar in bars:
            if bar.second_ts_ms >= cutoff_ms:
                if start_price is None:
                    start_price = bar.price_open
                tick_count += bar.tick_count
        if start_price in (None, 0):
            return None
        if tick_count < threshold.min_ticks:
            return None
        end_price = bars[-1].price_last
        signed_delta = (end_price - start_price) / start_price * 100.0
        return abs(signed_delta), signed_delta, start_price, end_price, tick_count

    def _market_payload(self, market: MarketMetadata) -> dict[str, object]:
        return {
            "asset_id": market.asset_id,
            "condition_id": market.condition_id,
            "market_id": market.market_id,
            "market_slug": market.market_slug,
            "question": market.question,
            "description": market.description,
            "category": market.category,
            "tags": market.tags,
            "event_id": market.event_id,
            "event_title": market.event_title,
            "event_slug": market.event_slug,
            "sibling_asset_ids": market.sibling_asset_ids,
            "sibling_market_slugs": market.sibling_market_slugs,
            "outcome": market.outcome,
            "end_date": market.end_date,
            "volume_24h": market.volume_24h,
            "liquidity": market.liquidity,
            "polymarket_url": market.polymarket_url,
        }

    def _build_confidence(
        self,
        latest_tick: PriceTick,
        market: MarketMetadata,
        tick_count: int,
        quote_age_sec: float | None,
        is_warmup: bool,
    ) -> str:
        if is_warmup or not latest_tick.signal_source.startswith("midpoint"):
            return "weak"
        if quote_age_sec is None or latest_tick.spread is None:
            return "weak"
        bucket = self._liquidity_bucket(market.liquidity)
        if quote_age_sec <= 5 and latest_tick.spread <= 0.03 and bucket == "high" and tick_count >= 10:
            return "strong"
        if quote_age_sec <= 15 and latest_tick.spread <= 0.10 and tick_count >= 5:
            return "moderate"
        return "weak"

    @staticmethod
    def _liquidity_bucket(liquidity: float) -> str:
        if liquidity >= 100_000:
            return "high"
        if liquidity >= 25_000:
            return "medium"
        return "low"

    @staticmethod
    def _make_alert_id(asset_id: str, level: AlertLevel, timestamp_ms: int) -> str:
        return hashlib.blake2s(f"{asset_id}:{level.value}:{timestamp_ms}".encode(), digest_size=8).hexdigest()
