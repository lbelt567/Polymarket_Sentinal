from __future__ import annotations

import asyncio
import json
from urllib import request

from sentinel.processing.alerts import ShiftAlert


class DiscordNotifier:
    def __init__(self, webhook_url: str, timeout_sec: int = 10) -> None:
        self._webhook_url = webhook_url
        self._timeout_sec = timeout_sec

    async def send(self, alert: ShiftAlert) -> None:
        if not self._webhook_url:
            return
        direction = alert.shift.direction.upper()
        content = (
            f"[{alert.alert_level_label}] {alert.market['question']}\n"
            f"{direction} {alert.shift.delta_pct:.2f}% over {alert.shift.window_sec}s "
            f"({alert.signal_quality.confidence})"
        )
        payload = json.dumps({"content": content}).encode()
        await asyncio.to_thread(self._post_json, payload)

    def _post_json(self, payload: bytes) -> None:
        req = request.Request(
            self._webhook_url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=self._timeout_sec) as response:
            response.read()
