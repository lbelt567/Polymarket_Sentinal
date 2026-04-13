# Multi-Agent Trading Pipeline - Tightened V2 Plan

## Context

The Polymarket Sentinel (Scout agent) is complete and running. It monitors prediction markets 24/7, detects price shifts, and emits structured JSON alerts via HTTP webhooks with HMAC signing.

The next step is to build the downstream pipeline that turns a Scout alert into:

1. A researched catalyst
2. A validated trade candidate
3. A risk-checked execution decision
4. A recorded outcome for later evaluation

This version of the plan tightens the original design in four places:

- Scout remains unchanged
- Long-running work never happens inline on the Scout webhook request
- Every stage is durable and idempotent
- The Executor is deterministic and policy-driven, not LLM-driven

---

## Goals

- Preserve Scout as the alert producer with zero changes to its runtime behavior
- Build a reliable downstream pipeline inside this repo as sibling packages
- Support replay, retries, dead-letter recovery, and full decision tracing
- Keep the system safe enough for paper trading first and live trading much later
- Make evaluation and iteration possible by recording structured evidence and outcomes

## Non-Goals For V1

- Live autonomous trading
- Trading anything beyond U.S. equities and ETFs
- Short selling, options, crypto spot, or pre/post-market execution
- A generic orchestration platform
- Perfect catalyst attribution in every case

---

## Architectural Decisions

### 1. Monorepo Stays

All agents live in this repo under `src/` as sibling packages.

Rationale:

- Solo developer overhead stays low
- Shared contracts evolve safely in one place
- Docker images and process entry points are enough separation
- Cross-stage testing and replay are much easier

### 2. Scout Stays Untouched

Scout keeps posting signed `ShiftAlert` JSON to one downstream webhook endpoint.

Rationale:

- The alert payload is already rich
- The existing HMAC/header pattern is sufficient
- Avoids risk to the live monitoring system

### 3. Webhook At The Edge, Durable Queue Inside

The original sequential webhook chain is replaced after ingress.

New rule:

- Scout calls `Researcher API /webhook/alert`
- The API verifies HMAC, validates the payload, writes to Postgres, enqueues a research job, and returns `202 Accepted`
- All later work happens in background workers

This avoids:

- Blocking Scout on LLM/search latency
- Cascading failures from downstream outages
- Duplicate downstream side effects from retries without persistence

### 4. Postgres Is The Pipeline System Of Record

Postgres is required for the downstream pipeline in all real environments.

SQLite remains acceptable only for:

- Local tests
- Very small single-process experiments

Rationale:

- Multiple services/workers will write concurrently
- Job leasing and idempotency are easier and safer in Postgres
- Dead-letter, approval, order, and outcome tracking all want transactional guarantees

### 5. Executor Must Be Deterministic

The Executor does not use an LLM to decide whether to place an order.

The Executor may consume upstream analysis, but trade placement is controlled by:

- Deterministic risk rules
- Broker/account state
- Approval policy
- Market session checks
- Order construction logic

### 6. V1 Trading Scope Is Narrow By Design

V1 trading scope:

- U.S. equities and ETFs only
- Long-only
- Paper trading only
- Regular hours only
- Limit orders only
- Every position must have a stop-loss

This keeps the operational surface small while evaluation is still immature.

---

## End-To-End Flow

```text
Scout
  -> POST /webhook/alert (signed ShiftAlert JSON)
     Researcher API
       - verify HMAC
       - validate ShiftAlert mirror
       - upsert trace
       - persist ingest message
       - enqueue research job
       - return 202 fast

Postgres
  -> stage_messages
  -> stage_jobs
  -> stage_runs
  -> approvals
  -> broker_orders
  -> outcomes

Researcher Worker
  -> load ingest message
  -> run search + source validation + LLM synthesis
  -> persist ResearchReport
  -> enqueue analysis job if status=pass

Analyst Worker
  -> load ResearchReport
  -> resolve tradable symbols
  -> fetch market/account context
  -> compute deterministic filters + ranking
  -> persist AnalysisReport
  -> enqueue execution job if status=pass

Executor Worker
  -> load AnalysisReport
  -> apply risk engine
  -> create approval request or place paper order
  -> persist ExecutionDecision
  -> schedule outcome observations

Outcome Worker
  -> capture t+15m, t+1h, next_session_close observations
  -> persist labels for evaluation and backtesting
```

