CREATE TABLE IF NOT EXISTS pipeline_traces (
    trace_id         TEXT PRIMARY KEY,
    source_alert_id  TEXT NOT NULL UNIQUE,
    market_asset_id  TEXT,
    market_slug      TEXT,
    event_slug       TEXT,
    current_stage    TEXT NOT NULL,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_messages (
    message_id        TEXT PRIMARY KEY,
    trace_id          TEXT NOT NULL,
    stage             TEXT NOT NULL,
    parent_message_id TEXT,
    schema_version    TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    payload_jsonb     TEXT NOT NULL,
    payload_hash      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    FOREIGN KEY(trace_id) REFERENCES pipeline_traces(trace_id),
    FOREIGN KEY(parent_message_id) REFERENCES stage_messages(message_id)
);
CREATE INDEX IF NOT EXISTS idx_stage_messages_trace ON stage_messages(trace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_stage_messages_parent ON stage_messages(parent_message_id);

CREATE TABLE IF NOT EXISTS stage_jobs (
    job_id         TEXT PRIMARY KEY,
    trace_id       TEXT NOT NULL,
    stage          TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    state          TEXT NOT NULL,
    priority       INTEGER NOT NULL DEFAULT 100,
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    available_at   TEXT NOT NULL,
    leased_until   TEXT,
    worker_id      TEXT,
    last_error     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(stage, message_id),
    FOREIGN KEY(trace_id) REFERENCES pipeline_traces(trace_id),
    FOREIGN KEY(message_id) REFERENCES stage_messages(message_id)
);
CREATE INDEX IF NOT EXISTS idx_stage_jobs_ready ON stage_jobs(stage, state, available_at, priority);

CREATE TABLE IF NOT EXISTS stage_runs (
    run_id            TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL,
    message_id        TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    result_state      TEXT NOT NULL,
    model_name        TEXT,
    prompt_version    TEXT,
    token_input       INTEGER NOT NULL DEFAULT 0,
    token_output      INTEGER NOT NULL DEFAULT 0,
    cost_usd          TEXT NOT NULL DEFAULT '0',
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    tool_calls_jsonb  TEXT NOT NULL DEFAULT '[]',
    error_jsonb       TEXT,
    FOREIGN KEY(job_id) REFERENCES stage_jobs(job_id),
    FOREIGN KEY(message_id) REFERENCES stage_messages(message_id)
);

CREATE TABLE IF NOT EXISTS research_evidence (
    evidence_id      TEXT PRIMARY KEY,
    message_id       TEXT NOT NULL,
    url              TEXT NOT NULL,
    title            TEXT NOT NULL,
    publisher        TEXT NOT NULL,
    published_at     TEXT,
    retrieved_at     TEXT NOT NULL,
    source_tier      TEXT NOT NULL,
    relevance_score  REAL NOT NULL,
    claim_tag        TEXT NOT NULL,
    FOREIGN KEY(message_id) REFERENCES stage_messages(message_id)
);

CREATE TABLE IF NOT EXISTS analysis_candidates (
    candidate_id      TEXT PRIMARY KEY,
    message_id        TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    status            TEXT NOT NULL,
    score             REAL NOT NULL,
    broker_eligible   INTEGER NOT NULL,
    max_notional      TEXT NOT NULL,
    stop_loss_pct     TEXT NOT NULL,
    features_jsonb    TEXT NOT NULL,
    filters_jsonb     TEXT NOT NULL,
    FOREIGN KEY(message_id) REFERENCES stage_messages(message_id)
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snapshot_id            TEXT PRIMARY KEY,
    captured_at            TEXT NOT NULL,
    account_id             TEXT NOT NULL,
    equity                 TEXT NOT NULL,
    cash                   TEXT NOT NULL,
    buying_power           TEXT NOT NULL,
    gross_exposure         TEXT NOT NULL,
    net_exposure           TEXT NOT NULL,
    sector_exposure_jsonb  TEXT NOT NULL,
    positions_jsonb        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id      TEXT PRIMARY KEY,
    trace_id         TEXT NOT NULL,
    message_id       TEXT NOT NULL,
    candidate_id     TEXT NOT NULL,
    channel          TEXT NOT NULL,
    status           TEXT NOT NULL,
    requested_at     TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    decided_at       TEXT,
    approver_id      TEXT,
    decision_reason  TEXT,
    callback_token   TEXT NOT NULL UNIQUE,
    FOREIGN KEY(trace_id) REFERENCES pipeline_traces(trace_id),
    FOREIGN KEY(message_id) REFERENCES stage_messages(message_id)
);

CREATE TABLE IF NOT EXISTS broker_orders (
    order_id            TEXT PRIMARY KEY,
    trace_id            TEXT NOT NULL,
    candidate_id        TEXT NOT NULL,
    client_order_id     TEXT NOT NULL UNIQUE,
    broker              TEXT NOT NULL,
    broker_order_id     TEXT UNIQUE,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    qty                 TEXT,
    notional            TEXT,
    order_type          TEXT NOT NULL,
    tif                 TEXT NOT NULL,
    submitted_at        TEXT NOT NULL,
    status              TEXT NOT NULL,
    avg_fill_price      TEXT,
    stop_order_id       TEXT,
    raw_response_jsonb  TEXT NOT NULL,
    FOREIGN KEY(trace_id) REFERENCES pipeline_traces(trace_id)
);

CREATE TABLE IF NOT EXISTS dead_letters (
    dead_letter_id   TEXT PRIMARY KEY,
    job_id           TEXT NOT NULL,
    stage            TEXT NOT NULL,
    reason           TEXT NOT NULL,
    payload_jsonb    TEXT NOT NULL,
    first_failed_at  TEXT NOT NULL,
    last_failed_at   TEXT NOT NULL,
    replay_count     INTEGER NOT NULL DEFAULT 0,
    resolved_at      TEXT,
    FOREIGN KEY(job_id) REFERENCES stage_jobs(job_id)
);

CREATE TABLE IF NOT EXISTS outcome_observations (
    observation_id         TEXT PRIMARY KEY,
    trace_id               TEXT NOT NULL,
    symbol                 TEXT NOT NULL,
    horizon                TEXT NOT NULL,
    scheduled_for          TEXT NOT NULL,
    observed_at            TEXT,
    entry_price            TEXT NOT NULL,
    exit_price             TEXT,
    return_pct             TEXT,
    benchmark_return_pct   TEXT,
    label                  TEXT,
    FOREIGN KEY(trace_id) REFERENCES pipeline_traces(trace_id)
);
