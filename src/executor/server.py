from __future__ import annotations

from pipeline.approvals import ApprovalService
from pipeline.config import PipelineConfig
from pipeline.db import PipelineDB


def create_app(db: PipelineDB, approval_service: ApprovalService):
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("fastapi is required to create the executor API") from exc

    app = FastAPI(title="Polymarket Sentinel Executor API")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/approval/{token}/approve")
    async def approve(token: str, approver_id: str, reason: str | None = None) -> dict[str, str]:
        try:
            decision = approval_service.approve(token, approver_id=approver_id, reason=reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"approval_id": str(decision.approval_id), "status": decision.status}

    @app.post("/approval/{token}/reject")
    async def reject(token: str, approver_id: str, reason: str | None = None) -> dict[str, str]:
        try:
            decision = approval_service.reject(token, approver_id=approver_id, reason=reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"approval_id": str(decision.approval_id), "status": decision.status}

    return app


def run_server(config: PipelineConfig | None = None) -> None:
    from pipeline.config import load_pipeline_config

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("uvicorn is required to run the executor API") from exc

    loaded = config or load_pipeline_config()
    db = PipelineDB(loaded.database.url)
    db.initialize()
    approval_service = ApprovalService(db)
    uvicorn.run(create_app(db, approval_service), host="0.0.0.0", port=8003)