---

## Repo Structure

```text
Polymarket_Sentinal/
├── config.yaml                      # Scout config, unchanged
├── pipeline.yaml                    # New pipeline config
├── pyproject.toml                   # Expanded dependencies
├── Dockerfile                       # Scout image, unchanged
├── Dockerfile.pipeline              # Shared image for pipeline services
├── docker-compose.yaml              # Orchestrates Scout + pipeline + Postgres
├── PLAN.md                          # This file
├── src/
│   ├── sentinel/                    # Existing Scout, unchanged
│   ├── contracts/                   # Shared Pydantic v2 data models
│   │   ├── __init__.py
│   │   ├── scout.py                 # ShiftAlert mirror
│   │   ├── common.py                # Envelope, enums, shared primitives
│   │   ├── research.py              # Research contracts
│   │   ├── analysis.py              # Analysis contracts
│   │   └── execution.py             # Execution contracts
│   ├── pipeline/                    # Shared infra
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── db.py
│   │   ├── queue.py                 # Job leasing, retries, dead-letter
│   │   ├── idempotency.py
│   │   ├── tracing.py
│   │   ├── approvals.py
│   │   ├── outcomes.py
│   │   └── migrations/
│   ├── researcher/
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── config.py
│   │   ├── server.py                # FastAPI ingress only
│   │   ├── worker.py                # Research job runner
│   │   ├── agent.py                 # LLM orchestration
│   │   ├── prompts/
│   │   └── tools/
│   ├── analyst/
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── config.py
│   │   ├── worker.py
│   │   ├── agent.py
│   │   ├── filters.py
│   │   ├── prompts/
│   │   └── tools/
│   ├── executor/
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── config.py
│   │   ├── server.py                # Health + approval callbacks
│   │   ├── worker.py
│   │   ├── broker.py
│   │   ├── risk/
│   │   └── approvals/
│   └── outcome/
│       ├── __init__.py
│       ├── __main__.py
│       ├── worker.py
│       └── pricing.py
├── tests/
│   ├── test_contracts.py
│   ├── test_queue.py
│   ├── test_researcher/
│   ├── test_analyst/
│   ├── test_executor/
│   └── test_outcome/
└── scripts/
    ├── replay_alert.py
    └── replay_stage.py
```

---

## Runtime Components

### Researcher API

Responsibilities:

- Receive Scout webhook
- Verify HMAC signature and headers
- Validate `ShiftAlert` mirror
- Write ingest message and research job
- Return `202 Accepted` quickly

Non-responsibilities:

- No inline search
- No inline LLM call
- No direct call to Analyst

### Researcher Worker

Responsibilities:

- Find and validate catalyst evidence
- Produce `ResearchReport`
- Persist structured evidence, audit metadata, and stage result

### Analyst Worker

Responsibilities:

- Turn asset hypotheses into tradable candidates
- Resolve symbol eligibility
- Pull market data and compute deterministic filters
- Produce `AnalysisReport`

### Executor Worker

Responsibilities:

- Apply risk engine
- Check broker/account state
- Create approval request or place paper trade
- Produce `ExecutionDecision`

### Executor API

Responsibilities:

- Health checks
- Approval callbacks and approval status endpoints

### Outcome Worker

Responsibilities:

- Observe post-trade and post-signal performance
- Write evaluation labels for future tuning

---

## Data Contracts

All stage payloads are Pydantic v2 models under `src/contracts/`.

### Shared Rules

- Every stage output is wrapped in a `PipelineEnvelope`
- Every envelope has a unique `message_id`
- Every envelope has an `idempotency_key`
- Every envelope includes `schema_version`
- Breaking changes bump `schema_version`
- Do not store chain-of-thought
- Store structured evidence, tool usage, and short summaries instead

