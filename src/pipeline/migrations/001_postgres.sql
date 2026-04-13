CREATE TABLE IF NOT EXISTS pipeline_traces (
    trace_id         UUID PRIMARY KEY,
    source_alert_id  TEXT NOT NULL UNIQUE,
    market_asset_id  TEXT,
    market_slug      TEXT,
    event_slug       TEXT,
    current_stage    TEXT NOT NULL,
    status           TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_messages (
    message_id        UUID PRIMARY KEY,
    trace_id          UUID NOT NULL REFERENCES pipeline_traces(trace_id),
    stage             TEXT NOT NULL,
    parent_message_id UUID NULL REFERENCES stage_messages(message_id),
    schema_version    TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    payload_jsonb     JSONB NOT NULL,
    payload_hash      TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stage_messages_trace ON stage_messages(trace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_stage_messages_parent ON stage_messages(parent_message_id);

CREATE TABLE IF NOT EXISTS stage_jobs (
    job_id         UUID PRIMARY KEY,
    trace_id       UUID NOT NULL REFERENCES pipeline_traces(trace_id),
    stage          TEXT NOT NULL,
    message_id     UUID NOT NULL REFERENCES stage_messages(message_id),
    state          TEXT NOT NULL,
    priority       INT NOT NULL DEFAULT 100,
    attempt_count  INT NOT NULL DEFAULT 0,
    available_at   TIMESTAMPTZ NOT NULL,
    leased_until   TIMESTAMPTZ,
    worker_id      TEXT,
    last_error     TEXT,
    created_at     TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL,
    UNIQUE(stage, message_id)
);
CREATE INDEX IF NOT EXISTS idx_stage_jobs_ready ON stage_jobs(stage, state, available_at, priority);

CREATE TABLE IF NOT EXISTS stage_runs (
    run_id            UUID PRIMARY KEY,
    job_id            UUID NOT NULL REFERENCES stage_jobs(job_id),
    message_id        UUID NOT NULL REFERENCES stage_messages(message_id),
    started_at        TIMESTAMPTZ NOT NULL,
    finished_at       TIMESTAMPTZ,
    result_state      TEXT NOT NULL,
    model_name        TEXT,
    prompt_version    TEXT,
    token_input       INT NOT NULL DEFAULT 0,
    token_output      INT NOT NULL DEFAULT 0,
    cost_usd          NUMERIC(18, 8) NOT NULL DEFAULT 0,
    latency_ms        INT NOT NULL DEFAULT 0,
    tool_calls_jsonb  JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_jsonb       JSONB
);

CREATE TABLE IF NOT EXISTS research_evidence (
    evidence_id      UUID PRIMARY KEY,
    message_id       UUID NOT NULL REFERENCES stage_messages(message_id),
    url              TEXT NOT NULL,
    title            TEXT NOT NULL,
    publisher        TEXT NOT NULL,
    published_at     TIMESTAMPTZ,
    retrieved_at     TIMESTAMPTZ NOT NULL,
    source_tier      TEXT NOT NULL,
    relevance_score  DOUBLE PRECISION NOT NULL,
    claim_tag        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_candidates (
    candidate_id      UUID PRIMARY KEY,
    message_id        UUID NOT NULL REFERENCES stage_messages(message_id),
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    status            TEXT NOT NULL,
    score             DOUBLE PRECISION NOT NULL,
    broker_eligible   BOOLEAN NOT NULL,
    max_notional      NUMERIC(18, 8) NOT NULL,
    stop_loss_pct     NUMERIC(18, 8) NOT NULL,
    features_jsonb    JSONB NOT NULL,
    filters_jsonb     JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snapshot_id            UUID PRIMARY KEY,
    captured_at            TIMESTAMPTZ NOT NULL,
    account_id             TEXT NOT NULL,
    equity                 NUMERIC(18, 8) NOT NULL,
    cash                   NUMERIC(18, 8) NOT NULL,
    buying_power           NUMERIC(18, 8) NOT NULL,
    gross_exposure         NUMERIC(18, 8) NOT NULL,
    net_exposure           NUMERIC(18, 8) NOT NULL,
    sector_exposure_jsonb  JSONB NOT NULL,
    positions_jsonb        JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id      UUID PRIMARY KEY,
    trace_id         UUID NOT NULL REFERENCES pipeline_traces(trace_id),
    message_id       UUID NOT NULL REFERENCES stage_messages(message_id),
    candidate_id     UUID NOT NULL,
    channel          TEXT NOT NULL,
    status           TEXT NOT NULL,
    requested_at     TIMESTAMPTZ NOT NULL,
    expires_at       TIMESTAMPTZ NOT NULL,
    decided_at       TIMESTAMPTZ,
    approver_id      TEXT,
    decision_reason  TEXT,
    callback_token   TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS broker_orders (
    order_id            UUID PRIMARY KEY,
    trace_id            UUID NOT NULL REFERENCES pipeline_traces(trace_id),
    candidate_id        UUID NOT NULL,
    client_order_id     TEXT NOT NULL UNIQUE,
    broker              TEXT NOT NULL,
    broker_order_id     TEXT UNIQUE,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    qty                 NUMERIC(18, 8),
    notional            NUMERIC(18, 8),
    order_type          TEXT NOT NULL,
    tif                 TEXT NOT NULL,
    submitted_at        TIMESTAMPTZ NOT NULL,
    status              TEXT NOT NULL,
    avg_fill_price      NUMERIC(18, 8),
    stop_order_id       TEXT,
    raw_response_jsonb  JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_letters (
    dead_letter_id   UUID PRIMARY KEY,
    job_id           UUID NOT NULL REFERENCES stage_jobs(job_id),
    stage            TEXT NOT NULL,
    reason           TEXT NOT NULL,
    payload_jsonb    JSONB NOT NULL,
    first_failed_at  TIMESTAMPTZ NOT NULL,
    last_failed_at   TIMESTAMPTZ NOT NULL,
    replay_count     INT NOT NULL DEFAULT 0,
    resolved_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS outcome_observations (
    observation_id         UUID PRIMARY KEY,
    trace_id               UUID NOT NULL REFERENCES pipeline_traces(trace_id),
    symbol                 TEXT NOT NULL,
    horizon                TEXT NOT NULL,
    scheduled_for          TIMESTAMPTZ NOT NULL,
    observed_at            TIMESTAMPTZ,
    entry_price            NUMERIC(18, 8) NOT NULL,
    exit_price             NUMERIC(18, 8),
    return_pct             NUMERIC(18, 8),
    benchmark_return_pct   NUMERIC(18, 8),
    label                  TEXT
);
