# Decisive Shift Engine - Implementation Plan (v2)

## Context

Build a 24/7 Polymarket sentiment tracker from scratch (greenfield project). The system monitors prediction market price movements in real-time, detects significant shifts using a multi-timeframe waterfall, and emits structured alerts. A cold archive supports backtesting.

**Downstream consumer:** An AI agent suite will ingest the structured JSON alerts to research broader market impact and decide on trades. Every alert must be self-contained and rich enough for an agent to act on without calling back to the tracker.

**Key discovery: Polymarket's public market data requires NO API keys or authentication.** Both the REST API (Gamma) and WebSocket (CLOB WS) are freely accessible for read-only data. You only need credentials for Discord/Telegram notifications.

---

## What You Need to Gather

| Credential | How to Get | Cost |
|---|---|---|
| Discord Webhook URL | Server Settings > Integrations > Webhooks > New Webhook | Free |
| Telegram Bot Token | Message @BotFather on Telegram, `/newbot` | Free |
| Telegram Chat ID | Message @userinfobot after creating bot | Free |
| JSON Webhook URL (optional) | Your AI agent suite's HTTP endpoint, or write to a local JSON file | Free |
| **No Polymarket API key** | Public endpoints are unauthenticated | Free |

---

## Architecture

```
              LAYER A: INGESTION
┌───────────────────────────────────────────────────────┐
│  Gatekeeper (every 5 min)                             │
│  GET gamma-api.polymarket.com/events                  │
│    ?active=true&closed=false&order=volume_24hr         │
│  Extract markets from nested event response            │
│  Filters:                                             │
│    - volume24hr >= $10K                               │
│    - category NOT IN excluded_categories (Sports)      │
│    - active=true, closed=false                        │
│    - min_liquidity >= $5K                             │
│  Tracks: canonical "Yes" token only per market         │
│    (resolved by matching outcome label ↔ token id,     │
│     never by array position alone)                     │
│  Lifecycle: prune resolved/closed, add new markets     │
│    from REST + immediate WS market_resolved handling   │
│  Stores: category, tags, description, event_id,       │
│          end_date, sibling market ids                  │
│  Output: subscribe/unsubscribe asset_ids               │
│                                                       │
│  WS Client (persistent connection)                    │
│  wss://ws-subscriptions-clob.polymarket.com/ws/market │
│  Heartbeat: "PING" text every 10s                     │
│  Subscribe with custom_feature_enabled=true           │
│    (required for market_resolved and best_bid_ask)    │
│  Price signal (composite, priority order):             │
│    1. Midpoint from book bids[0]/asks[0] if spread    │
│       ≤ $0.10                                         │
│    2. Midpoint from price_change best_bid/best_ask    │
│    3. last_trade_price as fallback                    │
│  Emits: PriceTick with signal_source tag + quote_ts    │
│  Reconnect: exponential backoff + jitter              │
│  Output: asyncio.Queue[PriceTick]                     │
└──────────────┬────────────────────────────────────────┘
               │
               ▼
              LAYER B: PROCESSING
┌───────────────────────────────────────────────────────┐
│  Shift Detector                                       │
│  dict[asset_id → second-bars] (time-bucketed)         │
│  Store 1-second aggregates for 6 minutes max          │
│  Avoid raw-tick hard-cap truncation under load        │
│                                                       │
│  Every 1 sec, scan all deques:                        │
│    L1 Scout:  |ΔP|>8%  in 1min, min 3 ticks → log   │
│    L2 Confirm: |ΔP|>5% in 3min, min 5 ticks → notify │
│    L3 Trend:  |ΔP|>3%  in 5min, min 10 ticks → alert │
│  Quality gate: skip if spread > $0.15, quote stale,   │
│    or market stale > 2min                             │
│  Warm-up: allow last_trade-only detection for newly   │
│    subscribed markets for <= 60s with weak confidence │
│  Cooldown: 5 min per (market_id, level)               │
│  Tracks: signed delta, direction, tick count, spread, │
│    quote freshness                                    │
│                                                       │
│  SQLite Hot Buffer (WAL mode, last 48h)               │
│  Archiver (hourly → Parquet cold storage)             │
│  Notifier (JSON webhook / Discord / Telegram)         │
│  Metrics (in-memory counters, logged every 60s)       │
└──────────────────────────────────────────────────────┘
               │
               ▼
          data/archive/YYYY/MM/DD/HH.parquet
          (queryable via DuckDB for backtesting)
```

