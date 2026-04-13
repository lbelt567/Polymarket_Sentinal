from __future__ import annotations

from typing import Any


def build_health_payload(service: str, **extra: Any) -> dict[str, Any]:
    payload = {"service": service, "status": "ok"}
    payload.update(extra)
    return payload
