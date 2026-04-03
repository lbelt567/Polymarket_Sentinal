from __future__ import annotations

import asyncio
import json
from urllib import parse, request

from sentinel.processing.alerts import ShiftAlert


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout_sec: int = 10) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout_sec = timeout_sec

    async def send(self, alert: ShiftAlert) -> None:
        if not self._bot_token or not self._chat_id:
            return
        text = (
            f"{alert.alert_level_label}\n"
            f"{alert.market['question']}\n"
            f"{alert.shift.direction.upper()} {alert.shift.delta_pct:.2f}% over {alert.shift.window_sec}s"
        )
        payload = json.dumps({"chat_id": self._chat_id, "text": text}).encode()
        await asyncio.to_thread(self._post_json, payload)

    def _post_json(self, payload: bytes) -> None:
        url = f"https://api.telegram.org/bot{parse.quote(self._bot_token)}/sendMessage"
        req = request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=self._timeout_sec) as response:
            response.read()
