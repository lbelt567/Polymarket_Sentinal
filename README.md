# Polymarket Sentinel

A real-time Polymarket prediction market tracker and multi-agent trading pipeline. Sentinel monitors price movements 24/7, detects significant shifts using a multi-timeframe waterfall, and feeds structured alerts into a four-stage AI agent pipeline (Researcher → Analyst → Executor → Outcome) that autonomously researches catalysts, evaluates trade opportunities, executes orders with risk controls, and tracks performance.

## What It Does

Sentinel connects to Polymarket's public APIs to:

1. **Discover markets** via the Gamma REST API (`/events` endpoint), filtering by volume, liquidity, and category
2. **Stream live prices** via the CLOB WebSocket, computing composite price signals from order book midpoints
3. **Detect shifts** using graduated thresholds across three timeframes (1min/3min/5min)
4. **Emit alerts** as structured JSON files, HTTP webhooks, Discord messages, or Telegram notifications
5. **Research catalysts** -- AI agent searches the web for news driving the shift, generates asset hypotheses
6. **Analyze opportunities** -- evaluates hypotheses with technical indicators (RSI, SMA, volatility) and tradability filters
7. **Execute trades** -- deterministic risk checks (position sizing, sector exposure, circuit breaker) then order submission
8. **Track outcomes** -- observes trade performance at T+15m, T+1h, and next session close

No API keys are needed for market data -- Polymarket's public endpoints are unauthenticated for read-only access. The trading pipeline requires API keys for web search (Brave/Serper), Claude (LLM reasoning), and your broker.

## Architecture

```
              LAYER A: INGESTION
┌─────────────────────────────────────────────────────────┐
│  Gatekeeper (REST, every 5 min)                         │
│  Discovers active markets, filters by volume/liquidity  │
│  Tracks canonical "Yes" token only per market           │
│                                                         │
│  WebSocket Client (persistent connection)               │
│  Composite price signal:                                │
│    1. Book midpoint (spread <= $0.10)                   │
│    2. Price change midpoint                             │
│    3. Last trade price (fallback)                       │
│  Reconnects with exponential backoff + jitter           │
└──────────────┬──────────────────────────────────────────┘
               │ asyncio.Queue[PriceTick]
               ▼
              LAYER B: PROCESSING
┌─────────────────────────────────────────────────────────┐
│  Shift Detector                                         │
│  SecondBar aggregation (1-sec OHLC bars)                │
│  Time-pruned buffers (6 min window, not fixed maxlen)   │
│                                                         │
│  Graduated thresholds:                                  │
│    L1 Scout:   |delta| > 8%  in 1 min  (log only)      │
│    L2 Confirm: |delta| > 5%  in 3 min  (notify)        │
│    L3 Trend:   |delta| > 5%  in 5 min  (decisive)      │
│                                                         │
│  Quality gates: spread, quote age, min ticks, staleness │
│  Confidence scoring: strong / moderate / weak           │
│  Cooldown: 5 min per (market, level)                    │
│                                                         │
│  Agent Alert Policy (post-detection filter):            │
│    - Confirmation + Trend only (no Scout)               │
│    - Excludes weak confidence & last_trade signals      │
│    - Price floor/ceiling ($0.10 - $0.98)                │
│    - Minimum absolute move ($0.02)                      │
│    - Min liquidity $25K for agent feed                  │
│    - Event-level dedup (1 per event per 15 min)         │
│                                                         │
│  SQLite hot buffer (WAL, last 48h)                      │
│  Archiver (hourly export to Parquet cold storage)       │
│  Notifications (JSON file / signed webhook / Discord /  │
│  Telegram)                                              │
└─────────────────────────────────────────────────────────┘
               │
               ▼
          data/archive/YYYY/MM/DD/HH.parquet
          (queryable via DuckDB for backtesting)
```

Single process, single event loop, 6 concurrent asyncio tasks. Memory footprint ~5 MB for 200 markets.

## Multi-Agent Trading Pipeline

When Sentinel emits an alert, it enters a four-stage pipeline where independent workers consume jobs from a lease-based queue:

