from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from sentinel.config import NotificationsConfig, StorageConfig
from sentinel.storage.sqlite_store import SQLiteStore
from sentinel.utils.metrics import Metrics


class RetentionManager:
    def __init__(
        self,
        storage_config: StorageConfig,
        notifications_config: NotificationsConfig,
        store: SQLiteStore,
        metrics: Metrics,
        time_fn,
    ) -> None:
        self._storage_config = storage_config
        self._notifications_config = notifications_config
        self._store = store
        self._metrics = metrics
        self._time_fn = time_fn
        self._logger = logging.getLogger(__name__)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.prune_once()
                await asyncio.sleep(self._storage_config.archive_interval_sec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - runtime guard
                self._logger.warning("retention_error", extra={"extra_data": {"error": str(exc)}})
                await asyncio.sleep(min(self._storage_config.archive_interval_sec, 60))

    async def prune_once(self) -> None:
        now_ms = self._time_fn()
        deleted_shift_events = 0
        deleted_alert_files = 0
        deleted_archive_files = 0

        if self._storage_config.shift_event_retention_days > 0:
            cutoff_ms = now_ms - (self._storage_config.shift_event_retention_days * 86_400_000)
            deleted_shift_events = await self._store.delete_shift_events_before(cutoff_ms)
            self._metrics.increment("retention_deleted_shift_events", deleted_shift_events)

        if self._notifications_config.json_file_retention_days > 0:
            cutoff_ms = now_ms - (self._notifications_config.json_file_retention_days * 86_400_000)
            deleted_alert_files = await asyncio.to_thread(
                self._prune_alert_files,
                Path(self._notifications_config.json_file_dir),
                cutoff_ms,
            )
            self._metrics.increment("retention_deleted_alert_files", deleted_alert_files)

        if self._storage_config.archive_retention_days > 0:
            cutoff_ms = now_ms - (self._storage_config.archive_retention_days * 86_400_000)
            deleted_archive_files = await asyncio.to_thread(
                self._prune_archive_files,
                Path(self._storage_config.archive_dir),
                cutoff_ms,
            )
            self._metrics.increment("retention_deleted_archive_files", deleted_archive_files)

        self._metrics.mark_retention_run()
        self._logger.info(
            "retention_run",
            extra={
                "extra_data": {
                    "deleted_shift_events": deleted_shift_events,
                    "deleted_alert_files": deleted_alert_files,
                    "deleted_archive_files": deleted_archive_files,
                }
            },
        )

    def _prune_alert_files(self, root: Path, cutoff_ms: int) -> int:
        if not root.exists():
            return 0
        deleted = 0
        for path in root.glob("*.json"):
            file_ts_ms = self._parse_alert_ts_ms(path)
            if file_ts_ms is not None and file_ts_ms < cutoff_ms:
                path.unlink(missing_ok=True)
                deleted += 1
        return deleted

    def _prune_archive_files(self, root: Path, cutoff_ms: int) -> int:
        if not root.exists():
            return 0
        deleted = 0
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".parquet", ".json"}:
                continue
            file_hour_ms = self._parse_archive_hour_ms(root, path)
            if file_hour_ms is not None and file_hour_ms < cutoff_ms:
                path.unlink(missing_ok=True)
                deleted += 1
        for directory in sorted((item for item in root.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                continue
        return deleted

    @staticmethod
    def _parse_alert_ts_ms(path: Path) -> int | None:
        prefix = path.stem.split("_", 1)[0]
        if prefix.isdigit():
            return int(prefix)
        try:
            return int(path.stat().st_mtime * 1000)
        except OSError:
            return None

    @staticmethod
    def _parse_archive_hour_ms(root: Path, path: Path) -> int | None:
        try:
            rel = path.relative_to(root)
        except ValueError:
            return None
        if len(rel.parts) != 4:
            return None
        year, month, day, filename = rel.parts
        hour_token = filename.split(".", 1)[0]
        if not (year.isdigit() and month.isdigit() and day.isdigit() and hour_token.isdigit()):
            return None
        dt = datetime(
            year=int(year),
            month=int(month),
            day=int(day),
            hour=int(hour_token),
            tzinfo=timezone.utc,
        )
        return int(dt.timestamp() * 1000)
