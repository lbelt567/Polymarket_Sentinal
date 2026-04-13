from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from analyst.agent import build_stage_summary, score_candidate
from analyst.filters import evaluate_filters
from analyst.tools.market_data import MarketDataProvider
from analyst.tools.technicals import realized_volatility, rsi, sma
from contracts.analysis import AnalysisReport, AnalysisStatus, ThesisHorizon, TradeCandidate, TradeSide
from contracts.common import PipelineEnvelope, PortfolioSnapshot, StageAudit, StageName
from contracts.research import Confidence, Horizon, ResearchReport
from pipeline.config import PipelineConfig
from pipeline.db import PipelineDB
from pipeline.idempotency import build_analysis_key


class PortfolioProvider:
    def get_snapshot(self) -> PortfolioSnapshot:
        raise NotImplementedError


class StaticPortfolioProvider(PortfolioProvider):
    def __init__(self, snapshot: PortfolioSnapshot) -> None:
        self._snapshot = snapshot

    def get_snapshot(self) -> PortfolioSnapshot:
        return self._snapshot


class AnalystWorker:
    def __init__(
        self,
        db: PipelineDB,
        config: PipelineConfig,
        market_data: MarketDataProvider,
        portfolio_provider: PortfolioProvider,
        worker_id: str = "analyst-1",
    ) -> None:
        self._db = db
        self._config = config
        self._market_data = market_data
        self._portfolio_provider = portfolio_provider
        self._worker_id = worker_id

    def process_next(self) -> bool:
        lease = self._db.lease_next_job(StageName.ANALYSIS, self._worker_id, self._config.queue.lease_sec)
        if lease is None:
            return False

        started = time.perf_counter()
        try:
            parent = self._db.get_message(lease.message_id)
            report = ResearchReport.model_validate(parent.payload)
            portfolio_snapshot = self._portfolio_provider.get_snapshot()
            snapshot_id = self._db.save_portfolio_snapshot(portfolio_snapshot)

            candidates: list[TradeCandidate] = []
            rejected: list[TradeCandidate] = []
            analysis_ts = portfolio_snapshot.captured_at.isoformat()
            for hypothesis in report.asset_hypotheses:
                snapshot = self._market_data.get_snapshot(hypothesis.symbol)
                filters = evaluate_filters(snapshot, self._config.analyst)
                history = snapshot.history or [snapshot.last_price]
                features = {
                    "last_price": str(snapshot.last_price),
                    "spread_bps": str(snapshot.spread_bps),
                    "rsi_14": str(rsi(history) or ""),
                    "sma_20": str(sma(history, 20) or ""),
                    "sma_50": str(sma(history, 50) or ""),
                    "volatility": str(realized_volatility(history) or ""),
                    "avg_daily_dollar_volume": str(snapshot.avg_daily_dollar_volume),
                    "sector": snapshot.sector,
                    "session": snapshot.session,
                    "quote_age_sec": snapshot.quote_age_sec,
                }
                passed = [item for item in filters if item.passed]
                failed = [item for item in filters if not item.passed]
                confidence_weight = {
                    Confidence.HIGH: 1.5,
                    Confidence.MEDIUM: 1.0,
                    Confidence.LOW: 0.5,
                }[hypothesis.confidence]
                candidate = TradeCandidate(
                    symbol=hypothesis.symbol,
                    side=TradeSide.BUY,
                    thesis_horizon=ThesisHorizon.INTRADAY if hypothesis.horizon == Horizon.INTRADAY else ThesisHorizon.SWING,
                    broker_eligible=snapshot.tradable,
                    market_snapshot={
                        "last_price": str(snapshot.last_price),
                        "bid": str(snapshot.bid) if snapshot.bid is not None else None,
                        "ask": str(snapshot.ask) if snapshot.ask is not None else None,
                        "session": snapshot.session,
                        "quote_age_sec": snapshot.quote_age_sec,
                        "sector": snapshot.sector,
                    },
                    features=features,
                    filters=filters,
                    score=score_candidate(len(passed), len(failed), confidence_weight),
                    max_notional=min(portfolio_snapshot.equity * Decimal("0.05"), portfolio_snapshot.cash * Decimal("0.95")),
                    stop_loss_pct=Decimal("0.03"),
                    expiry_at=datetime.now(tz=timezone.utc) + timedelta(minutes=15),
                )
                if all(item.passed for item in filters):
                    candidates.append(candidate)
                else:
                    rejected.append(candidate)

            status = AnalysisStatus.PASS if candidates else AnalysisStatus.REJECT
            output = AnalysisReport(
                status=status,
                candidates=sorted(candidates, key=lambda item: item.score, reverse=True),
                rejected_candidates=sorted(rejected, key=lambda item: item.score, reverse=True),
                portfolio_snapshot_id=snapshot_id,
                stage_summary=build_stage_summary(len(candidates), len(rejected)),
            )
            envelope = PipelineEnvelope.new(
                trace_id=parent.trace_id,
                source_alert_id=parent.source_alert_id,
                parent_message_id=parent.message_id,
                stage=StageName.ANALYSIS,
                schema_version=parent.schema_version,
                idempotency_key=build_analysis_key(parent.message_id, self._config.analyst.ruleset_version, analysis_ts),
                payload=output.model_dump(mode="json"),
            )
            audit = StageAudit(
                message_id=envelope.message_id,
                prompt_version=self._config.analyst.ruleset_version,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            next_stage = StageName.EXECUTION if output.status == AnalysisStatus.PASS else None
            message_id = self._db.complete_job_success(
                lease,
                output_envelope=envelope,
                audit=audit,
                next_stage=next_stage,
                trace_status=output.status.value,
            )
            if message_id is not None:
                self._db.save_analysis_candidates(message_id, candidates=output.candidates, rejected_candidates=output.rejected_candidates)
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
