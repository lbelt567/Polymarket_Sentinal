CREATE TABLE IF NOT EXISTS price_ticks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id        TEXT    NOT NULL,
    market_slug     TEXT    NOT NULL,
    price           REAL    NOT NULL,
    best_bid        REAL,
    best_ask        REAL,
    spread          REAL,
    signal_source   TEXT    NOT NULL,
    quote_ts_ms     INTEGER,
    ts_ms           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_ticks_asset_ts ON price_ticks (asset_id, ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_price_ticks_ts ON price_ticks (ts_ms);

CREATE TABLE IF NOT EXISTS shift_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id            TEXT    NOT NULL,
    market_slug         TEXT    NOT NULL,
    question            TEXT    NOT NULL,
    category            TEXT    NOT NULL,
    level               TEXT    NOT NULL,
    direction           TEXT    NOT NULL,
    delta_pct           REAL    NOT NULL,
    signed_delta_pct    REAL    NOT NULL,
    price_start         REAL    NOT NULL,
    price_end           REAL    NOT NULL,
    window_sec          INTEGER NOT NULL,
    ticks_in_window     INTEGER NOT NULL,
    signal_source       TEXT    NOT NULL,
    spread_at_alert     REAL,
    quote_age_sec       REAL,
    confidence          TEXT    NOT NULL,
    ts_ms               INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shift_events_ts ON shift_events (ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_shift_events_category ON shift_events (category, ts_ms DESC);

CREATE TABLE IF NOT EXISTS tracked_markets (
    asset_id              TEXT PRIMARY KEY,
    market_id             TEXT NOT NULL,
    condition_id          TEXT NOT NULL DEFAULT '',
    market_slug           TEXT NOT NULL,
    question              TEXT NOT NULL,
    description           TEXT NOT NULL DEFAULT '',
    category              TEXT NOT NULL DEFAULT '',
    tags                  TEXT NOT NULL DEFAULT '[]',
    event_id              TEXT NOT NULL DEFAULT '',
    event_title           TEXT NOT NULL DEFAULT '',
    event_slug            TEXT NOT NULL DEFAULT '',
    sibling_asset_ids     TEXT NOT NULL DEFAULT '[]',
    sibling_market_slugs  TEXT NOT NULL DEFAULT '[]',
    outcome               TEXT NOT NULL,
    end_date              TEXT,
    volume_24h            REAL NOT NULL,
    liquidity             REAL NOT NULL DEFAULT 0,
    polymarket_url        TEXT NOT NULL DEFAULT '',
    active                INTEGER NOT NULL DEFAULT 1,
    subscribed_at_ms      INTEGER NOT NULL DEFAULT 0,
    last_tick_ms          INTEGER NOT NULL DEFAULT 0,
    last_update           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracked_markets_active ON tracked_markets (active);
