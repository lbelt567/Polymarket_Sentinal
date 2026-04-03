from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from sentinel.config import GatekeeperConfig
from sentinel.ingestion.models import MarketMetadata, SubscriptionCommand, ensure_list, extract_yes_token_id, normalize_tags, parse_float
from sentinel.storage.sqlite_store import SQLiteStore
from sentinel.utils.metrics import Metrics


@dataclass(slots=True)
class MarketDiff:
    subscribe_ids: list[str]
    unsubscribe_ids: list[str]
    tracked: dict[str, MarketMetadata]


class Gatekeeper:
    def __init__(
        self,
        config: GatekeeperConfig,
        store: SQLiteStore,
        command_queue: asyncio.Queue[SubscriptionCommand],
        metrics: Metrics,
        time_fn,
    ) -> None:
        self._config = config
        self._store = store
        self._command_queue = command_queue
        self._metrics = metrics
        self._time_fn = time_fn
        self._logger = logging.getLogger(__name__)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
                await asyncio.sleep(self._config.poll_interval_sec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - runtime guard
                self._logger.warning("gatekeeper_error", extra={"extra_data": {"error": str(exc)}})
                await asyncio.sleep(min(self._config.poll_interval_sec, 30))

    async def run_once(self) -> MarketDiff:
        events = await self.fetch_events()
        tracked = self.extract_tracked_markets(events)
        active_asset_ids = self._store.active_asset_ids()
        new_asset_ids = sorted(set(tracked) - active_asset_ids)
        removed_asset_ids = sorted(active_asset_ids - set(tracked))
        now_ms = self._time_fn()

        await self._store.upsert_tracked_markets(list(tracked.values()), now_ms=now_ms)
        await self._store.set_market_active(new_asset_ids, active=True, now_ms=now_ms)
        await self._store.set_market_active(removed_asset_ids, active=False, now_ms=now_ms)

        if new_asset_ids:
            await self._command_queue.put(SubscriptionCommand(operation="subscribe", asset_ids=new_asset_ids))
        if removed_asset_ids:
            await self._command_queue.put(SubscriptionCommand(operation="unsubscribe", asset_ids=removed_asset_ids))

        self._metrics.tracked_markets = len(tracked)
        self._metrics.mark_gatekeeper_run()
        self._logger.info(
            "gatekeeper_run",
            extra={
                "extra_data": {
                    "tracked_markets": len(tracked),
                    "subscribe_count": len(new_asset_ids),
                    "unsubscribe_count": len(removed_asset_ids),
                }
            },
        )
        return MarketDiff(subscribe_ids=new_asset_ids, unsubscribe_ids=removed_asset_ids, tracked=tracked)

    async def fetch_events(self) -> list[dict[str, Any]]:
        try:
            import aiohttp
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("aiohttp is required for live gatekeeper polling") from exc

        url = f"{self._config.gamma_api_url.rstrip('/')}{self._config.endpoint}"
        offset = 0
        events: list[dict[str, Any]] = []
        async with aiohttp.ClientSession() as session:
            while True:
                params = {
                    "active": "true",
                    "closed": "false",
                    "order": self._config.order_by,
                    "ascending": str(self._config.ascending).lower(),
                    "limit": str(self._config.page_size),
                    "offset": str(offset),
                }
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    response.raise_for_status()
                    payload = await response.json()
                batch = payload if isinstance(payload, list) else ensure_list(payload.get("events"))
                if not batch:
                    break
                events.extend(batch)
                if len(batch) < self._config.page_size:
                    break
                offset += self._config.page_size
        return events

    def extract_tracked_markets(self, events: list[dict[str, Any]]) -> dict[str, MarketMetadata]:
        excluded_categories = {category.lower() for category in self._config.excluded_categories}
        excluded_tag_slugs = {tag.lower() for tag in self._config.excluded_tag_slugs}
        candidates: list[MarketMetadata] = []
        for event in events:
            event_id = str(event.get("id", ""))
            event_title = str(event.get("title") or event.get("question") or "")
            event_slug = str(event.get("slug") or "")
            event_end_date = str(event.get("endDate") or event.get("end_date") or "")
            markets = [market for market in ensure_list(event.get("markets")) if isinstance(market, dict)]

            canonical_markets: list[tuple[dict[str, Any], str]] = []
            for market in markets:
                yes_token = extract_yes_token_id(market)
                if yes_token:
                    canonical_markets.append((market, yes_token))
                else:
                    self._logger.warning("skipping_market_without_yes_mapping", extra={"extra_data": {"market": market.get("slug")}})

            for market, yes_token in canonical_markets:
                market_slug = str(market.get("slug") or "")
                question = str(market.get("question") or market.get("title") or "")
                category = str(market.get("category") or event.get("category") or "")
                tags = normalize_tags(market.get("tags") or event.get("tags"))
                volume_24h = parse_float(market.get("volume24hr") or market.get("volume_24h") or event.get("volume24hr"))
                liquidity = parse_float(market.get("liquidity") or event.get("liquidity"))
                active = bool(market.get("active", event.get("active", True)))
                closed = bool(market.get("closed", event.get("closed", False)))
                if not active or closed:
                    continue
                if self._is_excluded_market(category, tags, excluded_categories, excluded_tag_slugs):
                    continue
                if volume_24h < self._config.min_volume_24h or liquidity < self._config.min_liquidity:
                    continue

                sibling_asset_ids = [asset_id for sibling_market, asset_id in canonical_markets if asset_id != yes_token]
                sibling_market_slugs = [
                    str(sibling_market.get("slug") or "")
                    for sibling_market, asset_id in canonical_markets
                    if asset_id != yes_token
                ]

                candidates.append(
                    MarketMetadata(
                        asset_id=yes_token,
                        market_id=str(market.get("id") or market.get("condition_id") or market.get("market") or ""),
                        condition_id=str(market.get("condition_id") or market.get("conditionId") or market.get("market") or ""),
                        market_slug=market_slug,
                        question=question,
                        description=str(market.get("description") or event.get("description") or ""),
                        category=category,
                        tags=tags,
                        event_id=event_id,
                        event_title=event_title,
                        event_slug=event_slug,
                        sibling_asset_ids=sibling_asset_ids,
                        sibling_market_slugs=sibling_market_slugs,
                        outcome="Yes",
                        end_date=str(market.get("endDate") or market.get("end_date") or event_end_date),
                        volume_24h=volume_24h,
                        liquidity=liquidity,
                        polymarket_url=str(market.get("url") or f"https://polymarket.com/event/{event_slug}"),
                    )
                )

        candidates.sort(key=lambda market: market.volume_24h, reverse=True)
        limited = candidates[: self._config.max_markets]
        return {market.asset_id: market for market in limited}

    @staticmethod
    def _is_excluded_market(
        category: str,
        tags: list[str],
        excluded_categories: set[str],
        excluded_tag_slugs: set[str],
    ) -> bool:
        normalized_category = category.strip().lower()
        normalized_tags = {tag.strip().lower() for tag in tags}
        return normalized_category in excluded_categories or bool(normalized_tags & excluded_tag_slugs)