### PipelineEnvelope

```python
class PipelineEnvelope(BaseModel):
    message_id: UUID
    trace_id: UUID
    source_alert_id: str
    parent_message_id: UUID | None
    stage: Literal["ingest", "research", "analysis", "execution", "approval", "outcome"]
    schema_version: str
    idempotency_key: str
    created_at: datetime
    payload: dict[str, Any]
```

### IngestedAlert

```python
class IngestedAlert(BaseModel):
    shift_alert: ShiftAlertMirror
    received_at: datetime
    signature_verified: bool
    replay_source: str | None = None
    priority: int = 100
```

### EvidenceItem

```python
class EvidenceItem(BaseModel):
    url: str
    title: str
    publisher: str
    published_at: datetime | None
    retrieved_at: datetime
    source_tier: Literal["primary", "major_press", "secondary"]
    relevance_score: float
    claim_tag: str
```

### AssetHypothesis

```python
class AssetHypothesis(BaseModel):
    symbol: str
    instrument_type: Literal["equity", "etf"]
    direction: Literal["bullish", "bearish"]
    horizon: Literal["intraday", "swing"]
    linkage_type: Literal["direct", "supplier", "competitor", "sector_proxy", "macro_proxy"]
    confidence: Literal["high", "medium", "low"]
    tradable_universe: bool
    rationale_summary: str
```

### ResearchReport

```python
class ResearchReport(BaseModel):
    status: Literal["pass", "defer", "insufficient_evidence", "conflicting_evidence"]
    catalyst_type: Literal["policy", "economic_data", "geopolitical", "corporate", "legal", "other"]
    catalyst_summary: str
    catalyst_started_at: datetime | None
    freshness_sec: int | None
    evidence: list[EvidenceItem]
    asset_hypotheses: list[AssetHypothesis]
    unsupported_claims: list[str]
    stage_summary: str
```

### FilterResult

```python
class FilterResult(BaseModel):
    name: str
    passed: bool
    value: str
    reason: str
```

### TradeCandidate

```python
class TradeCandidate(BaseModel):
    candidate_id: UUID
    symbol: str
    side: Literal["buy"]
    thesis_horizon: Literal["intraday", "swing"]
    broker_eligible: bool
    market_snapshot: dict[str, Any]
    features: dict[str, Any]
    filters: list[FilterResult]
    score: float
    max_notional: Decimal
    stop_loss_pct: Decimal
    expiry_at: datetime
```

### AnalysisReport

```python
class AnalysisReport(BaseModel):
    status: Literal["pass", "reject", "needs_review"]
    candidates: list[TradeCandidate]
    rejected_candidates: list[TradeCandidate]
    portfolio_snapshot_id: UUID
    stage_summary: str
```

### TradeIntent

```python
class TradeIntent(BaseModel):
    candidate_id: UUID
    symbol: str
    side: Literal["buy"]
    order_type: Literal["limit"]
    tif: Literal["day"]
    max_notional: Decimal
    limit_price: Decimal
    stop_loss_pct: Decimal
    max_slippage_bps: int
    thesis_horizon: Literal["intraday", "swing"]
```

### RiskCheckResult

```python
class RiskCheckResult(BaseModel):
    name: str
    passed: bool
    measured_value: str
    threshold: str
    reason: str
```

### ExecutionDecision

```python
class ExecutionDecision(BaseModel):
    status: Literal[
        "rejected_risk",
        "awaiting_approval",
        "approved",
        "submitted",
        "partially_filled",
        "filled",
        "cancelled",
        "expired",
        "error",
    ]
    selected_candidate_id: UUID | None
    intent: TradeIntent | None
    risk_checks: list[RiskCheckResult]
    approval_id: UUID | None
    broker_order_ids: list[str]
    stage_summary: str
```

### StageAudit

This is the audit artifact stored alongside the stage run. It replaces any idea of storing raw chain-of-thought.

