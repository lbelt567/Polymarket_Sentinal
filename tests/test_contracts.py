from __future__ import annotations

from contracts.common import PipelineEnvelope, StageName
from contracts.research import IngestedAlert
from contracts.scout import ShiftAlertMirror


def _sample_alert() -> dict[str, object]:
    return {
        "alert_id": "alert-123",
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
                    "symbol": "XLF",
                    "instrument_type": "etf",
                    "direction": "bullish",
                    "horizon": "intraday",
                    "linkage_type": "macro_proxy",
                    "confidence": "medium",
                    "tradable_universe": True,
                    "rationale_summary": "Rates-sensitive financials proxy.",
                }
            ]
        },
    }


def test_shift_alert_contract_round_trip() -> None:
    alert = ShiftAlertMirror.model_validate(_sample_alert())
    ingested = IngestedAlert(shift_alert=alert, signature_verified=True)
    envelope = PipelineEnvelope.new(
        trace_id="00000000-0000-0000-0000-000000000001",
        source_alert_id=alert.alert_id,
        stage=StageName.INGEST,
        schema_version="v1",
        idempotency_key="scout:alert-123",
        payload=ingested.model_dump(mode="json"),
    )

    assert envelope.payload["shift_alert"]["alert_id"] == "alert-123"
    assert envelope.stage == StageName.INGEST
