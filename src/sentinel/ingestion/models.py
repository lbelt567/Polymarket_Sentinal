from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def parse_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_ts_ms(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
        try:
            dt = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return 0
        return int(dt.astimezone(timezone.utc).timestamp() * 1000)
    return 0


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            loaded = json.loads(stripped)
        except json.JSONDecodeError:
            return [value]
        return loaded if isinstance(loaded, list) else [loaded]
    return [value]


def normalize_tags(value: Any) -> list[str]:
    tags: list[str] = []
    for item in ensure_list(value):
        if isinstance(item, dict):
            candidate = item.get("slug") or item.get("label") or item.get("name")
            if candidate:
                tags.append(str(candidate))
        elif item not in (None, ""):
            tags.append(str(item))
    return tags


@dataclass(slots=True)
class MarketMetadata:
    asset_id: str
    market_id: str
    condition_id: str
    market_slug: str
    question: str
    description: str
    category: str
    tags: list[str]
    event_id: str
    event_title: str
    event_slug: str
    sibling_asset_ids: list[str]
    sibling_market_slugs: list[str]
    outcome: str
    end_date: str
    volume_24h: float
    liquidity: float
    polymarket_url: str
    active: bool = True
    last_tick_ms: int = 0
    subscribed_at_ms: int = 0


@dataclass(slots=True)
class PriceTick:
    asset_id: str
    price: float
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    signal_source: str
    quote_ts_ms: int | None
    timestamp_ms: int


@dataclass(slots=True)
class MarketResolvedEvent:
    market_id: str
    winning_asset_id: str
    winning_outcome: str
    timestamp_ms: int
    asset_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SubscriptionCommand:
    operation: str
    asset_ids: list[str]


def extract_yes_token_id(market_payload: dict[str, Any]) -> str | None:
    outcomes = [str(item).strip().lower() for item in ensure_list(market_payload.get("outcomes"))]
    token_ids = [str(item) for item in ensure_list(market_payload.get("clobTokenIds") or market_payload.get("clob_token_ids"))]
    if not outcomes or not token_ids or len(outcomes) != len(token_ids):
        return None
    mapping = {outcome: token_id for outcome, token_id in zip(outcomes, token_ids, strict=True)}
    return mapping.get("yes")
