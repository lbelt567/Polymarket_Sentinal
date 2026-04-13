from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Mapping

from pydantic import ValidationError

from contracts.common import PipelineEnvelope, StageName
from contracts.research import IngestedAlert
from contracts.scout import ShiftAlertMirror
from pipeline.config import PipelineConfig
from pipeline.db import PipelineDB
from pipeline.idempotency import build_ingest_key


@dataclass(slots=True)
class IngressResponse:
    status_code: int
    body: dict[str, object]


class ResearcherIngressService:
    def __init__(self, db: PipelineDB, config: PipelineConfig, *, schema_version: str = "v1") -> None:
        self._db = db
        self._config = config
        self._schema_version = schema_version

    def handle_alert(self, body: bytes, headers: Mapping[str, str], *, replay_source: str | None = None) -> IngressResponse:
        header_map = {key.lower(): value for key, value in headers.items()}

        if not self._authorize(header_map):
            return IngressResponse(status_code=401, body={"error": "unauthorized"})
        if not self._verify_signature(body, header_map):
            return IngressResponse(status_code=401, body={"error": "invalid_signature"})

        try:
            shift_alert = ShiftAlertMirror.model_validate_json(body)
        except ValidationError as exc:
            return IngressResponse(status_code=400, body={"error": "invalid_payload", "details": exc.errors()})
        except json.JSONDecodeError:
            return IngressResponse(status_code=400, body={"error": "invalid_json"})

        header_alert_id = header_map.get("x-sentinel-alert-id")
        if header_alert_id and header_alert_id != shift_alert.alert_id:
            return IngressResponse(status_code=400, body={"error": "alert_id_mismatch"})

        trace_id = self._db.ensure_trace(
            source_alert_id=shift_alert.alert_id,
            market_asset_id=str(shift_alert.market.get("asset_id") or ""),
            market_slug=str(shift_alert.market.get("market_slug") or ""),
            event_slug=str(shift_alert.market.get("event_slug") or ""),
            current_stage=StageName.INGEST,
            status="persisted",
        )

        ingested = IngestedAlert(
            shift_alert=shift_alert,
            signature_verified=True,
            replay_source=replay_source,
            priority=100,
        )
        envelope = PipelineEnvelope.new(
            trace_id=trace_id,
            source_alert_id=shift_alert.alert_id,
            stage=StageName.INGEST,
            schema_version=self._schema_version,
            idempotency_key=build_ingest_key(shift_alert.alert_id),
            payload=ingested.model_dump(mode="json"),
        )
        message_id, inserted = self._db.upsert_message(envelope)
        if inserted:
            self._db.enqueue_job(trace_id=trace_id, stage=StageName.RESEARCH, message_id=message_id, priority=ingested.priority)

        return IngressResponse(
            status_code=202,
            body={
                "trace_id": str(trace_id),
                "message_id": str(message_id),
                "duplicate": not inserted,
                "stage": StageName.RESEARCH.value,
            },
        )

    def _authorize(self, headers: Mapping[str, str]) -> bool:
        expected = self._config.researcher.webhook_bearer_token
        if not expected:
            return True
        auth = headers.get("authorization", "")
        return auth == f"Bearer {expected}"

    def _verify_signature(self, body: bytes, headers: Mapping[str, str]) -> bool:
        secret = self._config.researcher.webhook_hmac_secret
        if not secret:
            return True
        provided = headers.get("x-sentinel-signature", "")
        if not provided.startswith("sha256="):
            return False
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided, f"sha256={digest}")


def create_app(service: ResearcherIngressService):
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("fastapi is required to create the researcher API") from exc

    app = FastAPI(title="Polymarket Sentinel Researcher API")

    @app.post("/webhook/alert")
    async def receive_alert(request: Request) -> JSONResponse:
        result = service.handle_alert(await request.body(), request.headers)
        return JSONResponse(status_code=result.status_code, content=result.body)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def run_server(config: PipelineConfig | None = None) -> None:
    from pipeline.config import load_pipeline_config

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("uvicorn is required to run the researcher API") from exc

    loaded = config or load_pipeline_config()
    db = PipelineDB(loaded.database.url)
    db.initialize()
    service = ResearcherIngressService(db, loaded)
    uvicorn.run(create_app(service), host=loaded.researcher.http_host, port=loaded.researcher.http_port)
