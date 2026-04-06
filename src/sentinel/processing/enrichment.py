from __future__ import annotations

import logging
from dataclasses import replace

from sentinel.processing.alerts import BeforeState, BeforeStateActivity, BeforeStateWindow, ShiftAlert
from sentinel.storage.sqlite_store import SQLiteStore


class BeforeStateEnricher:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._logger = logging.getLogger(__name__)

    async def enrich(self, alert: ShiftAlert) -> ShiftAlert:
        asset_id = str(alert.market.get("asset_id") or "")
        if not asset_id:
            return alert
        end_ms = alert.timestamp_ms
        start_ms = end_ms - 3_600_000
        rows = await self._store.fetch_price_ticks_for_asset(asset_id, start_ms, end_ms)
        if not rows:
            return alert

        window_30m_start = end_ms - 1_800_000
        lookback_30m_rows = [row for row in rows if int(row["ts_ms"]) >= window_30m_start]
        lookback_30m = self._summarize_window(lookback_30m_rows, 1_800)
        lookback_60m = self._summarize_window(rows, 3_600)
        activity = self._summarize_activity(rows, end_ms)
        regime_label = self._classify_regime(alert, lookback_30m, activity)
        return replace(
            alert,
            before_state=BeforeState(
                lookback_30m=lookback_30m,
                lookback_60m=lookback_60m,
                activity=activity,
                regime_label=regime_label,
            ),
        )

    def _summarize_window(self, rows: list[dict[str, object]], window_sec: int) -> BeforeStateWindow:
        if not rows:
            return BeforeStateWindow(
                window_sec=window_sec,
                price_start=None,
                price_end=None,
                net_delta_pct=None,
                high=None,
                low=None,
                range_pct=None,
                position_in_range=None,
                tick_count=0,
                avg_spread=None,
            )

        prices = [float(row["price"]) for row in rows]
        spreads = [float(row["spread"]) for row in rows if row["spread"] is not None]
        price_start = prices[0]
        price_end = prices[-1]
        high = max(prices)
        low = min(prices)
        net_delta_pct = ((price_end - price_start) / price_start * 100.0) if price_start else None
        range_pct = ((high - low) / price_start * 100.0) if price_start else None
        if high > low:
            position_in_range = (price_end - low) / (high - low)
        else:
            position_in_range = 0.5
        avg_spread = sum(spreads) / len(spreads) if spreads else None
        return BeforeStateWindow(
            window_sec=window_sec,
            price_start=round(price_start, 6),
            price_end=round(price_end, 6),
            net_delta_pct=round(net_delta_pct, 4) if net_delta_pct is not None else None,
            high=round(high, 6),
            low=round(low, 6),
            range_pct=round(range_pct, 4) if range_pct is not None else None,
            position_in_range=round(position_in_range, 4),
            tick_count=len(rows),
            avg_spread=round(avg_spread, 6) if avg_spread is not None else None,
        )

    def _summarize_activity(self, rows: list[dict[str, object]], end_ms: int) -> BeforeStateActivity:
        last_5m_cutoff = end_ms - 300_000
        prior_30m_cutoff = end_ms - 2_100_000
        last_5m_rows = [row for row in rows if int(row["ts_ms"]) >= last_5m_cutoff]
        prior_30m_rows = [row for row in rows if prior_30m_cutoff <= int(row["ts_ms"]) < last_5m_cutoff]
        last_5m_tick_count = len(last_5m_rows)
        prior_30m_avg_5m_tick_count = (len(prior_30m_rows) / 6.0) if prior_30m_rows else None
        tick_activity_ratio = None
        if prior_30m_avg_5m_tick_count and prior_30m_avg_5m_tick_count > 0:
            tick_activity_ratio = last_5m_tick_count / prior_30m_avg_5m_tick_count
        last_5m_avg_spread = self._avg_spread(last_5m_rows)
        prior_30m_avg_spread = self._avg_spread(prior_30m_rows)
        spread_change_pct = None
        if prior_30m_avg_spread not in (None, 0.0) and last_5m_avg_spread is not None:
            spread_change_pct = ((last_5m_avg_spread - prior_30m_avg_spread) / prior_30m_avg_spread) * 100.0
        return BeforeStateActivity(
            last_5m_tick_count=last_5m_tick_count,
            prior_30m_avg_5m_tick_count=round(prior_30m_avg_5m_tick_count, 2)
            if prior_30m_avg_5m_tick_count is not None
            else None,
            tick_activity_ratio=round(tick_activity_ratio, 4) if tick_activity_ratio is not None else None,
            last_5m_avg_spread=round(last_5m_avg_spread, 6) if last_5m_avg_spread is not None else None,
            prior_30m_avg_spread=round(prior_30m_avg_spread, 6) if prior_30m_avg_spread is not None else None,
            spread_change_pct=round(spread_change_pct, 4) if spread_change_pct is not None else None,
        )

    def _classify_regime(
        self,
        alert: ShiftAlert,
        lookback_30m: BeforeStateWindow,
        activity: BeforeStateActivity,
    ) -> str:
        net_delta_30m = lookback_30m.net_delta_pct
        range_30m = lookback_30m.range_pct
        alert_direction = 1 if alert.shift.signed_delta_pct >= 0 else -1
        if range_30m is not None and range_30m <= 4.0:
            if activity.tick_activity_ratio is None or activity.tick_activity_ratio >= 1.5:
                return "quiet_breakout"
        if net_delta_30m is not None and net_delta_30m <= -5.0 and alert_direction > 0:
            return "recovery_move"
        if net_delta_30m is not None and net_delta_30m >= 5.0 and alert_direction < 0:
            return "pullback_move"
        if net_delta_30m is not None and abs(net_delta_30m) >= 5.0 and (net_delta_30m * alert.shift.signed_delta_pct) > 0:
            return "trend_acceleration"
        if range_30m is not None and range_30m >= 12.0:
            return "choppy_regime"
        return "mixed_regime"

    @staticmethod
    def _avg_spread(rows: list[dict[str, object]]) -> float | None:
        spreads = [float(row["spread"]) for row in rows if row["spread"] is not None]
        if not spreads:
            return None
        return sum(spreads) / len(spreads)
