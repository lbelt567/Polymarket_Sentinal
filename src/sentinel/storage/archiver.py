from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from sentinel.config import StorageConfig
from sentinel.storage.sqlite_store import SQLiteStore
from sentinel.utils.metrics import Metrics


class Archiver:
    def __init__(self, config: StorageConfig, store: SQLiteStore, metrics: Metrics, time_fn) -> None:
        self._config = config
        self._store = store
        self._metrics = metrics
        self._time_fn = time_fn
        self._logger = logging.getLogger(__name__)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.archive_once()
                await asyncio.sleep(self._config.archive_interval_sec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - runtime guard
                self._logger.warning("archive_error", extra={"extra_data": {"error": str(exc)}})
                await asyncio.sleep(min(self._config.archive_interval_sec, 60))

    async def archive_once(self) -> None:
        now_ms = self._time_fn()
        current_hour_start = (now_ms // 3_600_000) * 3_600_000
        hour_starts = await self._store.list_archivable_hours(current_hour_start)
        for hour_start in hour_starts:
            rows = await self._store.fetch_price_ticks_between(hour_start, hour_start + 3_600_000)
            if not rows:
                continue
            await self._write_parquet(hour_start, rows)
            await self._write_market_snapshot(hour_start)

        retention_cutoff = now_ms - (self._config.hot_retention_hours * 3_600_000)
        await self._store.delete_price_ticks_before(retention_cutoff)
        self._metrics.archive_lag_sec = 0.0

    async def _write_parquet(self, hour_start_ms: int, rows: list[dict[str, object]]) -> None:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("pyarrow is required for archival") from exc

        dt = datetime.fromtimestamp(hour_start_ms / 1000, tz=timezone.utc)
        output_path = (
            Path(self._config.archive_dir)
            / f"{dt.year:04d}"
            / f"{dt.month:02d}"
            / f"{dt.day:02d}"
            / f"{dt.hour:02d}.parquet"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(rows)
        await asyncio.to_thread(pq.write_table, table, output_path)

    async def _write_market_snapshot(self, hour_start_ms: int) -> None:
        dt = datetime.fromtimestamp(hour_start_ms / 1000, tz=timezone.utc)
        output_path = (
            Path(self._config.archive_dir)
            / f"{dt.year:04d}"
            / f"{dt.month:02d}"
            / f"{dt.day:02d}"
            / f"{dt.hour:02d}.markets.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(market) for market in self._store.list_tracked_markets().values()]
        await asyncio.to_thread(output_path.write_text, json.dumps(payload, separators=(",", ":")))