**Single process, single event loop, 6 concurrent asyncio tasks** (added: metrics logger). Memory footprint remains small because detector state stores second-level bars rather than unbounded raw ticks.

---

## Critical Design Decisions (Mentor Review)

### 1. Events API for Discovery (not /markets)

The Gamma API's `/events` endpoint is the documented efficient path. Events nest markets inside them, which gives us:
- Natural grouping (sibling markets in same event)
- Event-level metadata (title, slug, end_date)
- Fewer API calls (one event contains multiple markets)

```
GET https://gamma-api.polymarket.com/events?active=true&closed=false&order=volume_24hr&ascending=false&limit=100&offset=0
```

Response nests markets inside events. We extract individual markets from each event, pulling both event-level and market-level metadata.

### 2. Time-Bucketed Buffers (not fixed maxlen raw ticks)

**Problem with `deque(maxlen=300)`:** A busy market receiving 10 ticks/sec fills 300 slots in 30 seconds and silently breaks the 3-minute and 5-minute windows.

**Problem with a raw-tick hard cap:** A plain `deque()` plus hard cap still drops valid in-window history during bursts. That means correctness degrades exactly when the market is most active.

**Solution:** Aggregate incoming ticks into 1-second bars per asset:
- Keep one bar per second for the last 6 minutes
- Each bar stores opening price, closing price, tick count, latest spread, and latest quote timestamp
- Detection windows run on second-bars, not raw ticks
- This keeps memory bounded while preserving full 1m/3m/5m coverage even in high-throughput markets

```python
@dataclass(slots=True)
class SecondBar:
    second_ts_ms: int
    price_open: float
    price_last: float
    tick_count: int
    spread_last: float | None
    quote_ts_ms: int | None

def _upsert_bar(self, bars: deque[SecondBar], tick: PriceTick) -> None:
    bucket_ms = (tick.timestamp_ms // 1000) * 1000
    if bars and bars[-1].second_ts_ms == bucket_ms:
        bar = bars[-1]
        bar.price_last = tick.price
        bar.tick_count += 1
        if tick.spread is not None:
            bar.spread_last = tick.spread
            bar.quote_ts_ms = tick.quote_ts_ms
    else:
        bars.append(
            SecondBar(
                second_ts_ms=bucket_ms,
                price_open=tick.price,
                price_last=tick.price,
                tick_count=1,
                spread_last=tick.spread,
                quote_ts_ms=tick.quote_ts_ms,
            )
        )

def _prune_bars(self, bars: deque[SecondBar]) -> None:
    cutoff_ms = current_time_ms() - (360 * 1000)
    while bars and bars[0].second_ts_ms < cutoff_ms:
        bars.popleft()
```

### 3. Composite Price Signal (not last_trade_price only)

Polymarket's own UI uses midpoint when spread ≤ $0.10, falling back to last trade when spread is wide. We mirror this logic:

**Priority order:**
1. **Book midpoint** from `book` events: `(float(bids[0]["price"]) + float(asks[0]["price"])) / 2` when both sides exist and spread ≤ $0.10
2. **Price change midpoint** from `price_change` events: `(float(best_bid) + float(best_ask)) / 2` -- these events explicitly include best_bid/best_ask
3. **Last trade price** from `last_trade_price` events -- fallback for thin/illiquid markets

Each PriceTick records its `signal_source` ("midpoint_book", "midpoint_change", "last_trade") so the detector and alerts can report signal quality.

