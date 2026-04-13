from __future__ import annotations

from typing import Any

import httpx


class PolymarketContextClient:
    def __init__(self, base_url: str = "https://gamma-api.polymarket.com") -> None:
        self._base_url = base_url.rstrip("/")

    def event(self, event_slug: str) -> dict[str, Any]:
        response = httpx.get(f"{self._base_url}/events", params={"slug": event_slug, "limit": 1}, timeout=10.0)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list) and payload:
            return payload[0]
        return {}
