from __future__ import annotations

import json

from sentinel.config import WebsocketConfig
from sentinel.ingestion.ws_client import MarketWSClient


def test_wide_spread_book_is_filtered() -> None:
    config = WebsocketConfig(
        url="wss://example.com",
        heartbeat_interval_sec=10,
        reconnect_base_delay_sec=1,
        reconnect_max_delay_sec=60,
        custom_feature_enabled=True,
        max_spread_for_midpoint=0.10,
        max_quote_age_sec=15,
    )
    raw_message = json.dumps(
        {
            "event_type": "best_bid_ask",
            "asset_id": "asset-1",
            "best_bid": "0.10",
            "best_ask": "0.30",
            "spread": "0.20",
            "timestamp": "1000",
        }
    )

    ticks, resolved = MarketWSClient.parse_message(raw_message, config)

    assert ticks == []
    assert resolved == []


def test_price_change_generates_midpoint_tick() -> None:
    config = WebsocketConfig(
        url="wss://example.com",
        heartbeat_interval_sec=10,
        reconnect_base_delay_sec=1,
        reconnect_max_delay_sec=60,
        custom_feature_enabled=True,
        max_spread_for_midpoint=0.10,
        max_quote_age_sec=15,
    )
    raw_message = json.dumps(
        {
            "event_type": "price_change",
            "timestamp": "2000",
            "price_changes": [
                {"asset_id": "asset-1", "best_bid": "0.45", "best_ask": "0.50"},
            ],
        }
    )

    ticks, _ = MarketWSClient.parse_message(raw_message, config)

    assert len(ticks) == 1
    assert ticks[0].price == 0.475
    assert ticks[0].quote_ts_ms == 2000
