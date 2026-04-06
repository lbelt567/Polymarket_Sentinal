from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

from sentinel.processing.alerts import AlertLevel, ShiftAlert, level_at_least
from sentinel.utils.metrics import Metrics


class Notifier(Protocol):
    async def send(self, alert: ShiftAlert) -> None:
        ...


class AlertEnricher(Protocol):
    async def enrich(self, alert: ShiftAlert) -> ShiftAlert:
        ...


@dataclass(slots=True)
class AgentAlertPolicy:
    allowed_levels: set[AlertLevel]
    excluded_confidences: set[str]
    excluded_signal_sources: set[str]
    min_price: float
    max_price: float
    min_liquidity: float
    min_abs_move: float
    event_dedup_sec: int


@dataclass(slots=True)
class NotificationDispatcher:
    notifiers: list[Notifier]
    min_severity: AlertLevel
    policy: AgentAlertPolicy
    metrics: Metrics | None = None
    enricher: AlertEnricher | None = None
    _event_cooldowns: dict[str, int] = field(default_factory=dict)

    async def dispatch(self, alert: ShiftAlert) -> None:
        await self.dispatch_many([alert])

    async def dispatch_many(self, alerts: list[ShiftAlert]) -> list[ShiftAlert]:
        selected_alerts = self.select_for_dispatch(alerts)
        if self.enricher is not None and selected_alerts:
            selected_alerts = list(await asyncio.gather(*(self._enrich_one(alert) for alert in selected_alerts)))
        await asyncio.gather(*(self._fan_out(alert) for alert in selected_alerts))
        return selected_alerts

    def select_for_dispatch(self, alerts: list[ShiftAlert]) -> list[ShiftAlert]:
        eligible: list[ShiftAlert] = []
        for alert in alerts:
            if self._is_allowed(alert):
                eligible.append(alert)
            elif self.metrics is not None:
                self.metrics.increment("alerts_skipped_policy")
        by_event: dict[str, list[ShiftAlert]] = {}
        for alert in eligible:
            by_event.setdefault(self._event_key(alert), []).append(alert)

        now_ms = max((alert.timestamp_ms for alert in eligible), default=0)
        selected: list[ShiftAlert] = []
        for event_key, group in by_event.items():
            cooldown_until = self._event_cooldowns.get(event_key, 0)
            if cooldown_until > now_ms:
                if self.metrics is not None:
                    self.metrics.increment("alerts_skipped_event_dedup", len(group))
                continue
            best_alert = max(group, key=self._rank_key)
            selected.append(best_alert)
            self._event_cooldowns[event_key] = best_alert.timestamp_ms + (self.policy.event_dedup_sec * 1000)
        return selected

    async def _fan_out(self, alert: ShiftAlert) -> None:
        if not level_at_least(AlertLevel(alert.alert_level), self.min_severity):
            return
        if self.metrics is not None and self.notifiers:
            self.metrics.increment("alerts_dispatched")
        await asyncio.gather(*(self._dispatch_one(notifier, alert) for notifier in self.notifiers))

    async def _dispatch_one(self, notifier: Notifier, alert: ShiftAlert) -> None:
        try:
            await notifier.send(alert)
        except Exception as exc:  # pragma: no cover - runtime guard
            if self.metrics is not None:
                self.metrics.increment("notifier_failures")
            logging.getLogger(__name__).warning(
                "notifier_failed",
                extra={
                    "extra_data": {
                        "notifier": type(notifier).__name__,
                        "alert_id": alert.alert_id,
                        "error": str(exc),
                    }
                },
            )

    async def _enrich_one(self, alert: ShiftAlert) -> ShiftAlert:
        try:
            assert self.enricher is not None
            return await self.enricher.enrich(alert)
        except Exception as exc:  # pragma: no cover - runtime guard
            logging.getLogger(__name__).warning(
                "alert_enrichment_failed",
                extra={
                    "extra_data": {
                        "alert_id": alert.alert_id,
                        "error": str(exc),
                    }
                },
            )
            return alert

    def _is_allowed(self, alert: ShiftAlert) -> bool:
        level = AlertLevel(alert.alert_level)
        if level not in self.policy.allowed_levels:
            return False
        if alert.signal_quality.confidence.lower() in self.policy.excluded_confidences:
            return False
        if alert.signal_quality.signal_source.lower() in self.policy.excluded_signal_sources:
            return False
        price_current = float(alert.shift.price_current)
        if price_current < self.policy.min_price or price_current > self.policy.max_price:
            return False
        abs_move = abs(float(alert.shift.price_end) - float(alert.shift.price_start))
        if abs_move < self.policy.min_abs_move:
            return False
        liquidity = float(alert.market.get("liquidity", 0.0))
        if liquidity < self.policy.min_liquidity:
            return False
        return True

    @staticmethod
    def _event_key(alert: ShiftAlert) -> str:
        market = alert.market
        return str(market.get("event_slug") or market.get("event_id") or market.get("asset_id"))

    @staticmethod
    def _rank_key(alert: ShiftAlert) -> tuple[int, int, float, int, float]:
        level_score = {
            AlertLevel.CONFIRMATION: 2,
            AlertLevel.TREND: 1,
        }.get(AlertLevel(alert.alert_level), 0)
        confidence_score = {
            "weak": 0,
            "moderate": 1,
            "strong": 2,
        }.get(alert.signal_quality.confidence.lower(), 0)
        liquidity = float(alert.market.get("liquidity", 0.0))
        return (
            level_score,
            confidence_score,
            float(alert.shift.delta_pct),
            abs(float(alert.shift.price_end) - float(alert.shift.price_start)),
            int(alert.shift.ticks_in_window),
            liquidity,
        )