```python
class StageAudit(BaseModel):
    message_id: UUID
    model_name: str | None
    prompt_version: str | None
    tool_calls: list[dict[str, Any]]
    token_input: int = 0
    token_output: int = 0
    latency_ms: int = 0
    cost_usd: Decimal = Decimal("0")
    error: str | None = None
```

---

## Database Design

The downstream pipeline uses Postgres as the system of record.

### 1. `pipeline_traces`

Purpose:

- One row per end-to-end alert flow

Columns:

- `trace_id UUID PRIMARY KEY`
- `source_alert_id TEXT UNIQUE NOT NULL`
- `market_asset_id TEXT`
- `market_slug TEXT`
- `event_slug TEXT`
- `current_stage TEXT NOT NULL`
- `status TEXT NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 2. `stage_messages`

Purpose:

- Immutable record of each envelope/payload

Columns:

- `message_id UUID PRIMARY KEY`
- `trace_id UUID NOT NULL REFERENCES pipeline_traces(trace_id)`
- `stage TEXT NOT NULL`
- `parent_message_id UUID NULL REFERENCES stage_messages(message_id)`
- `schema_version TEXT NOT NULL`
- `idempotency_key TEXT NOT NULL UNIQUE`
- `payload_jsonb JSONB NOT NULL`
- `payload_hash TEXT NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL`

### 3. `stage_jobs`

Purpose:

- Durable job queue for workers

Columns:

- `job_id UUID PRIMARY KEY`
- `trace_id UUID NOT NULL REFERENCES pipeline_traces(trace_id)`
- `stage TEXT NOT NULL`
- `message_id UUID NOT NULL REFERENCES stage_messages(message_id)`
- `state TEXT NOT NULL`
- `priority INT NOT NULL DEFAULT 100`
- `attempt_count INT NOT NULL DEFAULT 0`
- `available_at TIMESTAMPTZ NOT NULL`
- `leased_until TIMESTAMPTZ NULL`
- `worker_id TEXT NULL`
- `last_error TEXT NULL`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

Constraints:

- `UNIQUE(stage, message_id)`

### 4. `stage_runs`

Purpose:

- Record each execution attempt of a job

Columns:

- `run_id UUID PRIMARY KEY`
- `job_id UUID NOT NULL REFERENCES stage_jobs(job_id)`
- `message_id UUID NOT NULL REFERENCES stage_messages(message_id)`
- `started_at TIMESTAMPTZ NOT NULL`
- `finished_at TIMESTAMPTZ NULL`
- `result_state TEXT NOT NULL`
- `model_name TEXT NULL`
- `prompt_version TEXT NULL`
- `token_input INT NOT NULL DEFAULT 0`
- `token_output INT NOT NULL DEFAULT 0`
- `cost_usd NUMERIC(18,8) NOT NULL DEFAULT 0`
- `latency_ms INT NOT NULL DEFAULT 0`
- `tool_calls_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb`
- `error_jsonb JSONB NULL`

### 5. `research_evidence`

Purpose:

- Normalize evidence records for analysis and review

Columns:

- `evidence_id UUID PRIMARY KEY`
- `message_id UUID NOT NULL REFERENCES stage_messages(message_id)`
- `url TEXT NOT NULL`
- `title TEXT NOT NULL`
- `publisher TEXT NOT NULL`
- `published_at TIMESTAMPTZ NULL`
- `retrieved_at TIMESTAMPTZ NOT NULL`
- `source_tier TEXT NOT NULL`
- `relevance_score DOUBLE PRECISION NOT NULL`
- `claim_tag TEXT NOT NULL`

### 6. `analysis_candidates`

Purpose:

- Persist candidates and rejections in queryable form

Columns:

- `candidate_id UUID PRIMARY KEY`
- `message_id UUID NOT NULL REFERENCES stage_messages(message_id)`
- `symbol TEXT NOT NULL`
- `side TEXT NOT NULL`
- `status TEXT NOT NULL`
- `score DOUBLE PRECISION NOT NULL`
- `broker_eligible BOOLEAN NOT NULL`
- `max_notional NUMERIC(18,8) NOT NULL`
- `stop_loss_pct NUMERIC(18,8) NOT NULL`
- `features_jsonb JSONB NOT NULL`
- `filters_jsonb JSONB NOT NULL`

### 7. `portfolio_snapshots`

Purpose:

- Capture deterministic account state at analysis/execution time

Columns:

- `snapshot_id UUID PRIMARY KEY`
- `captured_at TIMESTAMPTZ NOT NULL`
- `account_id TEXT NOT NULL`
- `equity NUMERIC(18,8) NOT NULL`
- `cash NUMERIC(18,8) NOT NULL`
- `buying_power NUMERIC(18,8) NOT NULL`
- `gross_exposure NUMERIC(18,8) NOT NULL`
- `net_exposure NUMERIC(18,8) NOT NULL`
- `sector_exposure_jsonb JSONB NOT NULL`
- `positions_jsonb JSONB NOT NULL`

### 8. `approval_requests`

Purpose:

- Track human approval workflow

Columns:

- `approval_id UUID PRIMARY KEY`
- `trace_id UUID NOT NULL REFERENCES pipeline_traces(trace_id)`
- `candidate_id UUID NOT NULL`
- `channel TEXT NOT NULL`
- `status TEXT NOT NULL`
- `requested_at TIMESTAMPTZ NOT NULL`
- `expires_at TIMESTAMPTZ NOT NULL`
- `decided_at TIMESTAMPTZ NULL`
- `approver_id TEXT NULL`
- `decision_reason TEXT NULL`
- `callback_token TEXT NOT NULL UNIQUE`

### 9. `broker_orders`

Purpose:

- Record submitted and updated broker orders

Columns:

- `order_id UUID PRIMARY KEY`
- `trace_id UUID NOT NULL REFERENCES pipeline_traces(trace_id)`
- `candidate_id UUID NOT NULL`
- `client_order_id TEXT NOT NULL UNIQUE`
- `broker TEXT NOT NULL`
- `broker_order_id TEXT UNIQUE`
- `symbol TEXT NOT NULL`
- `side TEXT NOT NULL`
- `qty NUMERIC(18,8) NULL`
- `notional NUMERIC(18,8) NULL`
- `order_type TEXT NOT NULL`
- `tif TEXT NOT NULL`
- `submitted_at TIMESTAMPTZ NOT NULL`
- `status TEXT NOT NULL`
- `avg_fill_price NUMERIC(18,8) NULL`
- `stop_order_id TEXT NULL`
- `raw_response_jsonb JSONB NOT NULL`

### 10. `dead_letters`

Purpose:

- Preserve terminally failed jobs for replay and debugging

Columns:

- `dead_letter_id UUID PRIMARY KEY`
- `job_id UUID NOT NULL REFERENCES stage_jobs(job_id)`
- `stage TEXT NOT NULL`
- `reason TEXT NOT NULL`
- `payload_jsonb JSONB NOT NULL`
- `first_failed_at TIMESTAMPTZ NOT NULL`
- `last_failed_at TIMESTAMPTZ NOT NULL`
- `replay_count INT NOT NULL DEFAULT 0`
- `resolved_at TIMESTAMPTZ NULL`

### 11. `outcome_observations`

Purpose:

- Label downstream performance of signals and trades

Columns:

- `observation_id UUID PRIMARY KEY`
- `trace_id UUID NOT NULL REFERENCES pipeline_traces(trace_id)`
- `symbol TEXT NOT NULL`
- `horizon TEXT NOT NULL`
- `scheduled_for TIMESTAMPTZ NOT NULL`
- `observed_at TIMESTAMPTZ NULL`
- `entry_price NUMERIC(18,8) NOT NULL`
- `exit_price NUMERIC(18,8) NULL`
- `return_pct NUMERIC(18,8) NULL`
- `benchmark_return_pct NUMERIC(18,8) NULL`
- `label TEXT NULL`

---

## Queue And Idempotency Model

### Job States

All stage jobs move through the same generic job lifecycle:

- `queued`
- `leased`
- `running`
- `retry_wait`
- `succeeded`
- `dead_letter`
- `cancelled`

### Leasing Strategy

Workers lease jobs with `FOR UPDATE SKIP LOCKED`.

Each worker loop:

1. Select next available job by `stage`, `priority`, and `available_at`
2. Lease it for a bounded interval
3. Mark run start in `stage_runs`
4. Process
5. Write output message and next job in one transaction when possible
6. Mark success or failure

### Idempotency Keys

Required keys:

- Ingest: `scout:{alert_id}`
- Research: `research:{parent_message_id}:{prompt_version}`
- Analysis: `analysis:{parent_message_id}:{ruleset_version}:{data_snapshot_ts}`
- Execution: `execution:{parent_message_id}:{risk_ruleset_version}:{approval_mode}`
- Outcome: `outcome:{trace_id}:{horizon}`

### Broker Idempotency

`client_order_id` must be deterministic from:

- `trace_id`
- `candidate_id`
- `risk_ruleset_version`

This prevents duplicate order placement after retries or restarts.

### Replay Rules

- Replaying a raw Scout alert should not create a second trace if the original still exists and matches
- Replaying a dead-letter job creates a new run, not a new trace
- Replaying a stage should preserve `trace_id` and create a new `message_id`

---

## Per-Stage State Machines

### Ingress State Machine

```text
received
  -> verified
  -> persisted
  -> research_queued
  -> acknowledged

