from __future__ import annotations

import asyncio
from pathlib import Path
from urllib import request

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
    def __init__(self, url: str, timeout_sec: int = 10) -> None:
        self._url = url
        self._timeout_sec = timeout_sec

    async def send(self, alert: ShiftAlert) -> None:
        if not self._url:
            return
        data = alert.to_json().encode()
        await asyncio.to_thread(self._post_json, data)

    def _post_json(self, data: bytes) -> None:
        req = request.Request(self._url, data=data, method="POST", headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=self._timeout_sec) as response:
            response.read()
