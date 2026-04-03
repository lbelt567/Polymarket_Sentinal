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
class ShiftAlert:
    alert_id: str
    alert_level: str
    alert_level_label: str
    timestamp_iso: str
    timestamp_ms: int
    market: dict[str, object]
    shift: ShiftSummary
    signal_quality: SignalQuality

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["shift"] = asdict(self.shift)
        payload["signal_quality"] = asdict(self.signal_quality)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


def new_alert_timestamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()
