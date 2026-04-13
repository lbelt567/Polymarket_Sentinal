from __future__ import annotations

import hashlib
import json
from typing import Any

from contracts.common import PipelineEnvelope, StageAudit


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def envelope_payload(envelope: PipelineEnvelope) -> dict[str, Any]:
    return envelope.model_dump(mode="json")


def audit_payload(audit: StageAudit | None) -> dict[str, Any] | None:
    if audit is None:
        return None
    return audit.model_dump(mode="json")
