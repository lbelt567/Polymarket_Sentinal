from __future__ import annotations

import asyncio
import hashlib
import hmac
from urllib import error

from sentinel.app import SentinelApp
from sentinel.config import load_config
from sentinel.notifications.base import AgentAlertPolicy, NotificationDispatcher
from sentinel.notifications.json_webhook import HttpJsonWebhookNotifier
from sentinel.processing.alerts import AlertLevel, SignalQuality, ShiftAlert, ShiftSummary


class FailingNotifier:
    async def send(self, alert) -> None:
        raise RuntimeError("boom")


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, alert) -> None:
        self.calls += 1


def _alert() -> ShiftAlert:
    return ShiftAlert(
        alert_id="abc123",
        alert_level="trend",
        alert_level_label="Decisive Shift",
        timestamp_iso="2026-04-01T14:32:00+00:00",
        timestamp_ms=3000,
        market={
            "asset_id": "asset-1",
            "market_slug": "market-slug",
            "question": "Question?",
            "category": "Economics",
        },
        shift=ShiftSummary(
            direction="up",
            delta_pct=10.0,
            signed_delta_pct=10.0,
            price_start=0.50,
            price_end=0.55,
            price_current=0.55,
            window_sec=300,
            ticks_in_window=12,
        ),
        signal_quality=SignalQuality(
            signal_source="midpoint_book",
            best_bid=0.54,
            best_ask=0.56,
            spread=0.02,
            quote_age_sec=1.0,
            liquidity_bucket="high",
            confidence="strong",
        ),
    )


def test_dispatcher_isolates_notifier_failures() -> None:
    async def runner() -> None:
        recorder = RecordingNotifier()
        dispatcher = NotificationDispatcher(
            [FailingNotifier(), recorder],
            min_severity=AlertLevel.SCOUT,
            policy=AgentAlertPolicy(
                allowed_levels={AlertLevel.CONFIRMATION, AlertLevel.TREND},
                excluded_confidences={"weak"},
                excluded_signal_sources={"last_trade"},
                min_price=0.02,
                max_price=0.98,
                min_liquidity=0.0,
                min_abs_move=0.0,
                event_dedup_sec=900,
            ),
        )
        await dispatcher.dispatch(_alert())
        assert recorder.calls == 1

    asyncio.run(runner())


def test_dispatcher_deduplicates_by_event_and_filters_noise() -> None:
    recorder = RecordingNotifier()
    dispatcher = NotificationDispatcher(
        [recorder],
        min_severity=AlertLevel.SCOUT,
        policy=AgentAlertPolicy(
            allowed_levels={AlertLevel.CONFIRMATION, AlertLevel.TREND},
            excluded_confidences={"weak"},
            excluded_signal_sources={"last_trade"},
            min_price=0.02,
            max_price=0.98,
            min_liquidity=25_000.0,
            min_abs_move=0.02,
            event_dedup_sec=900,
        ),
    )
    weak = _alert()
    weak.alert_id = "weak"
    weak.signal_quality.confidence = "weak"
    weak.market["liquidity"] = 100_000.0
    moderate_trend = _alert()
    moderate_trend.alert_id = "moderate-trend"
    moderate_trend.signal_quality.confidence = "moderate"
    moderate_trend.shift.delta_pct = 8.0
    moderate_trend.shift.price_end = 0.505
    moderate_trend.shift.price_current = 0.505
    moderate_trend.market["liquidity"] = 100_000.0
    strong_confirmation = _alert()
    strong_confirmation.alert_id = "strong-confirmation"
    strong_confirmation.alert_level = "confirmation"
    strong_confirmation.signal_quality.confidence = "strong"
    strong_confirmation.shift.delta_pct = 9.0
    strong_confirmation.shift.price_end = 0.53
    strong_confirmation.shift.price_current = 0.53
    strong_confirmation.market["liquidity"] = 100_000.0
    low_liquidity = _alert()
    low_liquidity.alert_id = "low-liquidity"
    low_liquidity.signal_quality.confidence = "strong"
    low_liquidity.market["liquidity"] = 10_000.0
    strong_confirmation.market["event_slug"] = "same-event"
    moderate_trend.market["event_slug"] = "same-event"
    weak.market["event_slug"] = "same-event"
    low_liquidity.market["event_slug"] = "other-event"

    selected = dispatcher.select_for_dispatch([weak, moderate_trend, strong_confirmation, low_liquidity])

    assert [alert.alert_id for alert in selected] == ["strong-confirmation"]


def test_dispatcher_filters_small_absolute_moves() -> None:
    dispatcher = NotificationDispatcher(
        [],
        min_severity=AlertLevel.SCOUT,
        policy=AgentAlertPolicy(
            allowed_levels={AlertLevel.CONFIRMATION, AlertLevel.TREND},
            excluded_confidences={"weak"},
            excluded_signal_sources={"last_trade"},
            min_price=0.10,
            max_price=0.98,
            min_liquidity=25_000.0,
            min_abs_move=0.02,
            event_dedup_sec=900,
        ),
    )
    too_small = _alert()
    too_small.market["liquidity"] = 100_000.0
    too_small.shift.price_start = 0.20
    too_small.shift.price_end = 0.215
    too_small.shift.price_current = 0.215

    assert dispatcher.select_for_dispatch([too_small]) == []


def test_app_builds_configured_notifiers() -> None:
    app = SentinelApp(load_config())
    notifiers = app._build_notifiers()
    assert len(notifiers) == 1
    assert type(notifiers[0]).__name__ == "JsonFileNotifier"


def test_webhook_notifier_builds_auth_headers() -> None:
    alert = _alert()
    notifier = HttpJsonWebhookNotifier(
        "https://example.com/agent",
        bearer_token="token-123",
        hmac_secret="secret",
    )
    body = alert.to_json().encode("utf-8")

    headers = notifier._build_headers(alert, body)

    assert headers["Authorization"] == "Bearer token-123"
    assert headers["X-Sentinel-Alert-Id"] == alert.alert_id
    assert "X-Sentinel-Event-Slug" not in headers
    expected = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert headers["X-Sentinel-Signature"] == f"sha256={expected}"


def test_webhook_notifier_retries_transient_failures() -> None:
    async def runner() -> None:
        alert = _alert()
        attempts = 0
        notifier = HttpJsonWebhookNotifier(
            "https://example.com/agent",
            max_retries=2,
            retry_backoff_sec=0.0,
        )

        def fake_post(data: bytes, headers: dict[str, str]) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise error.URLError("down")

        notifier._post_json = fake_post  # type: ignore[method-assign]
        await notifier.send(alert)
        assert attempts == 3

    asyncio.run(runner())
