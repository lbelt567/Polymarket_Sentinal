from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from pipeline.db import PipelineDB


def generate_callback_token() -> str:
    return secrets.token_urlsafe(24)


@dataclass(slots=True)
class ApprovalDecision:
    approval_id: UUID
    status: str
    decided_at: datetime


class ApprovalService:
    def __init__(self, db: PipelineDB) -> None:
        self._db = db

    def request(
        self,
        *,
        trace_id: UUID,
        message_id: UUID,
        candidate_id: UUID,
        channel: str,
        timeout_minutes: int,
    ) -> tuple[UUID, str, datetime]:
        expires_at = datetime.now(tz=timezone.utc) + timedelta(minutes=timeout_minutes)
        token = generate_callback_token()
        approval_id = self._db.create_approval_request(
            trace_id=trace_id,
            message_id=message_id,
            candidate_id=candidate_id,
            channel=channel,
            callback_token=token,
            expires_at=expires_at,
        )
        return approval_id, token, expires_at

    def approve(self, token: str, approver_id: str, reason: str | None = None) -> ApprovalDecision:
        approval_id = self._db.decide_approval(token, status="approved", approver_id=approver_id, reason=reason)
        return ApprovalDecision(approval_id=approval_id, status="approved", decided_at=datetime.now(tz=timezone.utc))

    def reject(self, token: str, approver_id: str, reason: str | None = None) -> ApprovalDecision:
        approval_id = self._db.decide_approval(token, status="rejected", approver_id=approver_id, reason=reason)
        return ApprovalDecision(approval_id=approval_id, status="rejected", decided_at=datetime.now(tz=timezone.utc))