received
  -> invalid_signature

received
  -> invalid_payload

received
  -> duplicate
```

### Researcher State Machine

```text
queued
  -> researching
  -> pass

queued
  -> researching
  -> defer

queued
  -> researching
  -> insufficient_evidence

queued
  -> researching
  -> conflicting_evidence

researching
  -> retry_wait

researching
  -> dead_letter
```

Pass criteria:

- At least one primary source or two major press sources
- Source freshness inside configured window
- At least one tradable equity or ETF hypothesis
- No unresolved contradiction in the core catalyst

### Analyst State Machine

```text
queued
  -> loading_market_data
  -> scoring
  -> pass

queued
  -> loading_market_data
  -> scoring
  -> reject

queued
  -> loading_market_data
  -> scoring
  -> needs_review

loading_market_data
  -> retry_wait

loading_market_data
  -> dead_letter
```

Pass criteria:

- Symbol is broker-tradable
- Market session is regular-hours eligible
- Spread and slippage constraints pass
- Minimum liquidity and average volume pass
- Stop-loss distance is feasible
- Portfolio budget is available

### Executor State Machine

```text
queued
  -> risk_checking
  -> rejected_risk

queued
  -> risk_checking
  -> awaiting_approval

queued
  -> risk_checking
  -> submitting

awaiting_approval
  -> approved
  -> submitting