```python
@dataclass(slots=True)
class PriceTick:
    asset_id: str
    price: float
    best_bid: float | None    # None if source is last_trade
    best_ask: float | None
    spread: float | None
    signal_source: str         # "midpoint_book" | "midpoint_change" | "last_trade"
    quote_ts_ms: int | None    # Timestamp of quote data used to derive spread/midpoint
    timestamp_ms: int
```

**Warm-up behavior for new subscriptions:** newly tracked markets may receive `last_trade_price` events before the first quote-bearing `book` or `price_change` event arrives. During a short warm-up window (default: 60 seconds from subscription), the detector may use `last_trade`-based bars if volume/liquidity filters already passed and min-tick thresholds are met. Any alert emitted in this window is forced to `confidence="weak"`. Once a fresh quote arrives, normal quote freshness rules apply.

### 4. Canonical "Yes" Token Only

Each market has exactly 2 tokens (Yes and No). Their prices are complementary (sum to ~$1.00). Tracking both would produce duplicate/mirror alerts.

**Decision:** Track only the canonical "Yes" token's asset_id per market, but do **not** assume array position. The token must be resolved by matching the market's outcomes/outcome labels to the corresponding token ids returned by the API. If the Yes-token mapping is missing or ambiguous, skip that market and log it for review.

A price going UP on the Yes token means the event is becoming more likely. A price going DOWN means less likely. This gives us a single, unambiguous directional signal per market.

### 5. Graduated Thresholds with Quality Gates

A flat 5% across all windows is too blunt. Shorter windows need higher thresholds (noise is higher), longer windows can catch subtler moves:

| Level | Window | ΔP Threshold | Min Ticks | Effect |
|---|---|---|---|---|
| Scout | 1 min | 8% | 3 | Log only |
| Confirmation | 3 min | 5% | 5 | Notify |
| Trend | 5 min | 3% | 10 | Decisive Shift |

**Quality gates** (skip detection if any fail):
- Spread > $0.15 → market too illiquid for reliable signal
- Last tick older than 2 minutes → stale data, wait for fresh ticks
- Fewer than `min_ticks` in window → insufficient trading activity

### 6. Market Lifecycle Handling

The gatekeeper runs every 5 minutes and handles:

