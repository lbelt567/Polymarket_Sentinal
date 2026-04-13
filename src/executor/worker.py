from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from contracts.analysis import AnalysisReport
from contracts.common import PipelineEnvelope, PortfolioSnapshot, StageAudit, StageName
from contracts.execution import ExecutionDecision, ExecutionStatus, OrderType, TimeInForce, TradeIntent
from executor.broker import BrokerClient, PaperBrokerClient
from executor.risk.circuit_breaker import CircuitBreakerState
from executor.risk.limits import RiskEngine
from pipeline.approvals import ApprovalService
from pipeline.config import PipelineConfig
from pipeline.db import PipelineDB
from pipeline.idempotency import build_execution_key, build_stable_order_id
from pipeline.outcomes import schedule_default_outcomes


class ExecutionContextProvider:
    def get_portfolio_snapshot(self, snapshot_id: str) -> PortfolioSnapshot:
        raise NotImplementedError

    def get_circuit_breaker_state(self) -> CircuitBreakerState:
        return CircuitBreakerState()


class StaticExecutionContextProvider(ExecutionContextProvider):
    def __init__(self, snapshots: dict[str, PortfolioSnapshot], breaker: CircuitBreakerState | None = None) -> None:
        self._snapshots = snapshots
        self._breaker = breaker or CircuitBreakerState()

    def get_portfolio_snapshot(self, snapshot_id: str) -> PortfolioSnapshot:
        return self._snapshots[snapshot_id]

    def get_circuit_breaker_state(self) -> CircuitBreakerState:
        return self._breaker


