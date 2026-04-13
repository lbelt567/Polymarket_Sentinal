from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from contracts.analysis import ThesisHorizon, TradeSide
from contracts.common import ContractModel


class ExecutionStatus(StrEnum):
    REJECTED_RISK = "rejected_risk"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    ERROR = "error"


class OrderType(StrEnum):
    LIMIT = "limit"


class TimeInForce(StrEnum):
    DAY = "day"


class OutcomeHorizon(StrEnum):
    T_PLUS_15M = "t+15m"
    T_PLUS_1H = "t+1h"
    NEXT_SESSION_CLOSE = "next_session_close"


class TradeIntent(ContractModel):
    candidate_id: UUID
    symbol: str
    side: TradeSide
    order_type: OrderType
    tif: TimeInForce
    max_notional: Decimal
    limit_price: Decimal
    stop_loss_pct: Decimal
    max_slippage_bps: int
    thesis_horizon: ThesisHorizon


class RiskCheckResult(ContractModel):
    name: str
    passed: bool
    measured_value: str
    threshold: str
    reason: str


class ExecutionDecision(ContractModel):
    status: ExecutionStatus
    selected_candidate_id: UUID | None = None
    intent: TradeIntent | None = None
    risk_checks: list[RiskCheckResult] = Field(default_factory=list)
    approval_id: UUID | None = None
    broker_order_ids: list[str] = Field(default_factory=list)
    stage_summary: str


class OutcomeObservation(ContractModel):
    trace_id: UUID
    symbol: str
    horizon: OutcomeHorizon
    scheduled_for: datetime
    observed_at: datetime | None = None
    entry_price: Decimal
    exit_price: Decimal | None = None
    return_pct: Decimal | None = None
    benchmark_return_pct: Decimal | None = None
    label: str | None = None
