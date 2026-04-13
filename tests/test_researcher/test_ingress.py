from __future__ import annotations

import hashlib
import hmac
import json

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


def _config(database_url: str) -> PipelineConfig:
    return PipelineConfig(
        app=AppSection(env="test", log_level="INFO"),
        database=DatabaseSection(url=database_url, pool_size=1),
        queue=QueueSection(lease_sec=60, max_attempts=3, retry_backoff_sec=1),
        researcher=ResearcherSection(
            http_host="127.0.0.1",
            http_port=8001,
            webhook_bearer_token="token",
            webhook_hmac_secret="secret",
            prompt_version="v1",
            source_freshness_sec=3600,
        ),
        analyst=AnalystSection(),
        executor=ExecutorSection(),
        broker=BrokerSection(),
        notifications=NotificationsSection(),
    )


def _body() -> bytes:
    return json.dumps(
        {
            "alert_id": "alert-abc",
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
        }
    ).encode("utf-8")


def _headers(body: bytes) -> dict[str, str]:
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    return {
        "authorization": "Bearer token",
        "x-sentinel-alert-id": "alert-abc",
        "x-sentinel-signature": f"sha256={digest}",
    }


def test_ingress_validates_signature_and_deduplicates(tmp_path) -> None:
    config = _config(f"sqlite:///{tmp_path / 'pipeline.db'}")
    db = PipelineDB(config.database.url)
    db.initialize()
    service = ResearcherIngressService(db, config)
    body = _body()
    headers = _headers(body)

    first = service.handle_alert(body, headers)
    second = service.handle_alert(body, headers)
    bad = service.handle_alert(body, {**headers, "x-sentinel-signature": "sha256=bad"})

    assert first.status_code == 202
    assert first.body["duplicate"] is False
    assert second.status_code == 202
    assert second.body["duplicate"] is True
    assert bad.status_code == 401