awaiting_approval
  -> rejected

awaiting_approval
  -> expired

submitting
  -> submitted
  -> filled

submitted
  -> partially_filled

submitted
  -> cancelled

submitted
  -> expired

submitting
  -> error
```

### Approval State Machine

```text
requested
  -> delivered
  -> approved

requested
  -> delivered
  -> rejected

requested
  -> expired

requested
  -> cancelled
```

### Outcome State Machine

```text
scheduled
  -> pending
  -> observed
  -> scored

pending
  -> missed_data
  -> retry_wait
```

---

## Researcher Design

### Inputs

- `IngestedAlert`
- Source freshness config
- Search provider config
- Prompt version

### Outputs

- `ResearchReport`
- `StageAudit`

### Search Strategy

Primary tools:

- Brave Search
- Serper fallback

Source preference:

- Primary sources first when available
- Major press second
- Secondary summaries only as support

Examples of primary sources:

- Government releases
- SEC filings
- Company IR announcements
- Court rulings
- Economic calendar releases

### Required Behaviors

- Deduplicate repeated URLs
- Penalize stale or conflicting sources
- Resolve ticker universe only to equities or ETFs in v1
- Explicitly mark unsupported claims instead of hallucinating certainty

---

## Analyst Design

### Inputs

- `ResearchReport`
- Broker symbol eligibility
- Market data snapshot
- Portfolio snapshot
- Ruleset version

### Outputs

- `AnalysisReport`
- `StageAudit`

### Deterministic Filters

Initial filter set:

- Instrument is tradable via broker
- Instrument is not halted
- Regular market session is open
- Spread below configured max
- Average daily dollar volume above configured minimum
- Price within allowed band
- Earnings proximity filter if relevant
- Position size and stop distance feasible

### Suggested Features

- Last price
- Session return
- RSI
- SMA20 / SMA50
- ATR or realized volatility
- Gap percentage
- Average daily volume
- Relative volume

Notes:

- Fundamentals like `P/E` may be best-effort only
- Do not make trade eligibility depend on fundamentals being present

---

## Executor Design

### Inputs

- `AnalysisReport`
- Portfolio snapshot
- Broker account state
- Approval mode
- Risk ruleset version

### Outputs

- `ExecutionDecision`
- `StageAudit`
- `broker_orders` records

### Approval Modes

- `manual_all`: every trade requires approval
- `paper_auto_live_manual`: paper auto, live manual
- `paper_auto_only`: paper only, no live path enabled

### Trade Construction Rules

- Long-only for v1
- Limit order only
- Day time-in-force only
- Every order includes stop-loss logic
- One new position per trace
- No pre-market or after-hours trading
- Cancel if approval or price snapshot becomes stale

### Hard-Coded Risk Limits

- Max single position: `5%` of account equity
- Max sector exposure: `20%`
- Max gross invested: `80%`
- Daily drawdown halt: `2%`
- Consecutive loss cooldown: `3 losses -> 1 hour`
- Quote freshness must pass
- Open orders must count against available budget

---

## Outcome Tracking

Every execution or rejected opportunity should later be observable for evaluation.

Required horizons:

- `t+15m`
- `t+1h`
- `next_session_close`

For each horizon capture:

- Entry price
- Exit or observed price
- Return percentage
- Benchmark return percentage
- Outcome label

Potential labels:

- `positive_alpha`
- `flat`
- `negative_alpha`
- `missed_move`
- `execution_not_applicable`

---

## Configuration

Add `pipeline.yaml` with sections for:

```yaml
app:
  env: dev
  log_level: INFO

