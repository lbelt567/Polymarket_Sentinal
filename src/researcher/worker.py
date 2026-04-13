from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import uuid4

from contracts.common import PipelineEnvelope, StageAudit, StageName
from contracts.research import IngestedAlert, ResearchStatus
from pipeline.config import PipelineConfig
from pipeline.db import PipelineDB
from pipeline.idempotency import build_research_key
from researcher.agent import FallbackResearcherAgent, ResearcherAgent


class ResearcherWorker:
    def __init__(self, db: PipelineDB, config: PipelineConfig, agent: ResearcherAgent | None = None, worker_id: str = "researcher-1") -> None:
        self._db = db
        self._config = config
        self._agent = agent or FallbackResearcherAgent(prompt_version=config.researcher.prompt_version)
        self._worker_id = worker_id

    def process_next(self) -> bool:
        lease = self._db.lease_next_job(StageName.RESEARCH, self._worker_id, self._config.queue.lease_sec)
        if lease is None:
            return False

        started = time.perf_counter()
        try:
            parent = self._db.get_message(lease.message_id)
            ingested = IngestedAlert.model_validate(parent.payload)
            report = self._agent.analyze(ingested)
            envelope = PipelineEnvelope.new(
                trace_id=parent.trace_id,
                source_alert_id=parent.source_alert_id,
                parent_message_id=parent.message_id,
                stage=StageName.RESEARCH,
                schema_version=parent.schema_version,
                idempotency_key=build_research_key(parent.message_id, self._config.researcher.prompt_version),
                payload=report.model_dump(mode="json"),
            )
            audit = StageAudit(
                message_id=envelope.message_id,
                model_name=getattr(self._agent, "model_name", None),
                prompt_version=getattr(self._agent, "prompt_version", None),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            next_stage = StageName.ANALYSIS if report.status == ResearchStatus.PASS else None
            trace_status = report.status.value
            message_id = self._db.complete_job_success(
                lease,
                output_envelope=envelope,
                audit=audit,
                next_stage=next_stage,
                trace_status=trace_status,
            )
            if message_id is not None:
                self._db.save_research_evidence(message_id, report.evidence)
            return True
        except Exception as exc:
            self._db.complete_job_retry(
                lease,
                error=str(exc),
                delay_sec=self._config.queue.retry_backoff_sec,
                max_attempts=self._config.queue.max_attempts,
            )
            return False

    def run_forever(self, poll_interval_sec: float = 1.0) -> None:
        while True:
            processed = self.process_next()
            if not processed:
                time.sleep(poll_interval_sec)
