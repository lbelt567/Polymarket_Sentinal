from __future__ import annotations

from typing import Any

import httpx


class BraveSearchClient:
    def __init__(self, api_key: str, base_url: str = "https://api.search.brave.com/res/v1/web/search") -> None:
        self._api_key = api_key
        self._base_url = base_url

    def search(self, query: str, count: int = 5) -> list[dict[str, Any]]:
        if not self._api_key:
            raise RuntimeError("Brave search API key is required")
        response = httpx.get(
            self._base_url,
            params={"q": query, "count": count},
            headers={"X-Subscription-Token": self._api_key},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("web", {}).get("results", [])