database:
  url: ${PIPELINE_DATABASE_URL}
  pool_size: 10

queue:
  lease_sec: 60
  max_attempts: 5
  retry_backoff_sec: 5

researcher:
  http_host: 0.0.0.0
  http_port: 8001
  webhook_bearer_token: ${PIPELINE_WEBHOOK_BEARER_TOKEN}
  webhook_hmac_secret: ${PIPELINE_WEBHOOK_HMAC_SECRET}
  prompt_version: v1
  source_freshness_sec: 21600

analyst:
  ruleset_version: v1
  min_avg_dollar_volume: 2000000
  max_spread_bps: 50

executor:
  approval_mode: manual_all
  risk_ruleset_version: v1
  max_position_pct: 0.05
  max_sector_exposure_pct: 0.20
  max_gross_invested_pct: 0.80
  max_daily_drawdown_pct: 0.02
  consecutive_loss_limit: 3
  cooldown_minutes: 60
  stale_quote_sec: 30

broker:
  provider: alpaca
  paper: true
  api_key: ${ALPACA_API_KEY}
  api_secret: ${ALPACA_API_SECRET}

notifications:
  discord_webhook_url: ${DISCORD_WEBHOOK_URL}
  telegram_bot_token: ${TELEGRAM_BOT_TOKEN}
  telegram_chat_id: ${TELEGRAM_CHAT_ID}
