from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse
from uuid import UUID, uuid4

from contracts.analysis import TradeCandidate
from contracts.common import PipelineEnvelope, PortfolioSnapshot, StageAudit, StageName
from contracts.execution import OutcomeObservation
from contracts.research import EvidenceItem
from pipeline.tracing import canonical_json, payload_hash


@dataclass(slots=True)
class JobLease:
    job_id: UUID
    run_id: UUID
    trace_id: UUID
    stage: StageName
    message_id: UUID
    attempt_count: int


@dataclass(slots=True)
class _DBTarget:
    dialect: str
    location: str


class PipelineDB:
    def __init__(self, database_url: str) -> None:
        self._target = self._parse_url(database_url)
        self._conn: Any | None = None
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            if self._target.dialect == "sqlite":
                self._conn = self._open_sqlite(self._target.location)
                self._conn.executescript(self._load_schema("001_sqlite.sql"))
                self._conn.commit()
                return

            if self._target.dialect == "postgres":
                try:
                    import psycopg
                    from psycopg.rows import dict_row
                except ImportError as exc:  # pragma: no cover - dependency guard
                    raise RuntimeError("psycopg is required for PostgreSQL pipeline storage") from exc

                self._conn = psycopg.connect(self._target.location, row_factory=dict_row)
                with self._conn.cursor() as cur:
                    cur.execute(self._load_schema("001_postgres.sql"))
                self._conn.commit()
                return

            raise RuntimeError(f"Unsupported database dialect: {self._target.dialect}")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def ensure_trace(
        self,
        *,
        source_alert_id: str,
        market_asset_id: str | None = None,
        market_slug: str | None = None,
        event_slug: str | None = None,
        current_stage: StageName = StageName.INGEST,
        status: str = "received",
    ) -> UUID:
        now = self._now()
        with self._tx() as conn:
            row = self._fetchone(
                conn,
                "SELECT trace_id FROM pipeline_traces WHERE source_alert_id = ?",
                "SELECT trace_id FROM pipeline_traces WHERE source_alert_id = %s",
                (source_alert_id,),
            )
            if row is not None:
                trace_id = UUID(str(row["trace_id"]))
                self._execute(
                    conn,
                    """
                    UPDATE pipeline_traces
                    SET current_stage = ?, status = ?, updated_at = ?
                    WHERE trace_id = ?
                    """,
                    """
                    UPDATE pipeline_traces
                    SET current_stage = %s, status = %s, updated_at = %s
                    WHERE trace_id = %s
                    """,
                    (current_stage.value, status, now.isoformat(), str(trace_id)),
                )
                return trace_id

            trace_id = uuid4()
            self._execute(
                conn,
                """
                INSERT INTO pipeline_traces (
                    trace_id, source_alert_id, market_asset_id, market_slug, event_slug,
                    current_stage, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                """
                INSERT INTO pipeline_traces (
                    trace_id, source_alert_id, market_asset_id, market_slug, event_slug,
                    current_stage, status, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(trace_id),
                    source_alert_id,
                    market_asset_id,
                    market_slug,
                    event_slug,
                    current_stage.value,
                    status,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            return trace_id

    def upsert_message(self, envelope: PipelineEnvelope) -> tuple[UUID, bool]:
        with self._tx() as conn:
            return self._upsert_message_conn(conn, envelope)

    def get_message(self, message_id: UUID | str) -> PipelineEnvelope:
        with self._tx() as conn:
            row = self._fetchone(
                conn,
                """
                SELECT message_id, trace_id, stage, parent_message_id, schema_version,
                       idempotency_key, payload_jsonb, created_at
                FROM stage_messages
                WHERE message_id = ?
                """,
                """
                SELECT message_id, trace_id, stage, parent_message_id, schema_version,
                       idempotency_key, payload_jsonb, created_at
                FROM stage_messages
                WHERE message_id = %s
                """,
                (str(message_id),),
            )
        if row is None:
            raise KeyError(f"message not found: {message_id}")
        payload_raw = row["payload_jsonb"]
        if isinstance(payload_raw, str):
            payload = json.loads(payload_raw)
        else:
            payload = payload_raw
        return PipelineEnvelope(
            message_id=UUID(str(row["message_id"])),
            trace_id=UUID(str(row["trace_id"])),
            source_alert_id=self._source_alert_id_for_trace(UUID(str(row["trace_id"]))),
            parent_message_id=UUID(str(row["parent_message_id"])) if row["parent_message_id"] else None,
            stage=StageName(str(row["stage"])),
            schema_version=str(row["schema_version"]),
            idempotency_key=str(row["idempotency_key"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            payload=payload,
        )

    def enqueue_job(
        self,
        *,
        trace_id: UUID,
        stage: StageName,
        message_id: UUID,
        priority: int = 100,
        available_at: datetime | None = None,
    ) -> UUID:
        with self._tx() as conn:
            return self._enqueue_job_conn(
                conn,
                trace_id=trace_id,
                stage=stage,
                message_id=message_id,
                priority=priority,
                available_at=available_at,
            )

    def lease_next_job(self, stage: StageName, worker_id: str, lease_sec: int) -> JobLease | None:
        now = self._now()
        leased_until = now + timedelta(seconds=lease_sec)
        with self._tx() as conn:
            row = self._fetchone(
                conn,
                """
                SELECT job_id, trace_id, stage, message_id, attempt_count
                FROM stage_jobs
                WHERE stage = ?
                  AND state IN ('queued', 'retry_wait')
                  AND available_at <= ?
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                """,
                """
                SELECT job_id, trace_id, stage, message_id, attempt_count
                FROM stage_jobs
                WHERE stage = %s
                  AND state IN ('queued', 'retry_wait')
                  AND available_at <= %s
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (stage.value, now.isoformat()),
            )
            if row is None:
                return None

            attempt_count = int(row["attempt_count"]) + 1
            self._execute(
                conn,
                """
                UPDATE stage_jobs
                SET state = ?, attempt_count = ?, leased_until = ?, worker_id = ?, updated_at = ?
                WHERE job_id = ?
                """,
                """
                UPDATE stage_jobs
                SET state = %s, attempt_count = %s, leased_until = %s, worker_id = %s, updated_at = %s
                WHERE job_id = %s
                """,
                ("running", attempt_count, leased_until.isoformat(), worker_id, now.isoformat(), str(row["job_id"])),
            )
            run_id = uuid4()
            self._execute(
                conn,
                """
                INSERT INTO stage_runs (
                    run_id, job_id, message_id, started_at, finished_at, result_state,
                    model_name, prompt_version, token_input, token_output,
                    cost_usd, latency_ms, tool_calls_jsonb, error_jsonb
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                """
                INSERT INTO stage_runs (
                    run_id, job_id, message_id, started_at, finished_at, result_state,
                    model_name, prompt_version, token_input, token_output,
                    cost_usd, latency_ms, tool_calls_jsonb, error_jsonb
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                """,
                (
                    str(run_id),
                    str(row["job_id"]),
                    str(row["message_id"]),
                    now.isoformat(),
                    None,
                    "running",
                    None,
                    None,
                    0,
                    0,
                    "0",
                    0,
                    "[]",
                    None,
                ),
            )
            return JobLease(
                job_id=UUID(str(row["job_id"])),
                run_id=run_id,
                trace_id=UUID(str(row["trace_id"])),
                stage=StageName(str(row["stage"])),
                message_id=UUID(str(row["message_id"])),
                attempt_count=attempt_count,
            )

    def complete_job_success(
        self,
        lease: JobLease,
        *,
        output_envelope: PipelineEnvelope | None = None,
        audit: StageAudit | None = None,
        next_stage: StageName | None = None,
        next_priority: int = 100,
        next_available_at: datetime | None = None,
        trace_status: str = "running",
    ) -> UUID | None:
        now = self._now()
        with self._tx() as conn:
            output_message_id: UUID | None = None
            if output_envelope is not None:
                output_message_id, inserted = self._upsert_message_conn(conn, output_envelope)
                if next_stage is not None and inserted:
                    self._enqueue_job_conn(
                        conn,
                        trace_id=output_envelope.trace_id,
                        stage=next_stage,
                        message_id=output_message_id,
                        priority=next_priority,
                        available_at=next_available_at,
                    )
                self._execute(
                    conn,
                    """
                    UPDATE pipeline_traces
                    SET current_stage = ?, status = ?, updated_at = ?
                    WHERE trace_id = ?
                    """,
                    """
                    UPDATE pipeline_traces
                    SET current_stage = %s, status = %s, updated_at = %s
                    WHERE trace_id = %s
                    """,
                    (output_envelope.stage.value, trace_status, now.isoformat(), str(output_envelope.trace_id)),
                )

            self._execute(
                conn,
                """
                UPDATE stage_jobs
                SET state = ?, leased_until = ?, updated_at = ?, last_error = ?
                WHERE job_id = ?
                """,
                """
                UPDATE stage_jobs
                SET state = %s, leased_until = %s, updated_at = %s, last_error = %s
                WHERE job_id = %s
                """,
                ("succeeded", None, now.isoformat(), None, str(lease.job_id)),
            )
            tool_calls = audit.tool_calls if audit is not None else []
            self._execute(
                conn,
                """
                UPDATE stage_runs
                SET finished_at = ?, result_state = ?, model_name = ?, prompt_version = ?,
                    token_input = ?, token_output = ?, cost_usd = ?, latency_ms = ?,
                    tool_calls_jsonb = ?, error_jsonb = ?
                WHERE run_id = ?
                """,
                """
                UPDATE stage_runs
                SET finished_at = %s, result_state = %s, model_name = %s, prompt_version = %s,
                    token_input = %s, token_output = %s, cost_usd = %s, latency_ms = %s,
                    tool_calls_jsonb = %s::jsonb, error_jsonb = %s::jsonb
                WHERE run_id = %s
                """,
                (
                    now.isoformat(),
                    "succeeded",
                    audit.model_name if audit else None,
                    audit.prompt_version if audit else None,
                    audit.token_input if audit else 0,
                    audit.token_output if audit else 0,
                    str(audit.cost_usd if audit else Decimal("0")),
                    audit.latency_ms if audit else 0,
                    canonical_json(tool_calls),
                    canonical_json({"error": audit.error}) if audit and audit.error else None,
                    str(lease.run_id),
                ),
            )
            return output_message_id

    def complete_job_retry(self, lease: JobLease, *, error: str, delay_sec: int, max_attempts: int) -> None:
        if lease.attempt_count >= max_attempts:
            self.dead_letter_job(lease, reason=error)
            return
        now = self._now()
        available_at = now + timedelta(seconds=delay_sec)
        with self._tx() as conn:
            self._execute(
                conn,
                """
                UPDATE stage_jobs
                SET state = ?, leased_until = ?, available_at = ?, updated_at = ?, last_error = ?
                WHERE job_id = ?
                """,
                """
                UPDATE stage_jobs
                SET state = %s, leased_until = %s, available_at = %s, updated_at = %s, last_error = %s
                WHERE job_id = %s
                """,
                ("retry_wait", None, available_at.isoformat(), now.isoformat(), error, str(lease.job_id)),
            )
            self._execute(
                conn,
                """
                UPDATE stage_runs
                SET finished_at = ?, result_state = ?, error_jsonb = ?
                WHERE run_id = ?
                """,
                """
                UPDATE stage_runs
                SET finished_at = %s, result_state = %s, error_jsonb = %s::jsonb
                WHERE run_id = %s
                """,
                (now.isoformat(), "retry_wait", canonical_json({"error": error}), str(lease.run_id)),
            )

    def dead_letter_job(self, lease: JobLease, *, reason: str) -> None:
        now = self._now()
        envelope = self.get_message(lease.message_id)
        with self._tx() as conn:
            self._execute(
                conn,
                """
                UPDATE stage_jobs
                SET state = ?, leased_until = ?, updated_at = ?, last_error = ?
                WHERE job_id = ?
                """,
                """
                UPDATE stage_jobs
                SET state = %s, leased_until = %s, updated_at = %s, last_error = %s
                WHERE job_id = %s
                """,
                ("dead_letter", None, now.isoformat(), reason, str(lease.job_id)),
            )
            self._execute(
                conn,
                """
                UPDATE stage_runs
                SET finished_at = ?, result_state = ?, error_jsonb = ?
                WHERE run_id = ?
                """,
                """
                UPDATE stage_runs
                SET finished_at = %s, result_state = %s, error_jsonb = %s::jsonb
                WHERE run_id = %s
                """,
                (now.isoformat(), "dead_letter", canonical_json({"error": reason}), str(lease.run_id)),
            )
            self._execute(
                conn,
                """
                INSERT INTO dead_letters (
                    dead_letter_id, job_id, stage, reason, payload_jsonb,
                    first_failed_at, last_failed_at, replay_count, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                """
                INSERT INTO dead_letters (
                    dead_letter_id, job_id, stage, reason, payload_jsonb,
                    first_failed_at, last_failed_at, replay_count, resolved_at
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                """,
                (
                    str(uuid4()),
                    str(lease.job_id),
                    lease.stage.value,
                    reason,
                    canonical_json(envelope.payload),
                    now.isoformat(),
                    now.isoformat(),
                    0,
                    None,
                ),
            )

    def save_research_evidence(self, message_id: UUID, evidence: list[EvidenceItem]) -> None:
        if not evidence:
            return
        with self._tx() as conn:
            for item in evidence:
                self._execute(
                    conn,
                    """
                    INSERT INTO research_evidence (
                        evidence_id, message_id, url, title, publisher,
                        published_at, retrieved_at, source_tier, relevance_score, claim_tag
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    """
                    INSERT INTO research_evidence (
                        evidence_id, message_id, url, title, publisher,
                        published_at, retrieved_at, source_tier, relevance_score, claim_tag
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid4()),
                        str(message_id),
                        item.url,
                        item.title,
                        item.publisher,
                        item.published_at.isoformat() if item.published_at else None,
                        item.retrieved_at.isoformat(),
                        item.source_tier.value,
                        item.relevance_score,
                        item.claim_tag,
                    ),
                )

    def save_analysis_candidates(
        self,
        message_id: UUID,
        *,
        candidates: list[TradeCandidate],
        rejected_candidates: list[TradeCandidate],
    ) -> None:
        with self._tx() as conn:
            for status, items in (("candidate", candidates), ("rejected", rejected_candidates)):
                for item in items:
                    self._execute(
                        conn,
                        """
                        INSERT INTO analysis_candidates (
                            candidate_id, message_id, symbol, side, status, score,
                            broker_eligible, max_notional, stop_loss_pct, features_jsonb, filters_jsonb
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        """
                        INSERT INTO analysis_candidates (
                            candidate_id, message_id, symbol, side, status, score,
                            broker_eligible, max_notional, stop_loss_pct, features_jsonb, filters_jsonb
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                        """,
                        (
                            str(item.candidate_id),
                            str(message_id),
                            item.symbol,
                            item.side.value,
                            status,
                            item.score,
                            int(item.broker_eligible),
                            str(item.max_notional),
                            str(item.stop_loss_pct),
                            canonical_json(item.features),
                            canonical_json([result.model_dump(mode="json") for result in item.filters]),
                        ),
                    )

    def save_portfolio_snapshot(self, snapshot: PortfolioSnapshot) -> UUID:
        with self._tx() as conn:
            self._execute(
                conn,
                """
                INSERT INTO portfolio_snapshots (
                    snapshot_id, captured_at, account_id, equity, cash, buying_power,
                    gross_exposure, net_exposure, sector_exposure_jsonb, positions_jsonb
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                """
                INSERT INTO portfolio_snapshots (
                    snapshot_id, captured_at, account_id, equity, cash, buying_power,
                    gross_exposure, net_exposure, sector_exposure_jsonb, positions_jsonb
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                """,
                (
                    str(snapshot.snapshot_id),
                    snapshot.captured_at.isoformat(),
                    snapshot.account_id,
                    str(snapshot.equity),
                    str(snapshot.cash),
                    str(snapshot.buying_power),
                    str(snapshot.gross_exposure),
                    str(snapshot.net_exposure),
                    canonical_json({k: str(v) for k, v in snapshot.sector_exposure.items()}),
                    canonical_json(snapshot.positions),
                ),
            )
        return snapshot.snapshot_id

    def create_approval_request(
        self,
        *,
        trace_id: UUID,
        message_id: UUID,
        candidate_id: UUID,
        channel: str,
        callback_token: str,
        expires_at: datetime,
    ) -> UUID:
        approval_id = uuid4()
        now = self._now()
        with self._tx() as conn:
            self._execute(
                conn,
                """
                INSERT INTO approval_requests (
                    approval_id, trace_id, message_id, candidate_id, channel, status,
                    requested_at, expires_at, decided_at, approver_id, decision_reason, callback_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                """
                INSERT INTO approval_requests (
                    approval_id, trace_id, message_id, candidate_id, channel, status,
                    requested_at, expires_at, decided_at, approver_id, decision_reason, callback_token
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(approval_id),
                    str(trace_id),
                    str(message_id),
                    str(candidate_id),
                    channel,
                    "requested",
                    now.isoformat(),
                    expires_at.isoformat(),
                    None,
                    None,
                    None,
                    callback_token,
                ),
            )
        return approval_id

    def get_approval_request_by_message(self, message_id: UUID) -> dict[str, Any] | None:
        with self._tx() as conn:
            return self._fetchone(
                conn,
                "SELECT * FROM approval_requests WHERE message_id = ? ORDER BY requested_at DESC LIMIT 1",
                "SELECT * FROM approval_requests WHERE message_id = %s ORDER BY requested_at DESC LIMIT 1",
                (str(message_id),),
            )

    def decide_approval(self, callback_token: str, *, status: str, approver_id: str, reason: str | None = None) -> UUID:
        now = self._now()
        with self._tx() as conn:
            row = self._fetchone(
                conn,
                "SELECT approval_id FROM approval_requests WHERE callback_token = ?",
                "SELECT approval_id FROM approval_requests WHERE callback_token = %s",
                (callback_token,),
            )
            if row is None:
                raise KeyError("approval request not found")
            self._execute(
                conn,
                """
                UPDATE approval_requests
                SET status = ?, decided_at = ?, approver_id = ?, decision_reason = ?
                WHERE callback_token = ?
                """,
                """
                UPDATE approval_requests
                SET status = %s, decided_at = %s, approver_id = %s, decision_reason = %s
                WHERE callback_token = %s
                """,
                (status, now.isoformat(), approver_id, reason, callback_token),
            )
            return UUID(str(row["approval_id"]))

    def save_broker_order(
        self,
        *,
        trace_id: UUID,
        candidate_id: UUID,
        client_order_id: str,
        broker: str,
        broker_order_id: str | None,
        symbol: str,
        side: str,
        qty: Decimal | None,
        notional: Decimal | None,
        order_type: str,
        tif: str,
        status: str,
        avg_fill_price: Decimal | None,
        stop_order_id: str | None,
        raw_response: dict[str, Any],
    ) -> UUID:
        with self._tx() as conn:
            existing = self._fetchone(
                conn,
                "SELECT order_id FROM broker_orders WHERE client_order_id = ?",
                "SELECT order_id FROM broker_orders WHERE client_order_id = %s",
                (client_order_id,),
            )
            if existing is not None:
                return UUID(str(existing["order_id"]))
            order_id = uuid4()
            self._execute(
                conn,
                """
                INSERT INTO broker_orders (
                    order_id, trace_id, candidate_id, client_order_id, broker, broker_order_id,
                    symbol, side, qty, notional, order_type, tif, submitted_at, status,
                    avg_fill_price, stop_order_id, raw_response_jsonb
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                """
                INSERT INTO broker_orders (
                    order_id, trace_id, candidate_id, client_order_id, broker, broker_order_id,
                    symbol, side, qty, notional, order_type, tif, submitted_at, status,
                    avg_fill_price, stop_order_id, raw_response_jsonb
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    str(order_id),
                    str(trace_id),
                    str(candidate_id),
                    client_order_id,
                    broker,
                    broker_order_id,
                    symbol,
                    side,
                    str(qty) if qty is not None else None,
                    str(notional) if notional is not None else None,
                    order_type,
                    tif,
                    self._now().isoformat(),
                    status,
                    str(avg_fill_price) if avg_fill_price is not None else None,
                    stop_order_id,
                    canonical_json(raw_response),
                ),
            )
        return order_id

    def get_broker_order_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            return self._fetchone(
                conn,
                "SELECT * FROM broker_orders WHERE client_order_id = ?",
                "SELECT * FROM broker_orders WHERE client_order_id = %s",
                (client_order_id,),
            )

    def save_outcome_observation(self, observation: OutcomeObservation) -> UUID:
        observation_id = uuid4()
        with self._tx() as conn:
            self._execute(
                conn,
                """
                INSERT INTO outcome_observations (
                    observation_id, trace_id, symbol, horizon, scheduled_for,
                    observed_at, entry_price, exit_price, return_pct,
                    benchmark_return_pct, label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                """
                INSERT INTO outcome_observations (
                    observation_id, trace_id, symbol, horizon, scheduled_for,
                    observed_at, entry_price, exit_price, return_pct,
                    benchmark_return_pct, label
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(observation_id),
                    str(observation.trace_id),
                    observation.symbol,
                    observation.horizon.value,
                    observation.scheduled_for.isoformat(),
                    observation.observed_at.isoformat() if observation.observed_at else None,
                    str(observation.entry_price),
                    str(observation.exit_price) if observation.exit_price is not None else None,
                    str(observation.return_pct) if observation.return_pct is not None else None,
                    str(observation.benchmark_return_pct) if observation.benchmark_return_pct is not None else None,
                    observation.label,
                ),
            )
        return observation_id

    def list_outcome_observations(self, trace_id: UUID) -> list[dict[str, Any]]:
        with self._tx() as conn:
            rows = self._fetchall(
                conn,
                """
                SELECT * FROM outcome_observations
                WHERE trace_id = ?
                ORDER BY scheduled_for ASC
                """,
                """
                SELECT * FROM outcome_observations
                WHERE trace_id = %s
                ORDER BY scheduled_for ASC
                """,
                (str(trace_id),),
            )
        return rows

    def update_outcome_observation(
        self,
        observation_id: UUID | str,
        *,
        observed_at: datetime,
        exit_price: Decimal,
        return_pct: Decimal,
        benchmark_return_pct: Decimal | None,
        label: str,
    ) -> None:
        with self._tx() as conn:
            self._execute(
                conn,
                """
                UPDATE outcome_observations
                SET observed_at = ?, exit_price = ?, return_pct = ?, benchmark_return_pct = ?, label = ?
                WHERE observation_id = ?
                """,
                """
                UPDATE outcome_observations
                SET observed_at = %s, exit_price = %s, return_pct = %s, benchmark_return_pct = %s, label = %s
                WHERE observation_id = %s
                """,
                (
                    observed_at.isoformat(),
                    str(exit_price),
                    str(return_pct),
                    str(benchmark_return_pct) if benchmark_return_pct is not None else None,
                    label,
                    str(observation_id),
                ),
            )

    def get_dead_letters(self) -> list[dict[str, Any]]:
        with self._tx() as conn:
            rows = self._fetchall(
                conn,
                "SELECT * FROM dead_letters ORDER BY first_failed_at ASC",
                "SELECT * FROM dead_letters ORDER BY first_failed_at ASC",
                (),
            )
        return [dict(row) for row in rows]

    def _source_alert_id_for_trace(self, trace_id: UUID) -> str:
        with self._tx() as conn:
            row = self._fetchone(
                conn,
                "SELECT source_alert_id FROM pipeline_traces WHERE trace_id = ?",
                "SELECT source_alert_id FROM pipeline_traces WHERE trace_id = %s",
                (str(trace_id),),
            )
        if row is None:
            raise KeyError(f"trace not found: {trace_id}")
        return str(row["source_alert_id"])

    @staticmethod
    def _parse_url(url: str) -> _DBTarget:
        if not url or url == ":memory:":
            return _DBTarget("sqlite", ":memory:")
        if url.startswith("sqlite:///"):
            return _DBTarget("sqlite", url[len("sqlite:///") :])
        parsed = urlparse(url)
        if parsed.scheme in {"postgres", "postgresql"}:
            return _DBTarget("postgres", url)
        return _DBTarget("sqlite", url)

    @staticmethod
    def _open_sqlite(path_str: str) -> sqlite3.Connection:
        if path_str != ":memory:":
            path = Path(path_str)
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path_str, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _load_schema(filename: str) -> str:
        return (Path(__file__).parent / "migrations" / filename).read_text()

    def _now(self) -> datetime:
        return datetime.now(tz=timezone.utc)

    def _upsert_message_conn(self, conn: Any, envelope: PipelineEnvelope) -> tuple[UUID, bool]:
        existing = self._fetchone(
            conn,
            "SELECT message_id FROM stage_messages WHERE idempotency_key = ?",
            "SELECT message_id FROM stage_messages WHERE idempotency_key = %s",
            (envelope.idempotency_key,),
        )
        if existing is not None:
            return UUID(str(existing["message_id"])), False

        payload = canonical_json(envelope.payload)
        self._execute(
            conn,
            """
            INSERT INTO stage_messages (
                message_id, trace_id, stage, parent_message_id, schema_version,
                idempotency_key, payload_jsonb, payload_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            """
            INSERT INTO stage_messages (
                message_id, trace_id, stage, parent_message_id, schema_version,
                idempotency_key, payload_jsonb, payload_hash, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                str(envelope.message_id),
                str(envelope.trace_id),
                envelope.stage.value,
                str(envelope.parent_message_id) if envelope.parent_message_id else None,
                envelope.schema_version,
                envelope.idempotency_key,
                payload,
                payload_hash(envelope.payload),
                envelope.created_at.isoformat(),
            ),
        )
        return envelope.message_id, True

    def _enqueue_job_conn(
        self,
        conn: Any,
        *,
        trace_id: UUID,
        stage: StageName,
        message_id: UUID,
        priority: int,
        available_at: datetime | None,
    ) -> UUID:
        now = self._now()
        available = available_at or now
        job_id = uuid4()
        existing = self._fetchone(
            conn,
            "SELECT job_id FROM stage_jobs WHERE stage = ? AND message_id = ?",
            "SELECT job_id FROM stage_jobs WHERE stage = %s AND message_id = %s",
            (stage.value, str(message_id)),
        )
        if existing is not None:
            return UUID(str(existing["job_id"]))
        self._execute(
            conn,
            """
            INSERT INTO stage_jobs (
                job_id, trace_id, stage, message_id, state, priority,
                attempt_count, available_at, leased_until, worker_id,
                last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            """
            INSERT INTO stage_jobs (
                job_id, trace_id, stage, message_id, state, priority,
                attempt_count, available_at, leased_until, worker_id,
                last_error, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(job_id),
                str(trace_id),
                stage.value,
                str(message_id),
                "queued",
                priority,
                0,
                available.isoformat(),
                None,
                None,
                None,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        return job_id

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        if self._conn is None:
            raise RuntimeError("PipelineDB is not initialized")
        with self._lock:
            if self._target.dialect == "sqlite":
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    yield self._conn
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
                return

            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _execute(self, conn: Any, sqlite_sql: str, postgres_sql: str, params: tuple[Any, ...]) -> None:
        sql = sqlite_sql if self._target.dialect == "sqlite" else postgres_sql
        conn.execute(sql, params)

    def _fetchone(self, conn: Any, sqlite_sql: str, postgres_sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        sql = sqlite_sql if self._target.dialect == "sqlite" else postgres_sql
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    def _fetchall(self, conn: Any, sqlite_sql: str, postgres_sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        sql = sqlite_sql if self._target.dialect == "sqlite" else postgres_sql
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        return [dict(row) for row in rows]