| Event | Action |
|---|---|
| New market passes filters | Add to tracked_markets, subscribe WS |
| Market volume drops below $10K | Unsubscribe WS, keep in DB (may recover) |
| Market resolved via WS `market_resolved` | Immediately unsubscribe WS, prune detector state, mark inactive in DB |
| Market closed/resolved on REST refresh | Reconcile state, mark inactive if WS event was missed |
| Market reappears after being below threshold | Re-subscribe WS, resume tracking |
| WS sends no ticks for market for 30+ min | Mark stale, skip detection (don't unsubscribe -- may resume) |

The detector checks `tracked_markets.active` before processing. Dead market state is pruned to free memory, and the gatekeeper remains the reconciliation layer if WS lifecycle events are missed.

**WS subscription requirement:** `market_resolved` and `best_bid_ask` require `custom_feature_enabled: true` in the market-channel subscription payload. REST reconciliation remains mandatory because WS lifecycle events may still be missed in production.

---

## Gatekeeper Filters

```yaml
gatekeeper:
  gamma_api_url: "https://gamma-api.polymarket.com"
  endpoint: "/events"             # Use events, not markets
  poll_interval_sec: 300
  min_volume_24h: 10000
  min_liquidity: 5000             # NEW: minimum liquidity filter
  max_markets: 200
  excluded_categories: ["Sports"]
  stale_timeout_sec: 1800         # 30 min no ticks → mark stale
```

Filtering cascade:
1. `active=true, closed=false` (API-level)
2. `category not in excluded_categories` (Sports excluded)
3. `volume_24h >= min_volume_24h` ($10K)
4. `liquidity >= min_liquidity` ($5K)
5. Take top `max_markets` by volume after filtering

---

## Project Structure

```
Polymarket_Sentinal/
├── pyproject.toml
├── .env.example
├── config.yaml
├── src/
│   └── sentinel/
│       ├── __init__.py
│       ├── __main__.py           # Entry: python -m sentinel
│       ├── app.py                # Orchestrator, signal handling, shutdown
│       ├── config.py             # Load config.yaml + .env → dataclasses
│       ├── ingestion/
│       │   ├── __init__.py
│       │   ├── gatekeeper.py     # Events API polling, market discovery, lifecycle
│       │   ├── ws_client.py      # WebSocket client, composite price signal
│       │   └── models.py         # Pydantic models for API/WS messages
│       ├── processing/
│       │   ├── __init__.py
│       │   ├── detector.py       # Time-pruned deques, graduated waterfall
│       │   ├── alerts.py         # ShiftAlert dataclass, AlertLevel enum
│       │   └── stream.py         # Generic tick stream interface (live + replay)
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── sqlite_store.py   # Hot buffer with batched writes
│       │   ├── archiver.py       # SQLite → Parquet export + prune
│       │   └── schema.sql        # DDL for tables + indexes
│       ├── notifications/
│       │   ├── __init__.py
│       │   ├── base.py           # Notifier protocol + dispatch
│       │   ├── discord.py        # Discord webhook (human-readable)
│       │   ├── telegram.py       # Telegram bot (human-readable)
│       │   └── json_webhook.py   # Structured JSON for AI agents
│       └── utils/
│           ├── __init__.py
│           ├── logging.py        # Structured logging setup
│           └── metrics.py        # In-memory counters + periodic logger
├── tests/
│   ├── conftest.py
│   ├── test_detector.py
│   ├── test_gatekeeper.py
│   ├── test_sqlite_store.py
│   ├── test_archiver.py
│   ├── test_ws_parser.py
│   └── test_price_signal.py      # NEW: composite price signal tests
├── scripts/
│   └── backtest.py               # DuckDB query tool over Parquet archive
└── data/                          # gitignored runtime data
    ├── sentinel.db
    ├── alerts/                    # JSON alert files (one per alert)
    └── archive/
```

---

## Alert Schema (AI Agent Contract)

```json
{
  "alert_id": "a1b2c3d4",
  "alert_level": "trend",
  "alert_level_label": "Decisive Shift",
  "timestamp_iso": "2026-04-01T14:32:00Z",
  "timestamp_ms": 1775234520000,
  "market": {
    "asset_id": "71321044878926...",
    "condition_id": "0xabc123...",
    "market_id": "mkt_789",
    "question": "Will the Fed cut rates in June 2026?",
    "description": "This market resolves YES if the Federal Reserve announces a rate cut at its June 2026 FOMC meeting...",
    "category": "Economics",
    "tags": ["fed", "interest-rates", "monetary-policy"],
    "event_id": "evt_456",
    "event_title": "Federal Reserve June 2026 Meeting",
    "event_slug": "fed-june-2026",
    "sibling_market_slugs": ["fed-hold-june-2026", "fed-hike-june-2026"],
    "outcome": "Yes",
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
    "price_current": 0.522,
    "window_sec": 300,
    "ticks_in_window": 47
  },
  "signal_quality": {
    "signal_source": "midpoint_book",
    "best_bid": 0.52,
    "best_ask": 0.525,
    "spread": 0.005,
    "quote_age_sec": 1.2,
    "liquidity_bucket": "high",
    "confidence": "strong"
  }
}
```

**New fields vs v1:**
- `signal_quality.signal_source`: "midpoint_book" | "midpoint_change" | "last_trade" -- tells agents how reliable the price signal is
- `signal_quality.best_bid` / `best_ask` / `spread`: current orderbook state at alert time
- `signal_quality.quote_age_sec`: age of the quote used for spread/midpoint checks
- `signal_quality.liquidity_bucket`: "high" (>$100K), "medium" ($25K-$100K), "low" ($5K-$25K)
- `signal_quality.confidence`: "strong" and "moderate" require fresh midpoint-backed quotes; warm-up or `last_trade` fallback is always "weak"
- `market.end_date`: when the market closes -- agents need this for time-sensitive decisions
- `market.sibling_market_slugs`: other markets in the same event for correlated analysis
- `market.event_title`: human-readable event name for agent research context

**Confidence scoring logic:**
```
if signal_source.startswith("midpoint") and quote_age_sec <= 5 and spread <= 0.03 and liquidity_bucket == "high" and ticks >= 10:
    confidence = "strong"
elif signal_source.startswith("midpoint") and quote_age_sec <= 15 and spread <= 0.10 and ticks >= 5:
    confidence = "moderate"
else:
    confidence = "weak"
```

**Delivery modes** (configurable, not mutually exclusive):
1. **JSON file:** Write each alert as `data/alerts/{timestamp}_{asset_id}.json` -- simplest, agents poll the directory
2. **HTTP POST:** POST to a configurable URL endpoint -- for when agents run as a server
3. **Discord/Telegram:** Human-readable summaries for your own monitoring

---

## Storage Design

**Hot buffer (SQLite, WAL mode):**
```sql
CREATE TABLE price_ticks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id        TEXT    NOT NULL,
    market_slug     TEXT    NOT NULL,
    price           REAL    NOT NULL,
    best_bid        REAL,               -- NULL if source is last_trade
    best_ask        REAL,
    spread          REAL,
    signal_source   TEXT    NOT NULL,    -- 'midpoint_book' | 'midpoint_change' | 'last_trade'
    quote_ts_ms     INTEGER,             -- NULL if source has no quote backing
    ts_ms           INTEGER NOT NULL
);
CREATE INDEX idx_price_ticks_asset_ts ON price_ticks (asset_id, ts_ms DESC);
CREATE INDEX idx_price_ticks_ts ON price_ticks (ts_ms);

CREATE TABLE shift_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id            TEXT    NOT NULL,
    market_slug         TEXT    NOT NULL,
    question            TEXT    NOT NULL,
    category            TEXT    NOT NULL,
    level               TEXT    NOT NULL,    -- 'scout' | 'confirmation' | 'trend'
    direction           TEXT    NOT NULL,    -- 'up' | 'down'
    delta_pct           REAL    NOT NULL,    -- always positive
    signed_delta_pct    REAL    NOT NULL,    -- positive=up, negative=down
    price_start         REAL    NOT NULL,
    price_end           REAL    NOT NULL,
    window_sec          INTEGER NOT NULL,
    ticks_in_window     INTEGER NOT NULL,
    signal_source       TEXT    NOT NULL,
    spread_at_alert     REAL,
    quote_age_sec       REAL,
    confidence          TEXT    NOT NULL,    -- 'strong' | 'moderate' | 'weak'
    ts_ms               INTEGER NOT NULL
);
CREATE INDEX idx_shift_events_ts ON shift_events (ts_ms DESC);
CREATE INDEX idx_shift_events_category ON shift_events (category, ts_ms DESC);

CREATE TABLE tracked_markets (
    asset_id    TEXT PRIMARY KEY,
    market_id   TEXT NOT NULL,
    condition_id TEXT NOT NULL DEFAULT '',
    market_slug TEXT NOT NULL,
    question    TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '[]',   -- JSON array
    event_id    TEXT NOT NULL DEFAULT '',
    event_title TEXT NOT NULL DEFAULT '',
    event_slug  TEXT NOT NULL DEFAULT '',
    sibling_ids TEXT NOT NULL DEFAULT '[]',   -- JSON array of sibling asset_ids
    outcome     TEXT NOT NULL,                -- always 'Yes' (canonical token)
    end_date    TEXT,                         -- market close date ISO
    volume_24h  REAL NOT NULL,
    liquidity   REAL NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    subscribed_at_ms INTEGER NOT NULL DEFAULT 0,
    last_tick_ms INTEGER NOT NULL DEFAULT 0,  -- for staleness detection
    last_update INTEGER NOT NULL
);
```

**Cold archive:** Parquet files at `data/archive/YYYY/MM/DD/HH.parquet`, queryable via DuckDB with `read_parquet('data/archive/**/*.parquet')`.

**Pragmas:** `journal_mode=WAL`, `synchronous=NORMAL`, `cache_size=-64000`

---

## Key Algorithm: Delta Computation

```python
def _compute_delta(
    bars: deque[SecondBar], window_sec: int, min_ticks: int
) -> tuple[float, float, float, float, int] | None:
    """Returns (delta_pct, signed_delta_pct, start_price, end_price, tick_count) or None.
    Uses 1-second bars to preserve full windows under high throughput."""
    if len(bars) < 2:
        return None
    now_ms = bars[-1].second_ts_ms
    cutoff_ms = now_ms - (window_sec * 1000)
    start_price = None
    tick_count = 0
    for bar in bars:
        if bar.second_ts_ms >= cutoff_ms:
            if start_price is None:
                start_price = bar.price_open
            tick_count += bar.tick_count
    if start_price is None or start_price == 0:
        return None
    if tick_count < min_ticks:
        return None  # Quality gate: not enough trading activity
    end_price = bars[-1].price_last
    signed_delta = (end_price - start_price) / start_price * 100.0
    delta_pct = abs(signed_delta)
    return (delta_pct, signed_delta, start_price, end_price, tick_count)
```

**Quality gates applied before detection:**
```python
def _should_skip(self, asset_id: str, bars: deque[SecondBar]) -> bool:
    # Stale: no tick in last 2 minutes
    if current_time_ms() - bars[-1].second_ts_ms > 120_000:
        return True
    latest_with_quote = next((b for b in reversed(bars) if b.spread_last is not None), None)
    if latest_with_quote is None:
        market = self._store.get_tracked_market(asset_id)
        if market and current_time_ms() - market.subscribed_at_ms <= 60_000:
            return False  # Warm-up: allow trade-only detection with weak confidence
        return True
    quote_age_ms = current_time_ms() - (latest_with_quote.quote_ts_ms or 0)
    if quote_age_ms > 15_000:
        return True
    if latest_with_quote.spread_last > 0.15:
        return True
    # Market inactive
    market = self._store.get_tracked_market(asset_id)
    if market and not market.active:
        return True
    return False
```

---

## Generic Stream Interface (Replay/Backfill Support)

The detector consumes a `TickStream` protocol, not a raw WebSocket. This allows the same detection logic to run on live data and historical replays:

```python
from typing import Protocol, AsyncIterator

class TickStream(Protocol):
    async def ticks(self) -> AsyncIterator[PriceTick]: ...

class LiveStream:
    """Wraps asyncio.Queue fed by ws_client."""
    def __init__(self, queue: asyncio.Queue[PriceTick]) -> None: ...
    async def ticks(self) -> AsyncIterator[PriceTick]:
        while True:
            yield await self._queue.get()

class ReplayStream:
    """Reads from Parquet archive via DuckDB for backtesting."""
    def __init__(self, parquet_glob: str, speed: float = 1.0) -> None: ...
    async def ticks(self) -> AsyncIterator[PriceTick]:
        # Query DuckDB, yield rows as PriceTick, respect original timing * speed
        ...
```

The detector's `_ingest_loop` consumes `async for tick in stream.ticks()` -- same code path for live and replay. The `backtest.py` script uses `ReplayStream` + `ShiftDetector` to validate detection logic against historical data.

---

## Observability

In-memory counters logged every 60 seconds:

```python
@dataclass
class Metrics:
    tracked_markets: int = 0          # Currently tracked
    ws_reconnects: int = 0            # Total since start
    ticks_ingested: int = 0           # Total ticks received
    ticks_per_minute: int = 0         # Rolling 1-min rate
    ticks_dropped_parse_error: int = 0
    ticks_dropped_stale: int = 0
    ticks_dropped_quote_stale: int = 0
    market_resolved_ws_events: int = 0
    alerts_scout: int = 0
    alerts_confirmation: int = 0
    alerts_trend: int = 0
    alerts_skipped_cooldown: int = 0
    alerts_skipped_quality: int = 0   # Skipped by quality gates
    archive_lag_sec: float = 0        # Time since last successful archive
    last_gatekeeper_run_iso: str = ""
    signal_source_counts: dict = field(default_factory=dict)  # midpoint_book: N, last_trade: M
```

Logged as structured JSON to both stdout and `data/sentinel.log` every 60 seconds. This is essential for debugging 24/7 operation -- you'll know immediately if tick rates drop, reconnects spike, or quality gates are rejecting everything.

---

## Configuration

**`config.yaml`:**
```yaml
gatekeeper:
  gamma_api_url: "https://gamma-api.polymarket.com"
  endpoint: "/events"
  poll_interval_sec: 300
  min_volume_24h: 10000
  min_liquidity: 5000
  max_markets: 200
  excluded_categories: ["Sports"]
  stale_timeout_sec: 1800

websocket:
  url: "wss://ws-subscriptions-clob.polymarket.com/ws/market"
  heartbeat_interval_sec: 10
  reconnect_base_delay_sec: 1
  reconnect_max_delay_sec: 60
  custom_feature_enabled: true   # Required for market_resolved and best_bid_ask
  max_spread_for_midpoint: 0.10
  max_quote_age_sec: 15           # Ignore stale quotes when deriving midpoint/spread

detector:
  check_interval_sec: 1
  buffer_max_age_sec: 360         # Prune bars older than 6 min
  bar_interval_sec: 1             # Aggregate ticks into 1-second bars
  cooldown_sec: 300
  stale_threshold_sec: 120        # Skip if no tick in 2 min
  max_spread_for_detection: 0.15  # Skip if spread too wide
  max_quote_age_sec: 15           # Skip if latest quote is stale
  warmup_grace_sec: 60            # Allow weak-confidence trade-only detection right after subscribe
  thresholds:
    scout:
      window_sec: 60
      delta_pct: 8.0
      min_ticks: 3
    confirmation:
      window_sec: 180
      delta_pct: 5.0
      min_ticks: 5
    trend:
      window_sec: 300
      delta_pct: 3.0
      min_ticks: 10

storage:
  sqlite_path: "data/sentinel.db"
  hot_retention_hours: 48
  archive_dir: "data/archive"
  archive_interval_sec: 3600

notifications:
  enabled_channels: ["json_file", "discord"]
  min_severity: "confirmation"
  json_file_dir: "data/alerts"
  json_webhook_url: ""
  discord_webhook_url: "${DISCORD_WEBHOOK_URL}"
  telegram_bot_token: "${TELEGRAM_BOT_TOKEN}"
  telegram_chat_id: "${TELEGRAM_CHAT_ID}"

metrics:
  log_interval_sec: 60

logging:
  level: "INFO"
  file: "data/sentinel.log"
```

**`.env.example`:**
```
DISCORD_WEBHOOK_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

---

## Dependencies

```toml
[project]
name = "polymarket-sentinel"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "websockets>=13.0",
    "aiohttp>=3.9",
    "aiosqlite>=0.20",
    "pyarrow>=14.0",
    "duckdb>=1.0",
    "pydantic>=2.0",
    "pyyaml>=6.0",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]
