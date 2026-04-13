from __future__ import annotations

from contracts.common import PipelineEnvelope, StageName
from pipeline.db import PipelineDB


def test_stage_messages_are_idempotent_and_jobs_chain(tmp_path) -> None:
    db = PipelineDB(f"sqlite:///{tmp_path / 'pipeline.db'}")
    db.initialize()
    trace_id = db.ensure_trace(source_alert_id="alert-1")

    envelope = PipelineEnvelope.new(
        trace_id=trace_id,
        source_alert_id="alert-1",
        stage=StageName.INGEST,
        schema_version="v1",
        idempotency_key="scout:alert-1",
        payload={"value": 1},
    )
    message_id, inserted = db.upsert_message(envelope)
    duplicate_message_id, duplicate_inserted = db.upsert_message(envelope)
    db.enqueue_job(trace_id=trace_id, stage=StageName.RESEARCH, message_id=message_id)

    lease = db.lease_next_job(StageName.RESEARCH, "worker-1", lease_sec=60)
    assert lease is not None

    output = PipelineEnvelope.new(
        trace_id=trace_id,
        source_alert_id="alert-1",
        parent_message_id=message_id,
        stage=StageName.RESEARCH,
        schema_version="v1",
        idempotency_key="research:1:v1",
        payload={"value": 2},
    )
    db.complete_job_success(lease, output_envelope=output, next_stage=StageName.ANALYSIS)
    next_lease = db.lease_next_job(StageName.ANALYSIS, "worker-2", lease_sec=60)

    assert inserted is True
    assert duplicate_inserted is False
    assert duplicate_message_id == message_id
    assert next_lease is not None
    assert next_lease.message_id == output.message_id
