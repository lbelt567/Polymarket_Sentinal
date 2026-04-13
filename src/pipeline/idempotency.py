from __future__ import annotations

import hashlib
from uuid import UUID


def build_ingest_key(alert_id: str) -> str:
    return f"scout:{alert_id}"


def build_research_key(parent_message_id: UUID | str, prompt_version: str) -> str:
    return f"research:{parent_message_id}:{prompt_version}"


def build_analysis_key(parent_message_id: UUID | str, ruleset_version: str, data_snapshot_ts: str) -> str:
    return f"analysis:{parent_message_id}:{ruleset_version}:{data_snapshot_ts}"


def build_execution_key(parent_message_id: UUID | str, risk_ruleset_version: str, approval_mode: str) -> str:
    return f"execution:{parent_message_id}:{risk_ruleset_version}:{approval_mode}"


def build_outcome_key(trace_id: UUID | str, horizon: str) -> str:
    return f"outcome:{trace_id}:{horizon}"


def build_stable_order_id(trace_id: UUID | str, candidate_id: UUID | str, risk_ruleset_version: str) -> str:
    payload = f"{trace_id}:{candidate_id}:{risk_ruleset_version}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:24]
    return f"sentinel-{digest}"