```

---

## Build Sequence

### Phase 1: Foundation
1. `pyproject.toml` + project scaffolding (all directories + `__init__.py` files)
2. `config.yaml` + `.env.example` + `config.py` (dataclass-based config loading)
3. `utils/logging.py` (structured logging to file + stdout)
4. `utils/metrics.py` (in-memory counters + periodic logger)
5. `storage/schema.sql` + `sqlite_store.py` (WAL mode, batched inserts, enriched schema)
6. Unit tests for SQLite store

### Phase 2: Ingestion
7. `ingestion/models.py` (Pydantic models for Events API response + WS events including book/price_change/last_trade/market_resolved)
8. `ingestion/gatekeeper.py` (Events API polling, market extraction, volume+liquidity+category filters, lifecycle handling, canonical Yes token selection by outcome mapping)
9. `ingestion/ws_client.py` (connect, heartbeat, subscribe with `custom_feature_enabled`, composite price signal with midpoint priority, quote freshness tracking, market_resolved handling, exponential backoff reconnect)
10. Unit tests for gatekeeper (sports exclusion, lifecycle transitions, ambiguous token mapping) + WS parsing (all 3 price event types plus market_resolved, midpoint calculation, spread extraction, quote aging, warm-up behavior)

### Phase 3: Detection
11. `processing/alerts.py` (AlertLevel enum, ShiftAlert dataclass with signal_quality fields)
12. `processing/stream.py` (TickStream protocol, LiveStream, ReplayStream)
13. `processing/detector.py` (1-second bar aggregation with `price_open`/`price_last`, graduated thresholds, quality gates, cooldown tracking, min_ticks enforcement, warm-up handling)
14. Unit tests with synthetic price sequences (graduated thresholds, quality gate rejection, spread filtering, stale quote skipping, startup warm-up handling, direction detection, cooldown, burst traffic preservation)

### Phase 4: Notifications + Archival
15. `notifications/base.py` + `json_webhook.py` (structured JSON with signal_quality -- primary output for AI agents)
16. `notifications/discord.py` + `telegram.py` (human-readable -- secondary)
17. `storage/archiver.py` (SQLite → Parquet export + prune old ticks)
18. Tests for archiver round-trip + JSON alert schema validation (verify all agent-facing fields present)

### Phase 5: Orchestration
19. `app.py` (wire all subsystems, asyncio.gather with 6 tasks, signal handlers, graceful shutdown)
20. `__main__.py` (entry point)
21. End-to-end smoke test against live Polymarket
22. `scripts/backtest.py` (DuckDB query tool using ReplayStream + detector)

---

## Hosting Recommendation

| Option | Cost | Best For |
|---|---|---|
| Your local machine | $0/month | Development + initial deployment |
| Oracle Cloud Free Tier | $0/month forever | 4 ARM vCPUs, 24GB RAM -- best free option |
| Hetzner VPS | ~$4/month | Cheapest reliable cloud |
| DigitalOcean | $7/month | Best support + reliability |

**Start local, move to Oracle Free Tier or Hetzner when you want 24/7 uptime without keeping your machine on.**

---

## Verification Plan

1. **Unit tests:** `pytest tests/` -- composite price signal (midpoint vs fallback), graduated thresholds, quality gates, 1-second bar aggregation with `price_open`, gatekeeper filtering (sports excluded, liquidity gate), canonical Yes-token mapping, WS parsing (book/price_change/last_trade/market_resolved), SQLite round-trips, JSON alert schema, confidence scoring
2. **Smoke test:** Run `python -m sentinel` for 5 minutes, verify:
   - Events discovered (not markets), logged with event grouping
   - No Sports markets in output
   - WS connected, receiving ticks with signal_source tags (check `data/sentinel.db`)
   - Metrics logged every 60s showing tracked_markets count, tick rate, signal_source breakdown
   - Detector scanning (check logs for "scanning N markets")
3. **Price signal test:** Verify midpoint is preferred over last_trade by checking `signal_source` distribution in DB, verify stale quotes are rejected once `quote_age_sec` exceeds threshold, and verify warm-up alerts are possible but always marked `confidence="weak"`
4. **Quality gate test:** Find a low-liquidity market, verify detector skips it (check `alerts_skipped_quality` in metrics)
5. **Notification test:** Temporarily lower thresholds to trigger alerts, verify:
   - JSON files in `data/alerts/` contain all signal_quality fields
   - `confidence` field is computed correctly
   - `sibling_market_slugs` populated for multi-market events
   - Discord/Telegram messages arrive (if configured)
6. **Archive test:** Set archive interval to 60 seconds, verify Parquet files appear, query with `scripts/backtest.py`
7. **Replay test:** Archive some data, then run `backtest.py` using ReplayStream to verify same detection logic works on historical data and produces the same alerts after 1-second aggregation
8. **Resilience test:** Kill WiFi for 30 seconds, verify WS reconnects, `ws_reconnects` counter increments in metrics
9. **Lifecycle test:** Verify subscriptions use `custom_feature_enabled=true`, then wait for a market to resolve and confirm `market_resolved_ws_events` increments or REST reconciliation catches it and prunes detector state
