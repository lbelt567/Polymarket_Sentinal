from __future__ import annotations

import json

from sentinel.config import WebsocketConfig
from sentinel.ingestion.ws_client import MarketWSClient


CONFIG = WebsocketConfig(
    url="wss://example.com",
    heartbeat_interval_sec=10,
    reconnect_base_delay_sec=1,
    reconnect_max_delay_sec=60,
    custom_feature_enabled=True,
    max_spread_for_midpoint=0.10,
    max_quote_age_sec=15,
)


def test_parse_market_channel_messages() -> None:
    raw_message = json.dumps(
        [
            {
                "event_type": "book",
                "asset_id": "asset-book",
                "bids": [{"price": ".48", "size": "30"}],
                "asks": [{"price": ".52", "size": "25"}],
                "timestamp": "123456789000",
            },
            {
                "event_type": "price_change",
                "timestamp": "1757908892351",
                "price_changes": [
                    {
                        "asset_id": "asset-price-change",
                        "best_bid": "0.5",
                        "best_ask": "0.6",
                    }
                ],
            },
            {
                "event_type": "last_trade_price",
                "asset_id": "asset-last-trade",
                "price": "0.456",
                "timestamp": "1750428146322",
            },
            {
                "event_type": "market_resolved",
                "market": "market-1",
                "winning_asset_id": "asset-book",
                "winning_outcome": "Yes",
                "assets_ids": ["asset-book", "asset-other"],
                "timestamp": "1766790415550",
            },
        ]
    )

    ticks, resolved = MarketWSClient.parse_message(raw_message, CONFIG)

    assert [tick.asset_id for tick in ticks] == ["asset-book", "asset-price-change", "asset-last-trade"]
    assert ticks[0].price == 0.5
    assert ticks[0].signal_source == "midpoint_book"
    assert ticks[1].signal_source == "midpoint_change"
    assert ticks[2].signal_source == "last_trade"
    assert resolved[0].market_id == "market-1"
    assert resolved[0].asset_ids == ["asset-book", "asset-other"]
