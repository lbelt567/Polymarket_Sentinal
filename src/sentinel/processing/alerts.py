from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum


class AlertLevel(StrEnum):
    SCOUT = "scout"
    CONFIRMATION = "confirmation"
    TREND = "trend"


LEVEL_LABELS = {
    AlertLevel.SCOUT: "Scout Shift",
    AlertLevel.CONFIRMATION: "Confirmed Shift",
    AlertLevel.TREND: "Decisive Shift",
}

LEVEL_ORDER = {
    AlertLevel.SCOUT: 1,
    AlertLevel.CONFIRMATION: 2,
    AlertLevel.TREND: 3,
}


def level_at_least(level: AlertLevel, minimum: AlertLevel) -> bool:
    return LEVEL_ORDER[level] >= LEVEL_ORDER[minimum]


@dataclass(slots=True)
class SignalQuality:
    signal_source: str
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    quote_age_sec: float | None
    liquidity_bucket: str
    confidence: str


@dataclass(slots=True)
class ShiftSummary:
    direction: str
    delta_pct: float
    signed_delta_pct: float
    price_start: float
    price_end: float
    price_current: float
    window_sec: int
    ticks_in_window: int


@dataclass(slots=True)
class BeforeStateWindow:
    window_sec: int
    price_start: float | None
    price_end: float | None
    net_delta_pct: float | None
    high: float | None
    low: float | None
    range_pct: float | None
    position_in_range: float | None
    tick_count: int
    avg_spread: float | None


@dataclass(slots=True)
class BeforeStateActivity:
    last_5m_tick_count: int
    prior_30m_avg_5m_tick_count: float | None
    tick_activity_ratio: float | None
    last_5m_avg_spread: float | None
    prior_30m_avg_spread: float | None
    spread_change_pct: float | None


@dataclass(slots=True)
class BeforeState:
    lookback_30m: BeforeStateWindow
    lookback_60m: BeforeStateWindow
    activity: BeforeStateActivity
    regime_label: str


@dataclass(slots=True)
class ShiftAlert:
    alert_id: str
    alert_level: str
    alert_level_label: str
    timestamp_iso: str
    timestamp_ms: int
    market: dict[str, object]
    shift: ShiftSummary
    signal_quality: SignalQuality
    before_state: BeforeState | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["shift"] = asdict(self.shift)
        payload["signal_quality"] = asdict(self.signal_quality)
        if self.before_state is not None:
            payload["before_state"] = asdict(self.before_state)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


def new_alert_timestamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()
