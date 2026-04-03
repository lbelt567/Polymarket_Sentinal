# Polymarket Sentinel

A real-time Polymarket prediction market tracker that monitors price movements 24/7, detects significant shifts using a multi-timeframe waterfall, and emits structured JSON alerts designed for consumption by downstream AI agent systems.

## What It Does

Sentinel connects to Polymarket's public APIs to:

1. **Discover markets** via the Gamma REST API (`/events` endpoint), filtering by volume, liquidity, and category
2. **Stream live prices** via the CLOB WebSocket, computing composite price signals from order book midpoints
3. **Detect shifts** using graduated thresholds across three timeframes (1min/3min/5min)
4. **Emit alerts** as structured JSON files, HTTP webhooks, Discord messages, or Telegram notifications

No API keys are needed for market data -- Polymarket's public endpoints are unauthenticated for read-only access. You only need credentials if you enable Discord or Telegram notifications.

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
│    - Price floor/ceiling ($0.03 - $0.98)                │
│    - Min liquidity $25K for agent feed                  │
│    - Event-level dedup (1 per event per 15 min)         │
│                                                         │
│  SQLite hot buffer (WAL, last 48h)                      │
│  Archiver (hourly export to Parquet cold storage)       │
│  Notifications (JSON file / webhook / Discord / Telegram│
└─────────────────────────────────────────────────────────┘
               │
               ▼
          data/archive/YYYY/MM/DD/HH.parquet
          (queryable via DuckDB for backtesting)
```

Single process, single event loop, 6 concurrent asyncio tasks. Memory footprint ~5 MB for 200 markets.

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
  }
}
```

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
  min_price: 0.03
  max_price: 0.98
  min_liquidity: 25000
  event_dedup_sec: 900              # One alert per event per 15 min
```

## Project Structure

```
Polymarket_Sentinal/
├── config.yaml                  # Runtime configuration
├── .env.example                 # Template for notification credentials
├── pyproject.toml               # Dependencies and build config
├── src/sentinel/
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
├── tests/                       # pytest suite (10 tests)
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

Covers: detector delta computation, warm-up grace period, gatekeeper filtering, WebSocket message parsing, composite price signal, SQLite round-trips, archiver Parquet export, and notification dispatch/isolation.

## Design Decisions

- **Events API over Markets API**: Events nest markets, giving natural grouping, sibling metadata, and fewer API calls.
- **Canonical "Yes" token only**: Each market has Yes/No tokens with complementary prices. Tracking both produces duplicate mirror alerts.
- **SecondBar aggregation**: 1-second OHLC bars instead of raw tick deques. Prevents data loss during traffic bursts that would overflow fixed-size buffers.
- **Time-pruned buffers**: Deques pruned by timestamp (6 min), not fixed `maxlen`. Guarantees all three detection windows have complete data regardless of tick rate.
- **Composite price signal**: Mirrors Polymarket's own UI logic -- midpoint when spread is tight, last trade as fallback. Each tick tagged with `signal_source` for quality assessment.
- **Two-tier output**: Raw alerts stored in SQLite for analysis. Agent feed filtered by confidence, signal source, price range, liquidity, and event dedup.
- **Clock injection**: `time_fn: Callable[[], int]` threaded through all components enables deterministic testing and replay.

## License

MIT
