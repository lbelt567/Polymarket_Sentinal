from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

from sentinel.ingestion.models import PriceTick


class TickStream(Protocol):
    async def ticks(self) -> AsyncIterator[PriceTick]:
        ...


class VirtualClock:
    def __init__(self, initial_ms: int = 0) -> None:
        self._now_ms = initial_ms

    def now_ms(self) -> int:
        return self._now_ms

    def set(self, timestamp_ms: int) -> None:
        self._now_ms = timestamp_ms


class LiveStream:
    def __init__(self, queue: asyncio.Queue[PriceTick]) -> None:
        self._queue = queue

    async def ticks(self) -> AsyncIterator[PriceTick]:
        while True:
            yield await self._queue.get()


class ReplayStream:
    def __init__(self, parquet_glob: str, clock: VirtualClock, speed: float = 1.0) -> None:
        self._parquet_glob = parquet_glob
        self._clock = clock
        self._speed = max(speed, 0.0)

    async def ticks(self) -> AsyncIterator[PriceTick]:
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("duckdb is required for replay mode") from exc

        connection = duckdb.connect(database=":memory:")
        rows = connection.execute(
            """
            SELECT asset_id, price, best_bid, best_ask, spread, signal_source, quote_ts_ms, ts_ms
            FROM read_parquet(?)
            ORDER BY ts_ms ASC
            """,
            [self._parquet_glob],
        ).fetchall()
        previous_ts_ms: int | None = None
        for row in rows:
            tick = PriceTick(
                asset_id=str(row[0]),
                price=float(row[1]),
                best_bid=float(row[2]) if row[2] is not None else None,
                best_ask=float(row[3]) if row[3] is not None else None,
                spread=float(row[4]) if row[4] is not None else None,
                signal_source=str(row[5]),
                quote_ts_ms=int(row[6]) if row[6] is not None else None,
                timestamp_ms=int(row[7]),
            )
            if previous_ts_ms is not None and self._speed > 0:
                delay = max((tick.timestamp_ms - previous_ts_ms) / 1000 / self._speed, 0)
                if delay:
                    await asyncio.sleep(delay)
            self._clock.set(tick.timestamp_ms)
            previous_ts_ms = tick.timestamp_ms
            yield tick
        connection.close()


def default_parquet_glob(archive_dir: str | Path) -> str:
    return str(Path(archive_dir) / "**" / "*.parquet")
