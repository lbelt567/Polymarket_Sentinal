from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from uuid import UUID

from analyst.tools.market_data import MarketDataSnapshot, StaticMarketDataProvider
from analyst.worker import AnalystWorker, StaticPortfolioProvider
from contracts.common import PortfolioSnapshot
from contracts.common import StageName
from executor.broker import PaperBrokerClient
from executor.worker import ExecutorWorker, StaticExecutionContextProvider
from pipeline.config import (
    AnalystSection,
    AppSection,
    BrokerSection,
    DatabaseSection,
    ExecutorSection,
    NotificationsSection,
    PipelineConfig,
    QueueSection,
    ResearcherSection,
)
from pipeline.db import PipelineDB
from researcher.server import ResearcherIngressService
from researcher.worker import ResearcherWorker


def _config(database_url: str) -> PipelineConfig:
    return PipelineConfig(
        app=AppSection(env="test", log_level="INFO"),
        database=DatabaseSection(url=database_url, pool_size=1),
        queue=QueueSection(lease_sec=60, max_attempts=3, retry_backoff_sec=1),
        researcher=ResearcherSection(
            http_host="127.0.0.1",
            http_port=8001,
            webhook_bearer_token="",
            webhook_hmac_secret="secret",
            prompt_version="v1",
            source_freshness_sec=3600,
        ),
        analyst=AnalystSection(min_avg_dollar_volume=1_000_000, max_spread_bps=50, min_price=5.0, max_price=500.0),
        executor=ExecutorSection(approval_mode="paper_auto_only"),
        broker=BrokerSection(provider="paper", paper=True),
        notifications=NotificationsSection(),
    )


def _alert_body() -> bytes:
    return json.dumps(
        {
            "alert_id": "alert-flow",
            "alert_level": "confirmation",
            "alert_level_label": "Confirmed Shift",
            "timestamp_iso": "2026-04-06T12:00:00+00:00",
            "timestamp_ms": 1775476800000,
            "market": {
                "asset_id": "asset-1",
                "market_slug": "fed-cut-june-2026",
                "event_slug": "fed-june-2026",
                "question": "Will the Fed cut rates in June 2026?",
                "category": "Economics",
                "polymarket_url": "https://polymarket.com/event/fed-june-2026",
            },
            "shift": {
                "direction": "up",
                "delta_pct": 7.2,
                "signed_delta_pct": 7.2,
                "price_start": 0.45,
                "price_end": 0.522,
                "price_current": 0.522,
                "window_sec": 180,
                "ticks_in_window": 47,
            },
            "signal_quality": {
                "signal_source": "midpoint_book",
                "best_bid": 0.52,
                "best_ask": 0.524,
                "spread": 0.004,
                "quote_age_sec": 1.2,
                "liquidity_bucket": "high",
                "confidence": "strong",
            },
            "metadata": {
                "asset_hypotheses": [
                    {
                        "symbol": "AAPL",
                        "instrument_type": "equity",
                        "direction": "bullish",
                        "horizon": "intraday",
                        "linkage_type": "macro_proxy",
                        "confidence": "high",
                        "tradable_universe": True,
                        "rationale_summary": "Large-cap rates-sensitive proxy for test flow.",
                    }
                ]
            },
        }
    ).encode("utf-8")


def test_end_to_end_pipeline_to_execution_and_outcome_scheduling(tmp_path) -> None:
    config = _config(f"sqlite:///{tmp_path / 'pipeline.db'}")
    db = PipelineDB(config.database.url)
    db.initialize()

    body = _alert_body()
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    ingress = ResearcherIngressService(db, config)
    response = ingress.handle_alert(body, {"x-sentinel-signature": f"sha256={digest}"})
    assert response.status_code == 202

    assert ResearcherWorker(db, config).process_next() is True

    portfolio = PortfolioSnapshot(
        account_id="paper",
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        buying_power=Decimal("100000"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        sector_exposure={"technology": Decimal("0")},
    )
    market_data = StaticMarketDataProvider(
        {
            "AAPL": MarketDataSnapshot(
                symbol="AAPL",
                last_price=Decimal("175"),
                bid=Decimal("174.95"),
                ask=Decimal("175.05"),
                avg_daily_dollar_volume=Decimal("500000000"),
                session="regular",
                tradable=True,
                halted=False,
                quote_age_sec=1,
                sector="technology",
                history=[Decimal("170"), Decimal("171"), Decimal("172"), Decimal("173"), Decimal("174"), Decimal("175")] * 10,
            )
        }
    )
    analyst = AnalystWorker(db, config, market_data=market_data, portfolio_provider=StaticPortfolioProvider(portfolio))
    assert analyst.process_next() is True

    context_provider = StaticExecutionContextProvider({str(portfolio.snapshot_id): portfolio})
    executor = ExecutorWorker(db, config, context_provider=context_provider, broker=PaperBrokerClient())
    assert executor.process_next() is True

    observations = db.list_outcome_observations(trace_id=UUID(str(response.body["trace_id"])))
    outcome_lease = db.lease_next_job(stage=StageName.OUTCOME, worker_id="outcome-test", lease_sec=60)

    assert len(observations) == 3
    assert outcome_lease is not None