```

---

## Build Sequence

### Phase 0 - Foundation

- Add `pipeline.yaml`
- Add Postgres service to `docker-compose.yaml`
- Add dependency groups to `pyproject.toml`
- Add `src/contracts/`
- Add `src/pipeline/db.py`, `queue.py`, `idempotency.py`
- Add SQL migrations for all pipeline tables

Exit criteria:

- Migrations apply cleanly
- A basic queue worker can lease and complete a test job

### Phase 1 - Ingress

- Build `src/researcher/server.py`
- Mirror Scout `ShiftAlert` as Pydantic
- Verify HMAC and headers using Scout-compatible logic
- Persist `pipeline_traces`, `stage_messages`, and `stage_jobs`
- Return `202 Accepted` quickly

Exit criteria:

- Replaying a saved Scout alert creates one trace and one research job
- Replaying the same alert again does not create duplicates

### Phase 2 - Researcher

- Build Brave and Serper wrappers
- Build source-tiering and freshness checks
- Build LLM orchestration and prompt versioning
- Emit `ResearchReport` and `StageAudit`
- Enqueue analysis only when `status=pass`

Exit criteria:

- Research worker handles real saved alerts
- Evidence is structured and queryable
- Conflicting evidence is surfaced explicitly

### Phase 3 - Analyst

- Build market data wrappers
- Build broker symbol eligibility lookup
- Build deterministic filters and ranking
- Persist `AnalysisReport`, `analysis_candidates`, and portfolio snapshot
- Enqueue execution only when `status=pass`

Exit criteria:

- Candidate generation is deterministic from a fixed input snapshot
- Rejected candidates carry explicit filter reasons

### Phase 4 - Executor

- Build broker abstraction with Alpaca paper implementation
- Build risk engine
- Build approval request flow
- Build deterministic `TradeIntent`
- Persist order lifecycle updates

Exit criteria:

- Duplicate retries do not create duplicate orders
- Approval timeout and stale-price rejection work correctly

### Phase 5 - Outcome Tracking

- Build outcome scheduler
- Capture horizons and benchmark comparison
- Persist labels and observations

Exit criteria:

- Each executed trade and each high-confidence rejected signal gets outcome rows

### Phase 6 - Integration

- Wire all services into `docker-compose.yaml`
- Add replay scripts for ingest and stage-level replay
- Run end-to-end paper workflow

Exit criteria:

- Full pipeline works from saved Scout alert to recorded outcome
- Dead-letter replay works without breaking trace continuity

---

## Verification Plan

### Unit Tests

- Contract validation and serialization
- Idempotency key generation
- Queue leasing and retry behavior
- Risk rules and circuit-breaker logic
- Approval token generation and expiry

### Integration Tests

- Ingest valid Scout alert and return `202`
- Reject bad HMAC
- Deduplicate duplicate alert delivery
- Research to analysis handoff
- Analysis to execution handoff
- Approval callback flow
- Broker submission with deterministic `client_order_id`

### Replay Tests

- Replay saved Scout alert JSON through ingress
- Replay dead-letter jobs back into their stage
- Replay analysis and execution from stored parent messages

### Paper Trading Validation

- Run pipeline in paper mode for weeks
- Track fills, slippage, stop behavior, and missed trades
- Compare signal outcomes vs benchmark

### Cost And Performance

- Record LLM token usage per stage
- Track stage latency per job
- Track dead-letter volume
- Track approval response latency

---

## Operational Guardrails

- Never block Scout on downstream research or execution latency
- Never let an LLM place an order without deterministic policy checks
- Never store raw chain-of-thought
- Never submit a trade if broker/account state is stale
- Never allow retries to place duplicate orders
- Never skip outcome tracking if the goal is long-term iteration

---

## Open Editing Knobs

These are expected to change as the system matures:

- Prompt versions
- Source freshness windows
- Research source-tier rules
- Analyst filter thresholds
- Risk percentages
- Approval timeout windows
- Outcome horizons
- Paper-to-live graduation criteria

---

## Definition Of Done For V1

V1 is complete when all of the following are true:

- Scout posts to the Researcher ingress with no code changes to Scout
- Ingress persists and enqueues work durably in Postgres
- Researcher produces structured catalyst reports with evidence
- Analyst produces deterministic tradable candidates and explicit rejections
- Executor applies deterministic risk rules and supports manual approval
- Alpaca paper orders can be placed without duplicate execution on retries
- Dead-letter jobs can be replayed safely
- Outcome observations are recorded for later evaluation
- End-to-end paper trading runs stably for an extended period