class ExecutorWorker:
    def __init__(
        self,
        db: PipelineDB,
        config: PipelineConfig,
        context_provider: ExecutionContextProvider,
        broker: BrokerClient | None = None,
        approval_service: ApprovalService | None = None,
        worker_id: str = "executor-1",
    ) -> None:
        self._db = db
        self._config = config
        self._context_provider = context_provider
        self._broker = broker or PaperBrokerClient()
        self._approval_service = approval_service or ApprovalService(db)
        self._risk_engine = RiskEngine(config.executor)
        self._worker_id = worker_id

    def process_next(self) -> bool:
        lease = self._db.lease_next_job(StageName.EXECUTION, self._worker_id, self._config.queue.lease_sec)
        if lease is None:
            return False

        started = time.perf_counter()
        try:
            parent = self._db.get_message(lease.message_id)
            report = AnalysisReport.model_validate(parent.payload)
            if not report.candidates:
                decision = ExecutionDecision(status=ExecutionStatus.REJECTED_RISK, stage_summary="No candidates available for execution.")
                envelope = PipelineEnvelope.new(
                    trace_id=parent.trace_id,
                    source_alert_id=parent.source_alert_id,
                    parent_message_id=parent.message_id,
                    stage=StageName.EXECUTION,
                    schema_version=parent.schema_version,
                    idempotency_key=build_execution_key(parent.message_id, self._config.executor.risk_ruleset_version, self._config.executor.approval_mode),
                    payload=decision.model_dump(mode="json"),
                )
                audit = StageAudit(message_id=envelope.message_id, latency_ms=int((time.perf_counter() - started) * 1000))
                self._db.complete_job_success(lease, output_envelope=envelope, audit=audit, trace_status=decision.status.value)
                return True

            candidate = sorted(report.candidates, key=lambda item: item.score, reverse=True)[0]
            portfolio = self._context_provider.get_portfolio_snapshot(str(report.portfolio_snapshot_id))
            breaker = self._context_provider.get_circuit_breaker_state()
            checks = self._risk_engine.evaluate(candidate, portfolio, breaker)
            if not all(item.passed for item in checks):
                decision = ExecutionDecision(
                    status=ExecutionStatus.REJECTED_RISK,
                    selected_candidate_id=candidate.candidate_id,
                    risk_checks=checks,
                    stage_summary="Execution rejected by deterministic risk checks.",
                )
                envelope = PipelineEnvelope.new(
                    trace_id=parent.trace_id,
                    source_alert_id=parent.source_alert_id,
                    parent_message_id=parent.message_id,
                    stage=StageName.EXECUTION,
                    schema_version=parent.schema_version,
                    idempotency_key=build_execution_key(parent.message_id, self._config.executor.risk_ruleset_version, self._config.executor.approval_mode),
                    payload=decision.model_dump(mode="json"),
                )
                audit = StageAudit(message_id=envelope.message_id, latency_ms=int((time.perf_counter() - started) * 1000))
                self._db.complete_job_success(lease, output_envelope=envelope, audit=audit, trace_status=decision.status.value)
                return True

            limit_price = Decimal(str(candidate.market_snapshot["last_price"]))
            intent = TradeIntent(
                candidate_id=candidate.candidate_id,
                symbol=candidate.symbol,
                side=candidate.side,
                order_type=OrderType.LIMIT,
                tif=TimeInForce.DAY,
                max_notional=candidate.max_notional,
                limit_price=limit_price,
                stop_loss_pct=candidate.stop_loss_pct,
                max_slippage_bps=int(self._config.analyst.max_spread_bps),
                thesis_horizon=candidate.thesis_horizon,
            )

            if self._config.executor.approval_mode == "manual_all":
                preview_envelope = PipelineEnvelope.new(
                    trace_id=parent.trace_id,
                    source_alert_id=parent.source_alert_id,
                    parent_message_id=parent.message_id,
                    stage=StageName.EXECUTION,
                    schema_version=parent.schema_version,
                    idempotency_key=build_execution_key(parent.message_id, self._config.executor.risk_ruleset_version, self._config.executor.approval_mode),
                    payload={},
                )
                approval_id, _, _ = self._approval_service.request(
                    trace_id=parent.trace_id,
                    message_id=preview_envelope.message_id,
                    candidate_id=candidate.candidate_id,
                    channel="manual",
                    timeout_minutes=self._config.executor.approval_timeout_minutes,
                )
                decision = ExecutionDecision(
                    status=ExecutionStatus.AWAITING_APPROVAL,
                    selected_candidate_id=candidate.candidate_id,
                    intent=intent,
                    risk_checks=checks,
                    approval_id=approval_id,
                    stage_summary="Execution passed risk checks and is awaiting manual approval.",
                )
                envelope = preview_envelope.model_copy(update={"payload": decision.model_dump(mode="json")})
                audit = StageAudit(message_id=envelope.message_id, latency_ms=int((time.perf_counter() - started) * 1000))
                self._db.complete_job_success(
                    lease,
                    output_envelope=envelope,
                    audit=audit,
                    next_stage=StageName.APPROVAL,
                    trace_status=decision.status.value,
                )
                return True

            return self._submit_order(lease, parent, intent, candidate.candidate_id, checks, started)
        except Exception as exc:
            self._db.complete_job_retry(
                lease,
                error=str(exc),
                delay_sec=self._config.queue.retry_backoff_sec,
                max_attempts=self._config.queue.max_attempts,
            )
            return False

    def _submit_order(
        self,
        lease,
        parent: PipelineEnvelope,
        intent: TradeIntent,
        candidate_id,
        checks,
        started: float,
        *,
        stage: StageName = StageName.EXECUTION,
    ) -> bool:
        client_order_id = build_stable_order_id(parent.trace_id, candidate_id, self._config.executor.risk_ruleset_version)
        existing_order = self._db.get_broker_order_by_client_order_id(client_order_id)
        if existing_order is None:
            order = self._broker.place_order(intent, client_order_id=client_order_id)
            self._db.save_broker_order(
                trace_id=parent.trace_id,
                candidate_id=candidate_id,
                client_order_id=client_order_id,
                broker=self._broker.broker_name,
                broker_order_id=order.broker_order_id,
                symbol=intent.symbol,
                side=intent.side.value,
                qty=None,
                notional=intent.max_notional,
                order_type=intent.order_type.value,
                tif=intent.tif.value,
                status=order.status,
                avg_fill_price=order.avg_fill_price,
                stop_order_id=None,
                raw_response=order.raw_response,
            )
        else:
            class ExistingOrder:
                broker_order_id = str(existing_order["broker_order_id"] or existing_order["client_order_id"])
                status = str(existing_order["status"])
                avg_fill_price = Decimal(str(existing_order["avg_fill_price"])) if existing_order["avg_fill_price"] is not None else None
            order = ExistingOrder()
        decision = ExecutionDecision(
            status=ExecutionStatus.SUBMITTED,
            selected_candidate_id=candidate_id,
            intent=intent,
            risk_checks=checks,
            broker_order_ids=[order.broker_order_id],
            stage_summary="Order submitted to broker.",
        )
        envelope = PipelineEnvelope.new(
            trace_id=parent.trace_id,
            source_alert_id=parent.source_alert_id,
            parent_message_id=parent.message_id,
            stage=stage,
            schema_version=parent.schema_version,
            idempotency_key=build_execution_key(parent.message_id, self._config.executor.risk_ruleset_version, self._config.executor.approval_mode),
            payload=decision.model_dump(mode="json"),
        )
        audit = StageAudit(message_id=envelope.message_id, latency_ms=int((time.perf_counter() - started) * 1000))
        self._db.complete_job_success(
            lease,
            output_envelope=envelope,
            audit=audit,
            next_stage=StageName.OUTCOME,
            trace_status=decision.status.value,
        )
        if not self._db.list_outcome_observations(parent.trace_id):
            for observation in schedule_default_outcomes(parent.trace_id, intent.symbol, intent.limit_price):
                self._db.save_outcome_observation(observation)
        return True

    def run_forever(self, poll_interval_sec: float = 1.0) -> None:
        while True:
            processed = self.process_next()
            if not processed:
                time.sleep(poll_interval_sec)


