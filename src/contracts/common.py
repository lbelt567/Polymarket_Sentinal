from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class StageName(StrEnum):
    INGEST = "ingest"
    RESEARCH = "research"
    ANALYSIS = "analysis"
    EXECUTION = "execution"
    APPROVAL = "approval"
    OUTCOME = "outcome"


class PipelineEnvelope(ContractModel):
    message_id: UUID
    trace_id: UUID
    source_alert_id: str
    parent_message_id: UUID | None = None
    stage: StageName
    schema_version: str
    idempotency_key: str
    created_at: datetime
    payload: dict[str, Any]

    @classmethod
    def new(
        cls,
        *,
        trace_id: UUID,
        source_alert_id: str,
        stage: StageName,
        schema_version: str,
        idempotency_key: str,
        payload: dict[str, Any],
        parent_message_id: UUID | None = None,
    ) -> "PipelineEnvelope":
        return cls(
            message_id=uuid4(),
            trace_id=trace_id,
            source_alert_id=source_alert_id,
            parent_message_id=parent_message_id,
            stage=stage,
            schema_version=schema_version,
            idempotency_key=idempotency_key,
            created_at=datetime.now(tz=timezone.utc),
            payload=payload,
        )


class StageAudit(ContractModel):
    message_id: UUID
    model_name: str | None = None
    prompt_version: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    token_input: int = 0
    token_output: int = 0
    latency_ms: int = 0
    cost_usd: Decimal = Decimal("0")
    error: str | None = None


class PortfolioSnapshot(ContractModel):
    snapshot_id: UUID = Field(default_factory=uuid4)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    account_id: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    sector_exposure: dict[str, Decimal] = Field(default_factory=dict)
    positions: list[dict[str, Any]] = Field(default_factory=list)