```
              LAYER C: MULTI-AGENT PIPELINE
┌─────────────────────────────────────────────────────────┐
│                                                         │
│  ┌─────────────┐    ┌─────────────┐    ┌────────────┐  │
│  │  Researcher  │───▶│   Analyst   │───▶│  Executor  │  │
│  └─────────────┘    └─────────────┘    └─────┬──────┘  │
│   Web search,        RSI, SMA, vol,     Risk checks,   │
│   catalyst ID,       tradability        position size,  │
│   hypotheses         filters            circuit breaker │
│                                               │         │
│                                         ┌─────▼──────┐  │
│                                         │  Outcome    │  │
│                                         │  Observer   │  │
│                                         └────────────┘  │
│                                          T+15m, T+1h,   │
│                                          session close   │
│                                                         │
│  Infrastructure:                                        │
│    Queue ── lease-based jobs, retry backoff              │
│    PipelineDB ── audit trails, portfolio, traces        │
│    Approvals ── gated execution with timeout tokens     │
│    Contracts ── typed envelopes between stages          │
│    Idempotency ── replay-safe dedup keys                │
│    Circuit Breaker ── 2% daily drawdown / 3 loss limit  │
│    Tracing ── trace_id across all 6 stages              │
└─────────────────────────────────────────────────────────┘
```

### Pipeline Stages

**Researcher** (`python -m researcher`) -- Receives alerts and searches the web (Brave Search / Serper) for catalysts driving the price shift. Queries Polymarket context for related markets. Outputs a `ResearchReport` with catalyst type, evidence items, and 2-5 asset hypotheses with confidence levels.

**Analyst** (`python -m analyst`) -- Evaluates each hypothesis against live market conditions. Runs technical analysis (14-period RSI, SMA 20/50, 20-day realized volatility) and applies filters: tradable status, regular hours, price band $5-$500, spread < 50bps, minimum $2M daily volume. Outputs scored `TradeCandidate`s.

**Executor** (`python -m executor`) -- Performs deterministic risk checks before submitting orders:
- Position sizing: max 5% of equity per trade
- Sector exposure: max 20%
- Gross exposure: max 80%
- Quote freshness: max 30s
- Circuit breaker: halts on 2% daily drawdown or 3 consecutive losses

Orders use idempotent client order IDs to prevent duplicate submissions. Execution requires approval (configurable: manual or auto).

**Outcome Observer** (`python -m outcome`) -- Tracks trade performance at scheduled horizons (T+15m, T+1h, next session close). Records entry/exit prices, return percentage, and win/loss/breakeven labels. Reschedules if observation windows haven't arrived yet.

### Data Contracts

Each stage communicates through typed `PipelineEnvelope`s defined in `src/contracts/`:

| From | To | Contract |
|---|---|---|
| Sentinel | Researcher | `IngestedAlert` |
| Researcher | Analyst | `ResearchReport` (hypotheses + evidence) |
| Analyst | Executor | `TradeCandidate` (scored, filtered) |
| Executor | Outcome | `ExecutionDecision` (order IDs, fill prices) |
| Outcome | Archive | `OutcomeObservation` (returns, labels) |

Every envelope carries `message_id`, `trace_id`, `source_alert_id`, `idempotency_key`, and a `StageAudit` with model name, prompt version, token counts, latency, and cost.

## Alert Schema

Each alert is a self-contained JSON document designed for AI agent consumption:

```json
{
  "alert_id": "a1b2c3d4e5f6g7h8",
  "alert_level": "confirmation",
  "alert_level_label": "Confirmed Shift",
  "timestamp_iso": "2026-04-01T14:32:00+00:00",
  "timestamp_ms": 1775234520000,
  "market": {
    "asset_id": "71321044...",
    "question": "Will the Fed cut rates in June 2026?",
    "category": "Economics",
    "tags": ["fed", "interest-rates"],
    "event_title": "Federal Reserve June 2026 Meeting",
    "sibling_market_slugs": ["fed-hold-june-2026"],
    "end_date": "2026-06-15T18:00:00Z",
    "volume_24h": 250000.0,
    "liquidity": 180000.0,
    "polymarket_url": "https://polymarket.com/event/fed-june-2026"
  },
  "shift": {
    "direction": "up",
    "delta_pct": 7.2,
    "signed_delta_pct": 7.2,
    "price_start": 0.45,
    "price_end": 0.522,
    "window_sec": 180,
    "ticks_in_window": 47
  },
  "signal_quality": {
    "signal_source": "midpoint_book",
    "spread": 0.005,
    "liquidity_bucket": "high",
    "confidence": "strong"
  },
  "before_state": {
    "lookback_30m": {
      "window_sec": 1800,
      "price_start": 0.47,
      "price_end": 0.45,
      "net_delta_pct": -4.2553,
      "high": 0.48,
      "low": 0.44,
      "range_pct": 8.5106,
      "position_in_range": 0.25,
      "tick_count": 91,
      "avg_spread": 0.0075
    },
    "lookback_60m": {
      "window_sec": 3600,
      "price_start": 0.49,
      "price_end": 0.45,
      "net_delta_pct": -8.1633,
      "high": 0.5,
      "low": 0.44,
      "range_pct": 12.2449,
      "position_in_range": 0.1667,
      "tick_count": 151,
      "avg_spread": 0.0081
    },
    "activity": {
      "last_5m_tick_count": 18,
      "prior_30m_avg_5m_tick_count": 6.5,
      "tick_activity_ratio": 2.7692,
      "last_5m_avg_spread": 0.01,
      "prior_30m_avg_spread": 0.0074,
      "spread_change_pct": 35.1351
    },
    "regime_label": "recovery_move"
  }
}
```

