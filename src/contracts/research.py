from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pydantic import Field

from contracts.common import ContractModel
from contracts.scout import ShiftAlertMirror


class ResearchStatus(StrEnum):
    PASS = "pass"
    DEFER = "defer"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"


class CatalystType(StrEnum):
    POLICY = "policy"
    ECONOMIC_DATA = "economic_data"
    GEOPOLITICAL = "geopolitical"
    CORPORATE = "corporate"
    LEGAL = "legal"
    OTHER = "other"


class InstrumentType(StrEnum):
    EQUITY = "equity"
    ETF = "etf"


class Direction(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class Horizon(StrEnum):
    INTRADAY = "intraday"
    SWING = "swing"


class SourceTier(StrEnum):
    PRIMARY = "primary"
    MAJOR_PRESS = "major_press"
    SECONDARY = "secondary"


class LinkageType(StrEnum):
    DIRECT = "direct"
    SUPPLIER = "supplier"
    COMPETITOR = "competitor"
    SECTOR_PROXY = "sector_proxy"
    MACRO_PROXY = "macro_proxy"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IngestedAlert(ContractModel):
    shift_alert: ShiftAlertMirror
    received_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    signature_verified: bool
    replay_source: str | None = None
    priority: int = 100


class EvidenceItem(ContractModel):
    url: str
    title: str
    publisher: str
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    source_tier: SourceTier
    relevance_score: float
    claim_tag: str


class AssetHypothesis(ContractModel):
    symbol: str
    instrument_type: InstrumentType
    direction: Direction
    horizon: Horizon
    linkage_type: LinkageType
    confidence: Confidence
    tradable_universe: bool
    rationale_summary: str


class ResearchReport(ContractModel):
    status: ResearchStatus
    catalyst_type: CatalystType
    catalyst_summary: str
    catalyst_started_at: datetime | None = None
    freshness_sec: int | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)
    asset_hypotheses: list[AssetHypothesis] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    stage_summary: str
