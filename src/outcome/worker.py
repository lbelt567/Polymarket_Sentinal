from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal

from contracts.common import PipelineEnvelope, StageAudit, StageName
from contracts.execution import ExecutionDecision
from outcome.pricing import StaticOutcomePriceProvider
from pipeline.config import PipelineConfig
from pipeline.db import PipelineDB
from pipeline.idempotency import build_outcome_key
from pipeline.outcomes import OutcomePriceProvider, classify_outcome


class OutcomeWorker:
    def __init__(
        self,
        db: PipelineDB,
        config: PipelineConfig,
        price_provider: OutcomePriceProvider,
        worker_id: str = "outcome-1",
    ) -> None:
        self._db = db
        self._config = config
        self._price_provider = price_provider
        self._worker_id = worker_id

    def process_next(self) -> bool:
        lease = self._db.lease_next_job(StageName.OUTCOME, self._worker_id, self._config.queue.lease_sec)
        if lease is None:
            return False
        try:
            parent = self._db.get_message(lease.message_id)
            decision = ExecutionDecision.model_validate(parent.payload)
            if decision.intent is None:
                self._db.complete_job_success(lease, trace_status="outcome_skipped")
                return True

            observations = self._db.list_outcome_observations(parent.trace_id)
            if not observations:
                self._db.complete_job_success(lease, trace_status="outcome_missing")
                return True

            now = datetime.now(tz=timezone.utc)
            pending = [row for row in observations if row["observed_at"] is None]
            due = [row for row in pending if datetime.fromisoformat(str(row["scheduled_for"])) <= now]
            if not due:
                next_due = min(datetime.fromisoformat(str(row["scheduled_for"])) for row in pending)
                delay = max(1, int((next_due - now).total_seconds()))
                self._db.complete_job_retry(lease, error="waiting_for_outcome_window", delay_sec=delay, max_attempts=10_000)
                return False

            for row in due:
                scheduled_for = datetime.fromisoformat(str(row["scheduled_for"]))
                exit_price = self._price_provider.get_price(decision.intent.symbol, scheduled_for)
                benchmark = self._price_provider.get_benchmark_return(scheduled_for, row["horizon"]) if hasattr(self._price_provider, "get_benchmark_return") else None
                observed = classify_outcome(Decimal(str(row["entry_price"])), exit_price, benchmark)
                self._db.update_outcome_observation(
                    row["observation_id"],
                    observed_at=now,
                    exit_price=observed.exit_price,
                    return_pct=observed.return_pct,
                    benchmark_return_pct=observed.benchmark_return_pct,
                    label=observed.label,
                )

            still_pending = [row for row in self._db.list_outcome_observations(parent.trace_id) if row["observed_at"] is None]
            envelope = PipelineEnvelope.new(
                trace_id=parent.trace_id,
                source_alert_id=parent.source_alert_id,
                parent_message_id=parent.message_id,
                stage=StageName.OUTCOME,
                schema_version=parent.schema_version,
                idempotency_key=build_outcome_key(parent.trace_id, "final" if not still_pending else f"pending-{len(still_pending)}"),
                payload={"observed_count": len(due), "pending_count": len(still_pending)},
            )
            audit = StageAudit(message_id=envelope.message_id)
            if still_pending:
                next_due = min(datetime.fromisoformat(str(row["scheduled_for"])) for row in still_pending)
                self._db.complete_job_success(
                    lease,
                    output_envelope=envelope,
                    audit=audit,
                    next_stage=StageName.OUTCOME,
                    next_available_at=next_due,
                    trace_status="outcome_pending",
                )
            else:
                self._db.complete_job_success(lease, output_envelope=envelope, audit=audit, trace_status="outcome_complete")
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