`before_state` is only added to agent-facing alerts after policy filtering and event dedup. It is meant to help downstream agents distinguish breakout, recovery, pullback, and choppy-regime moves without shipping raw tick history.

## Quick Start

### Prerequisites

- Python 3.11+
- No API keys needed for market data

### Install

```bash
git clone https://github.com/lbelt567/Polymarket_Sentinal.git
cd Polymarket_Sentinal
pip install -e .
```

### Configure (optional)

The default `config.yaml` works out of the box. To enable Discord/Telegram notifications:

```bash
cp .env.example .env
# Edit .env with your webhook URLs / bot tokens
```

### Run

```bash
python -m sentinel
```

### What to Expect

| Time | What Happens |
|---|---|
| 0-5s | Config loads, SQLite initializes, WebSocket connects |
| 5-30s | Gatekeeper polls Gamma API, discovers ~100-200 markets |
| 30-60s | Ticks start flowing, metrics logging begins |
| 1-5 min | Warm-up period, detection windows filling |
| 5+ min | Full detection active, alerts written to `data/alerts/` |

### Verify It's Working

```bash
# Check tracked markets
sqlite3 data/sentinel.db "SELECT COUNT(*) FROM tracked_markets WHERE active=1;"

# Check signal source distribution
sqlite3 data/sentinel.db "SELECT signal_source, COUNT(*) FROM price_ticks GROUP BY signal_source;"

# Check for alerts
ls data/alerts/
```

## Configuration

All configuration lives in `config.yaml`. Key sections:

### Gatekeeper (Market Discovery)

```yaml
gatekeeper:
  poll_interval_sec: 300      # How often to re-scan markets
  min_volume_24h: 10000       # Minimum 24h volume ($)
  min_liquidity: 5000         # Minimum liquidity ($)
  max_markets: 200            # Cap on tracked markets
  excluded_categories: ["Sports"]
```

### Detector (Shift Detection)

```yaml
detector:
  cooldown_sec: 300           # 5 min cooldown per (market, level)
  stale_threshold_sec: 120    # Skip if no tick in 2 min
  max_spread_for_detection: 0.15
  thresholds:
    scout:        { window_sec: 60,  delta_pct: 8.0, min_ticks: 3  }
    confirmation: { window_sec: 180, delta_pct: 5.0, min_ticks: 5  }
    trend:        { window_sec: 300, delta_pct: 5.0, min_ticks: 10 }
```

### Notifications (Agent Feed Filtering)

```yaml
notifications:
  enabled_channels: ["json_file"]   # json_file, json_webhook, discord, telegram
  allowed_levels: ["confirmation", "trend"]
  excluded_confidences: ["weak"]
  excluded_signal_sources: ["last_trade"]
  min_price: 0.10
  max_price: 0.98
  min_liquidity: 25000
  min_abs_move: 0.02
  event_dedup_sec: 900              # One alert per event per 15 min
  json_file_retention_days: 30
```

### Storage Retention

```yaml
storage:
  hot_retention_hours: 48
  shift_event_retention_days: 30
  archive_retention_days: 90
  archive_interval_sec: 3600
```

Retention behavior:

- `price_ticks` stay in SQLite only for `storage.hot_retention_hours`
- `shift_events` are pruned after `storage.shift_event_retention_days`
- archived parquet and `*.markets.json` snapshots are pruned after `storage.archive_retention_days`
- JSON alert files are pruned after `notifications.json_file_retention_days`

### Agent Webhook

If your agent team is running as a service, enable `json_webhook` and point it at the agent ingress endpoint:

```yaml
notifications:
  enabled_channels: ["json_file", "json_webhook"]
  json_webhook_url: "https://agents.example.com/polymarket-alerts"
  json_webhook_bearer_token: "${JSON_WEBHOOK_BEARER_TOKEN}"
  json_webhook_hmac_secret: "${JSON_WEBHOOK_HMAC_SECRET}"
  json_webhook_timeout_sec: 10
  json_webhook_max_retries: 3
  json_webhook_retry_backoff_sec: 1.0
```

The webhook sender adds:

