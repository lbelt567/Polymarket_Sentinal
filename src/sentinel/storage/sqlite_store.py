from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from sentinel.ingestion.models import MarketMetadata, PriceTick
from sentinel.processing.alerts import ShiftAlert


class SQLiteStore:
    def __init__(self, path: str) -> None:
        self._path_str = path
        self._path = Path(path) if path != ":memory:" else None
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._tracked_cache: dict[str, MarketMetadata] = {}

    async def initialize(self) -> None:
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(self._path_str, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            schema_path = Path(__file__).with_name("schema.sql")
            conn.executescript(schema_path.read_text())
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-64000")
            conn.commit()
            return conn

        self._conn = await asyncio.to_thread(_open)
        await self.refresh_cache()

    async def close(self) -> None:
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            await asyncio.to_thread(conn.close)

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:  # pragma: no cover - defensive guard
            raise RuntimeError("SQLiteStore is not initialized")
        return self._conn

    def _query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        """Run a read query synchronously. Called via to_thread."""
        return self.connection.execute(sql, params).fetchall()

    def _mutate(self, sql: str, params: tuple | list = ()) -> None:
        """Run a write query synchronously. Called via to_thread."""
        self.connection.execute(sql, params)
        self.connection.commit()

    def _mutate_many(self, sql: str, params_seq: list[tuple]) -> None:
        """Run executemany synchronously. Called via to_thread."""
        self.connection.executemany(sql, params_seq)
        self.connection.commit()

    async def refresh_cache(self) -> None:
        async with self._lock:
            rows = await asyncio.to_thread(self._query, "SELECT * FROM tracked_markets")
        self._tracked_cache = {str(row["asset_id"]): self._row_to_market(row) for row in rows}

    def get_tracked_market(self, asset_id: str) -> MarketMetadata | None:
        return self._tracked_cache.get(asset_id)

    def active_asset_ids(self) -> set[str]:
        return {asset_id for asset_id, market in self._tracked_cache.items() if market.active}

    def list_tracked_markets(self) -> dict[str, MarketMetadata]:
        return dict(self._tracked_cache)

    async def upsert_tracked_markets(self, markets: list[MarketMetadata], now_ms: int) -> None:
        if not markets:
            return
        payload = []
        for market in markets:
            existing = self._tracked_cache.get(market.asset_id)
            payload.append(
                (
                    market.asset_id,
                    market.market_id,
                    market.condition_id,
                    market.market_slug,
                    market.question,
                    market.description,
                    market.category,
                    json.dumps(market.tags),
                    market.event_id,
                    market.event_title,
                    market.event_slug,
                    json.dumps(market.sibling_asset_ids),
                    json.dumps(market.sibling_market_slugs),
                    market.outcome,
                    market.end_date,
                    market.volume_24h,
                    market.liquidity,
                    market.polymarket_url,
                    int(existing.active if existing else market.active),
                    int(existing.subscribed_at_ms if existing else market.subscribed_at_ms),
                    int(existing.last_tick_ms if existing else market.last_tick_ms),
                    now_ms,
                )
            )

        async with self._lock:
            await asyncio.to_thread(
                self._mutate_many,
                """
                INSERT INTO tracked_markets (
                    asset_id, market_id, condition_id, market_slug, question, description,
                    category, tags, event_id, event_title, event_slug, sibling_asset_ids,
                    sibling_market_slugs, outcome, end_date, volume_24h, liquidity,
                    polymarket_url, active, subscribed_at_ms, last_tick_ms, last_update
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    market_id=excluded.market_id,
                    condition_id=excluded.condition_id,
                    market_slug=excluded.market_slug,
                    question=excluded.question,
                    description=excluded.description,
                    category=excluded.category,
                    tags=excluded.tags,
                    event_id=excluded.event_id,
                    event_title=excluded.event_title,
                    event_slug=excluded.event_slug,
                    sibling_asset_ids=excluded.sibling_asset_ids,
                    sibling_market_slugs=excluded.sibling_market_slugs,
                    outcome=excluded.outcome,
                    end_date=excluded.end_date,
                    volume_24h=excluded.volume_24h,
                    liquidity=excluded.liquidity,
                    polymarket_url=excluded.polymarket_url,
                    last_update=excluded.last_update
                """,
                payload,
            )
        for market in markets:
            cached = self._tracked_cache.get(market.asset_id)
            self._tracked_cache[market.asset_id] = replace(
                market,
                active=cached.active if cached else market.active,
                subscribed_at_ms=cached.subscribed_at_ms if cached else market.subscribed_at_ms,
                last_tick_ms=cached.last_tick_ms if cached else market.last_tick_ms,
            )

    async def set_market_active(self, asset_ids: list[str], active: bool, now_ms: int) -> None:
        if not asset_ids:
            return
        subscribed_at_ms = now_ms if active else 0
        placeholders = ",".join("?" for _ in asset_ids)
        async with self._lock:
            await asyncio.to_thread(
                self._mutate,
                f"UPDATE tracked_markets SET active=?, subscribed_at_ms=?, last_update=? WHERE asset_id IN ({placeholders})",
                [int(active), subscribed_at_ms, now_ms, *asset_ids],
            )
        for asset_id in asset_ids:
            market = self._tracked_cache.get(asset_id)
            if market is None:
                continue
            market.active = active
            market.subscribed_at_ms = subscribed_at_ms

    async def mark_market_resolved(self, asset_ids: list[str], now_ms: int) -> None:
        await self.set_market_active(asset_ids, active=False, now_ms=now_ms)

    async def update_last_tick(self, asset_id: str, timestamp_ms: int) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._mutate,
                "UPDATE tracked_markets SET last_tick_ms=?, last_update=? WHERE asset_id=?",
                (timestamp_ms, timestamp_ms, asset_id),
            )
        market = self._tracked_cache.get(asset_id)
        if market is not None:
            market.last_tick_ms = timestamp_ms

    async def insert_price_tick(self, tick: PriceTick, market_slug: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._mutate,
                """
                INSERT INTO price_ticks (
                    asset_id, market_slug, price, best_bid, best_ask, spread,
                    signal_source, quote_ts_ms, ts_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tick.asset_id,
                    market_slug,
                    tick.price,
                    tick.best_bid,
                    tick.best_ask,
                    tick.spread,
                    tick.signal_source,
                    tick.quote_ts_ms,
                    tick.timestamp_ms,
                ),
            )

    async def insert_shift_event(self, alert: ShiftAlert) -> None:
        shift = alert.shift
        quality = alert.signal_quality
        market = alert.market
        async with self._lock:
            await asyncio.to_thread(
                self._mutate,
                """
                INSERT INTO shift_events (
                    asset_id, market_slug, question, category, level, direction, delta_pct,
                    signed_delta_pct, price_start, price_end, window_sec, ticks_in_window,
                    signal_source, spread_at_alert, quote_age_sec, confidence, ts_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    market["asset_id"],
                    market["market_slug"],
                    market["question"],
                    market["category"],
                    alert.alert_level,
                    shift.direction,
                    shift.delta_pct,
                    shift.signed_delta_pct,
                    shift.price_start,
                    shift.price_end,
                    shift.window_sec,
                    shift.ticks_in_window,
                    quality.signal_source,
                    quality.spread,
                    quality.quote_age_sec,
                    quality.confidence,
                    alert.timestamp_ms,
                ),
            )

    async def list_archivable_hours(self, cutoff_ms: int) -> list[int]:
        async with self._lock:
            rows = await asyncio.to_thread(
                self._query,
                """
                SELECT DISTINCT (ts_ms / 3600000) * 3600000 AS hour_start
                FROM price_ticks
                WHERE ts_ms < ?
                ORDER BY hour_start ASC
                """,
                (cutoff_ms,),
            )
        return [int(row["hour_start"]) for row in rows]

    async def fetch_price_ticks_between(self, start_ms: int, end_ms: int) -> list[dict[str, object]]:
        async with self._lock:
            rows = await asyncio.to_thread(
                self._query,
                """
                SELECT asset_id, market_slug, price, best_bid, best_ask, spread,
                       signal_source, quote_ts_ms, ts_ms
                FROM price_ticks
                WHERE ts_ms >= ? AND ts_ms < ?
                ORDER BY ts_ms ASC
                """,
                (start_ms, end_ms),
            )
        return [dict(row) for row in rows]

    async def delete_price_ticks_before(self, cutoff_ms: int) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._mutate,
                "DELETE FROM price_ticks WHERE ts_ms < ?",
                (cutoff_ms,),
            )

    def _row_to_market(self, row: sqlite3.Row) -> MarketMetadata:
        return MarketMetadata(
            asset_id=str(row["asset_id"]),
            market_id=str(row["market_id"]),
            condition_id=str(row["condition_id"]),
            market_slug=str(row["market_slug"]),
            question=str(row["question"]),
            description=str(row["description"]),
            category=str(row["category"]),
            tags=json.loads(row["tags"]),
            event_id=str(row["event_id"]),
            event_title=str(row["event_title"]),
            event_slug=str(row["event_slug"]),
            sibling_asset_ids=json.loads(row["sibling_asset_ids"]),
            sibling_market_slugs=json.loads(row["sibling_market_slugs"]),
            outcome=str(row["outcome"]),
            end_date=str(row["end_date"] or ""),
            volume_24h=float(row["volume_24h"]),
            liquidity=float(row["liquidity"]),
            polymarket_url=str(row["polymarket_url"] or ""),
            active=bool(row["active"]),
            subscribed_at_ms=int(row["subscribed_at_ms"]),
            last_tick_ms=int(row["last_tick_ms"]),
        )
