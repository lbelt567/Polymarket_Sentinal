from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field

from contracts.common import ContractModel


class AnalysisStatus(StrEnum):
    PASS = "pass"
    REJECT = "reject"
    NEEDS_REVIEW = "needs_review"


class TradeSide(StrEnum):
    BUY = "buy"


class ThesisHorizon(StrEnum):
    INTRADAY = "intraday"
    SWING = "swing"


class FilterResult(ContractModel):
    name: str
    passed: bool
    value: str
    reason: str


class TradeCandidate(ContractModel):
    candidate_id: UUID = Field(default_factory=uuid4)
    symbol: str
    side: TradeSide
    thesis_horizon: ThesisHorizon
    broker_eligible: bool
    market_snapshot: dict[str, Any]
    features: dict[str, Any]
    filters: list[FilterResult] = Field(default_factory=list)
    score: float
    max_notional: Decimal
    stop_loss_pct: Decimal
    expiry_at: datetime


class AnalysisReport(ContractModel):
    status: AnalysisStatus
    candidates: list[TradeCandidate] = Field(default_factory=list)
    rejected_candidates: list[TradeCandidate] = Field(default_factory=list)
    portfolio_snapshot_id: UUID
    stage_summary: str
