from __future__ import annotations

from typing import Any

from pydantic import Field

from contracts.common import ContractModel


class SignalQualityModel(ContractModel):
    signal_source: str
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    quote_age_sec: float | None = None
    liquidity_bucket: str
    confidence: str


class ShiftSummaryModel(ContractModel):
    direction: str
    delta_pct: float
    signed_delta_pct: float
    price_start: float
    price_end: float
    price_current: float
    window_sec: int
    ticks_in_window: int


class BeforeStateWindowModel(ContractModel):
    window_sec: int
    price_start: float | None = None
    price_end: float | None = None
    net_delta_pct: float | None = None
    high: float | None = None
    low: float | None = None
    range_pct: float | None = None
    position_in_range: float | None = None
    tick_count: int
    avg_spread: float | None = None


class BeforeStateActivityModel(ContractModel):
    last_5m_tick_count: int
    prior_30m_avg_5m_tick_count: float | None = None
    tick_activity_ratio: float | None = None
    last_5m_avg_spread: float | None = None
    prior_30m_avg_spread: float | None = None
    spread_change_pct: float | None = None


class BeforeStateModel(ContractModel):
    lookback_30m: BeforeStateWindowModel
    lookback_60m: BeforeStateWindowModel
    activity: BeforeStateActivityModel
    regime_label: str


class ShiftAlertMirror(ContractModel):
    alert_id: str
    alert_level: str
    alert_level_label: str
    timestamp_iso: str
    timestamp_ms: int
    market: dict[str, Any]
    shift: ShiftSummaryModel
    signal_quality: SignalQualityModel
    before_state: BeforeStateModel | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
