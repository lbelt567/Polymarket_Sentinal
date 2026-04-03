from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

from sentinel.processing.alerts import AlertLevel, ShiftAlert, level_at_least


class Notifier(Protocol):
    async def send(self, alert: ShiftAlert) -> None:
        ...


@dataclass(slots=True)
class AgentAlertPolicy:
    allowed_levels: set[AlertLevel]
    excluded_confidences: set[str]
    excluded_signal_sources: set[str]
    min_price: float
    max_price: float
    min_liquidity: float
    event_dedup_sec: int


@dataclass(slots=True)
class NotificationDispatcher:
    notifiers: list[Notifier]
    min_severity: AlertLevel
    policy: AgentAlertPolicy
    _event_cooldowns: dict[str, int] = field(default_factory=dict)

    async def dispatch(self, alert: ShiftAlert) -> None:
        await self.dispatch_many([alert])

    async def dispatch_many(self, alerts: list[ShiftAlert]) -> list[ShiftAlert]:
        selected_alerts = self.select_for_dispatch(alerts)
        await asyncio.gather(*(self._fan_out(alert) for alert in selected_alerts))
        return selected_alerts

    def select_for_dispatch(self, alerts: list[ShiftAlert]) -> list[ShiftAlert]:
        eligible = [alert for alert in alerts if self._is_allowed(alert)]
        by_event: dict[str, list[ShiftAlert]] = {}
        for alert in eligible:
            by_event.setdefault(self._event_key(alert), []).append(alert)

        now_ms = max((alert.timestamp_ms for alert in eligible), default=0)
        selected: list[ShiftAlert] = []
        for event_key, group in by_event.items():
            cooldown_until = self._event_cooldowns.get(event_key, 0)
            if cooldown_until > now_ms:
                continue
            best_alert = max(group, key=self._rank_key)
            selected.append(best_alert)
            self._event_cooldowns[event_key] = best_alert.timestamp_ms + (self.policy.event_dedup_sec * 1000)
        return selected

    async def _fan_out(self, alert: ShiftAlert) -> None:
        if not level_at_least(AlertLevel(alert.alert_level), self.min_severity):
            return
        await asyncio.gather(*(self._dispatch_one(notifier, alert) for notifier in self.notifiers))

    async def _dispatch_one(self, notifier: Notifier, alert: ShiftAlert) -> None:
        try:
            await notifier.send(alert)
        except Exception as exc:  # pragma: no cover - runtime guard
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
            int(alert.shift.ticks_in_window),
            liquidity,
        )
