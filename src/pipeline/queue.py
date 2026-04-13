from __future__ import annotations

from dataclasses import dataclass

from contracts.common import StageName
from pipeline.config import QueueSection
from pipeline.db import JobLease, PipelineDB


@dataclass(slots=True)
class QueueLeaseRequest:
    stage: StageName
    worker_id: str


class StageQueue:
    def __init__(self, db: PipelineDB, config: QueueSection) -> None:
        self._db = db
        self._config = config

    def lease(self, request: QueueLeaseRequest) -> JobLease | None:
        return self._db.lease_next_job(request.stage, request.worker_id, self._config.lease_sec)

    def retry(self, lease: JobLease, error: str) -> None:
        self._db.complete_job_retry(
            lease,
            error=error,
            delay_sec=self._config.retry_backoff_sec,
            max_attempts=self._config.max_attempts,
        )