- `Authorization: Bearer ...` when configured
- `X-Sentinel-Alert-Id`
- `X-Sentinel-Alert-Level`
- `X-Sentinel-Timestamp-Ms`
- `X-Sentinel-Event-Slug` when available
- `X-Sentinel-Signature: sha256=...` when `json_webhook_hmac_secret` is set

That gives your downstream agents a durable local JSON outbox plus a signed HTTP delivery path.

## 24/7 Deployment

### Docker

Build and run on a VM:

```bash
docker build -t polymarket-sentinel .
docker run -d \
  --name polymarket-sentinel \
  --restart unless-stopped \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -v $(pwd)/.env:/app/.env:ro \
  polymarket-sentinel
```

The image includes a healthcheck using [scripts/healthcheck.py](/Users/luisbeltranjr/Downloads/Polymarket_Sentinal/scripts/healthcheck.py).

### systemd On A VM

If you prefer a plain VM process instead of Docker:

1. Create `/opt/polymarket-sentinel`
2. Copy the repo there and create a virtualenv at `/opt/polymarket-sentinel/.venv`
3. Install the package with `pip install -e .`
4. Copy [deploy/systemd/polymarket-sentinel.service](/Users/luisbeltranjr/Downloads/Polymarket_Sentinal/deploy/systemd/polymarket-sentinel.service) to `/etc/systemd/system/`
5. Run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-sentinel
sudo systemctl status polymarket-sentinel
```

### Health Checks

Use the bundled health script for cron, Docker, or external monitoring:

```bash
python scripts/healthcheck.py \
  --sqlite-path data/sentinel.db \
  --max-tick-age-sec 300 \
  --max-gatekeeper-age-sec 900 \
  --min-active-markets 25
```

It exits non-zero if market discovery is stale, ticks have stopped flowing, or active coverage collapses.

## Project Structure

```
Polymarket_Sentinal/
├── config.yaml                  # Runtime configuration
├── .env.example                 # Template for notification credentials
├── pyproject.toml               # Dependencies and build config
│
├── src/sentinel/                # Stage 0: Market monitoring
│   ├── __main__.py              # Entry point: python -m sentinel
│   ├── app.py                   # Orchestrator, 6 async tasks, shutdown
│   ├── config.py                # YAML + .env loading into dataclasses
│   ├── ingestion/
│   │   ├── gatekeeper.py        # Events API polling, market lifecycle
│   │   ├── ws_client.py         # WebSocket, composite price signal
│   │   └── models.py            # PriceTick, MarketMetadata, etc.
│   ├── processing/
│   │   ├── detector.py          # SecondBar deques, waterfall detection
│   │   ├── alerts.py            # ShiftAlert, AlertLevel, confidence
│   │   └── stream.py            # TickStream protocol (live + replay)
│   ├── storage/
│   │   ├── sqlite_store.py      # Hot buffer (WAL mode, async via to_thread)
│   │   ├── archiver.py          # SQLite -> Parquet hourly export
│   │   └── schema.sql           # DDL for price_ticks, shift_events, tracked_markets
│   ├── notifications/
│   │   ├── base.py              # AgentAlertPolicy, NotificationDispatcher
│   │   ├── json_webhook.py      # JSON file + HTTP webhook notifiers
│   │   ├── discord.py           # Discord webhook notifier
│   │   └── telegram.py          # Telegram bot notifier
│   └── utils/
│       ├── logging.py           # Structured JSON logging
│       └── metrics.py           # In-memory counters, periodic log
│
├── src/contracts/               # Typed data contracts between stages
│   ├── common.py                # PipelineEnvelope, StageAudit
│   ├── scout.py                 # IngestedAlert
│   ├── research.py              # ResearchReport, Hypothesis, Evidence
│   ├── analysis.py              # TradeCandidate, AnalysisReport
│   └── execution.py             # TradeIntent, RiskCheckResult, ExecutionDecision
│
├── src/pipeline/                # Shared pipeline infrastructure
│   ├── queue.py                 # Lease-based job queue with retry backoff
│   ├── db.py                    # PipelineDB: audit trails, portfolio, traces
│   ├── approvals.py             # Gated execution with timeout tokens
│   ├── config.py                # Pipeline configuration (risk rules, approval mode)
│   ├── runtime.py               # Worker runtime and lifecycle
│   ├── health.py                # Pipeline health checks
│   ├── tracing.py               # Cross-stage trace_id propagation
│   ├── idempotency.py           # Replay-safe dedup keys
│   ├── outcomes.py              # Outcome scheduling and observation
│   └── migrations/              # SQLite and Postgres schema migrations
│
├── src/researcher/              # Stage 1: Catalyst research
│   ├── agent.py                 # LLM-powered research agent
│   ├── worker.py                # Queue consumer
│   ├── server.py                # HTTP ingress for alerts
│   ├── tools/
│   │   ├── brave_search.py      # Brave Search API
│   │   ├── serper_search.py     # Serper Search API
│   │   └── polymarket.py        # Polymarket context lookups
│   └── prompts/                 # System prompts and templates
│
├── src/analyst/                 # Stage 2: Technical analysis
│   ├── agent.py                 # LLM-powered analyst agent
│   ├── worker.py                # Queue consumer
│   ├── filters.py               # Tradability filters
│   ├── tools/
│   │   ├── market_data.py       # Live quotes, sector info
│   │   └── technicals.py        # RSI, SMA, volatility
│   └── prompts/                 # System prompts and templates
│
├── src/executor/                # Stage 3: Order execution
│   ├── agent.py                 # LLM-powered executor agent
│   ├── worker.py                # Queue consumer
│   ├── broker.py                # Broker integration
│   ├── submission.py            # Idempotent order submission
│   ├── server.py                # HTTP server for approvals
│   ├── risk/
│   │   ├── limits.py            # Position, sector, exposure limits
│   │   └── circuit_breaker.py   # Drawdown and loss streak protection
│   └── prompts/                 # System prompts and templates
│
├── src/outcome/                 # Stage 4: Performance tracking
│   ├── worker.py                # Scheduled observation consumer
│   └── pricing.py               # Entry/exit price comparison
│
├── tests/                       # pytest suite
├── scripts/
│   └── backtest.py              # DuckDB replay over Parquet archive
└── data/                        # gitignored runtime data
    ├── sentinel.db              # SQLite hot buffer
    ├── alerts/                  # JSON alert files
    └── archive/                 # Parquet cold storage
