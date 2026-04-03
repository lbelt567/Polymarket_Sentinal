from __future__ import annotations

import asyncio

from sentinel.config import GatekeeperConfig
from sentinel.ingestion.gatekeeper import Gatekeeper
from sentinel.storage.sqlite_store import SQLiteStore
from sentinel.utils.metrics import Metrics


async def _make_gatekeeper() -> Gatekeeper:
    store = SQLiteStore(":memory:")
    await store.initialize()
    return Gatekeeper(
        GatekeeperConfig(
            gamma_api_url="https://gamma-api.polymarket.com",
            endpoint="/events",
            poll_interval_sec=300,
            min_volume_24h=10_000,
            min_liquidity=5_000,
            max_markets=200,
            excluded_categories=["Sports"],
            excluded_tag_slugs=["sports"],
            stale_timeout_sec=1800,
            page_size=100,
        ),
        store,
        asyncio.Queue(),
        Metrics(),
        time_fn=lambda: 0,
    )


def test_extract_tracked_markets_filters_and_builds_siblings() -> None:
    async def runner() -> None:
        gatekeeper = await _make_gatekeeper()
        events = [
            {
                "id": "evt-1",
                "title": "Fed Meeting",
                "slug": "fed-meeting",
                "markets": [
                    {
                        "id": "mkt-1",
                        "condition_id": "cond-1",
                        "slug": "fed-cut",
                        "question": "Will the Fed cut?",
                        "description": "Fed description",
                        "category": "Economics",
                        "tags": ["fed"],
                        "outcomes": ["Yes", "No"],
                        "clob_token_ids": ["yes-1", "no-1"],
                        "volume24hr": 25_000,
                        "liquidity": 15_000,
                        "active": True,
                        "closed": False,
                    },
                    {
                        "id": "mkt-2",
                        "condition_id": "cond-2",
                        "slug": "fed-hold",
                        "question": "Will the Fed hold?",
                        "description": "Hold description",
                        "category": "Economics",
                        "tags": ["fed", "rates"],
                        "outcomes": ["Yes", "No"],
                        "clob_token_ids": ["yes-2", "no-2"],
                        "volume24hr": 20_000,
                        "liquidity": 12_000,
                        "active": True,
                        "closed": False,
                    },
                ],
            },
            {
                "id": "evt-2",
                "title": "Sports Event",
                "slug": "sports",
                "markets": [
                    {
                        "id": "sports-1",
                        "condition_id": "sports-cond",
                        "slug": "team-win",
                        "question": "Will the team win?",
                        "description": "Sports",
                        "category": "",
                        "tags": ["sports", "soccer"],
                        "outcomes": ["Yes", "No"],
                        "clob_token_ids": ["yes-s", "no-s"],
                        "volume24hr": 50_000,
                        "liquidity": 50_000,
                        "active": True,
                        "closed": False,
                    }
                ],
            },
            {
                "id": "evt-3",
                "title": "Ambiguous Event",
                "slug": "ambiguous",
                "markets": [
                    {
                        "id": "amb-1",
                        "condition_id": "amb-cond",
                        "slug": "ambiguous-market",
                        "question": "Ambiguous?",
                        "description": "Ambiguous",
                        "category": "Economics",
                        "outcomes": ["Yes", "No", "Maybe"],
                        "clob_token_ids": ["only-one"],
                        "volume24hr": 50_000,
                        "liquidity": 50_000,
                        "active": True,
                        "closed": False,
                    }
                ],
            },
        ]

        tracked = gatekeeper.extract_tracked_markets(events)

        assert set(tracked) == {"yes-1", "yes-2"}
        assert tracked["yes-1"].sibling_asset_ids == ["yes-2"]
        assert tracked["yes-1"].sibling_market_slugs == ["fed-hold"]
        assert tracked["yes-1"].event_slug == "fed-meeting"
        assert tracked["yes-2"].sibling_asset_ids == ["yes-1"]

    asyncio.run(runner())
