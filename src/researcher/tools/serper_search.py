from __future__ import annotations

from typing import Any

import httpx


class SerperSearchClient:
    def __init__(self, api_key: str, base_url: str = "https://google.serper.dev/search") -> None:
        self._api_key = api_key
        self._base_url = base_url

    def search(self, query: str, num: int = 5) -> list[dict[str, Any]]:
        if not self._api_key:
            raise RuntimeError("Serper API key is required")
        response = httpx.post(
            self._base_url,
            json={"q": query, "num": num},
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("organic", [])
