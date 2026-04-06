from __future__ import annotations

import asyncio
import hashlib
import hmac
from pathlib import Path
from urllib import error, request

from sentinel.processing.alerts import ShiftAlert


class JsonFileNotifier:
    def __init__(self, output_dir: str) -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    async def send(self, alert: ShiftAlert) -> None:
        filename = f"{alert.timestamp_ms}_{alert.market['asset_id']}.json"
        path = self._output_dir / filename
        await asyncio.to_thread(path.write_text, alert.to_json())


class HttpJsonWebhookNotifier:
    def __init__(
        self,
        url: str,
        timeout_sec: int = 10,
        max_retries: int = 3,
        retry_backoff_sec: float = 1.0,
        bearer_token: str = "",
        hmac_secret: str = "",
    ) -> None:
        self._url = url
        self._timeout_sec = timeout_sec
        self._max_retries = max_retries
        self._retry_backoff_sec = retry_backoff_sec
        self._bearer_token = bearer_token
        self._hmac_secret = hmac_secret.encode("utf-8") if hmac_secret else b""

    async def send(self, alert: ShiftAlert) -> None:
        if not self._url:
            return
        data = alert.to_json().encode("utf-8")
        headers = self._build_headers(alert, data)
        attempt = 0
        while True:
            try:
                await asyncio.to_thread(self._post_json, data, headers)
                return
            except error.HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise
                if attempt >= self._max_retries:
                    raise
            except (error.URLError, OSError, TimeoutError):
                if attempt >= self._max_retries:
                    raise
            await asyncio.sleep(self._retry_backoff_sec * (2**attempt))
            attempt += 1

    def _build_headers(self, alert: ShiftAlert, data: bytes) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "polymarket-sentinel/0.1.0",
            "X-Sentinel-Alert-Id": alert.alert_id,
            "X-Sentinel-Alert-Level": alert.alert_level,
            "X-Sentinel-Timestamp-Ms": str(alert.timestamp_ms),
        }
        event_slug = str(alert.market.get("event_slug") or "")
        if event_slug:
            headers["X-Sentinel-Event-Slug"] = event_slug
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        if self._hmac_secret:
            digest = hmac.new(self._hmac_secret, data, hashlib.sha256).hexdigest()
            headers["X-Sentinel-Signature"] = f"sha256={digest}"
        return headers

    def _post_json(self, data: bytes, headers: dict[str, str]) -> None:
        req = request.Request(self._url, data=data, method="POST", headers=headers)
        with request.urlopen(req, timeout=self._timeout_sec) as response:
            response.read()