```

## Storage

**Hot buffer:** SQLite in WAL mode, retains last 48 hours of price ticks. All DB operations are wrapped with `asyncio.to_thread()` to avoid blocking the event loop.

**Cold archive:** Parquet files at `data/archive/YYYY/MM/DD/HH.parquet`, exported hourly. Queryable with DuckDB:

```python
import duckdb
duckdb.sql("SELECT * FROM read_parquet('data/archive/**/*.parquet') WHERE asset_id = '...' ORDER BY ts_ms")
```

## Backtesting

```bash
python scripts/backtest.py --parquet-glob "data/archive/**/*.parquet" --speed 10.0
```

Replays archived data through the same detection logic using a `VirtualClock` for deterministic timing.

## Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

Covers: detector delta computation, warm-up grace period, gatekeeper filtering, WebSocket message parsing, composite price signal, SQLite round-trips, archiver Parquet export, notification dispatch/isolation, pipeline queue leasing, researcher ingress and agent, analyst technicals and filters, executor risk checks and submission, outcome observations, approval flow, pipeline config, and runtime lifecycle.

## Design Decisions

- **Events API over Markets API**: Events nest markets, giving natural grouping, sibling metadata, and fewer API calls.
- **Canonical "Yes" token only**: Each market has Yes/No tokens with complementary prices. Tracking both produces duplicate mirror alerts.
- **SecondBar aggregation**: 1-second OHLC bars instead of raw tick deques. Prevents data loss during traffic bursts that would overflow fixed-size buffers.
- **Time-pruned buffers**: Deques pruned by timestamp (6 min), not fixed `maxlen`. Guarantees all three detection windows have complete data regardless of tick rate.
- **Composite price signal**: Mirrors Polymarket's own UI logic -- midpoint when spread is tight, last trade as fallback. Each tick tagged with `signal_source` for quality assessment.
- **Two-tier output**: Raw alerts stored in SQLite for analysis. Agent feed filtered by confidence, signal source, price range, liquidity, and event dedup.
- **Clock injection**: `time_fn: Callable[[], int]` threaded through all components enables deterministic testing and replay.
- **Lease-based queue**: Workers lease jobs with configurable retry backoff instead of pub/sub, ensuring at-least-once delivery and graceful worker restarts.
- **Typed contracts**: Each stage communicates through versioned dataclass envelopes (`src/contracts/`), making inter-stage interfaces explicit and testable.
- **Idempotent execution**: Stable client order IDs derived from message lineage prevent duplicate orders on replay or retry.
- **Circuit breaker**: Autonomous trading halts automatically on 2% daily drawdown or 3 consecutive losses -- a hard safety net independent of LLM reasoning.
- **Approval gates**: Execution requires explicit approval (manual or auto), adding a human-in-the-loop checkpoint before real capital is deployed.

## License

MIT