class ApprovalWorker:
    def __init__(
        self,
        db: PipelineDB,
        config: PipelineConfig,
        broker: BrokerClient | None = None,
        worker_id: str = "approval-1",
    ) -> None:
        self._db = db
        self._config = config
        self._broker = broker or PaperBrokerClient()
        self._worker_id = worker_id

    def process_next(self) -> bool:
        lease = self._db.lease_next_job(StageName.APPROVAL, self._worker_id, self._config.queue.lease_sec)
        if lease is None:
            return False
        try:
            parent = self._db.get_message(lease.message_id)
            request = self._db.get_approval_request_by_message(parent.message_id)
            if request is None:
                self._db.dead_letter_job(lease, reason="approval request not found")
                return False
            status = str(request["status"])
            expires_at = datetime.fromisoformat(str(request["expires_at"]))
            if status == "requested" and expires_at <= datetime.now(tz=timezone.utc):
                self._db.decide_approval(str(request["callback_token"]), status="expired", approver_id="system", reason="approval timed out")
                status = "expired"
            if status == "requested":
                delay = max(1, int((expires_at - datetime.now(tz=timezone.utc)).total_seconds()))
                self._db.complete_job_retry(
                    lease,
                    error="approval_pending",
                    delay_sec=min(delay, self._config.executor.approval_timeout_minutes * 60),
                    max_attempts=10_000,
                )
                return False
            if status != "approved":
                decision = ExecutionDecision(
                    status=ExecutionStatus.EXPIRED if status == "expired" else ExecutionStatus.CANCELLED,
                    selected_candidate_id=parent.payload.get("selected_candidate_id"),
                    intent=TradeIntent.model_validate(parent.payload["intent"]) if parent.payload.get("intent") else None,
                    risk_checks=[],
                    approval_id=None,
                    stage_summary=f"Approval stage ended with status={status}.",
                )
                envelope = PipelineEnvelope.new(
                    trace_id=parent.trace_id,
                    source_alert_id=parent.source_alert_id,
                    parent_message_id=parent.message_id,
                    stage=StageName.APPROVAL,
                    schema_version=parent.schema_version,
                    idempotency_key=f"approval:{parent.message_id}:{status}",
                    payload=decision.model_dump(mode="json"),
                )
                audit = StageAudit(message_id=envelope.message_id)
                self._db.complete_job_success(lease, output_envelope=envelope, audit=audit, trace_status=decision.status.value)
                return True

            intent = TradeIntent.model_validate(parent.payload["intent"])
            return ExecutorWorker(
                self._db,
                self._config,
                context_provider=StaticExecutionContextProvider({}),
                broker=self._broker,
            )._submit_order(lease, parent, intent, intent.candidate_id, [], time.perf_counter(), stage=StageName.APPROVAL)
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
