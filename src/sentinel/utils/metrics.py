from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Metrics:
    tracked_markets: int = 0
    ws_reconnects: int = 0
    ticks_ingested: int = 0
    ticks_per_minute: int = 0
    ticks_dropped_parse_error: int = 0
    ticks_dropped_stale: int = 0
    ticks_dropped_quote_stale: int = 0
    market_resolved_ws_events: int = 0
    alerts_scout: int = 0
    alerts_confirmation: int = 0
    alerts_trend: int = 0
    alerts_skipped_cooldown: int = 0
    alerts_skipped_quality: int = 0
    alerts_skipped_policy: int = 0
    alerts_skipped_event_dedup: int = 0
    alerts_dispatched: int = 0
    notifier_failures: int = 0
    retention_deleted_shift_events: int = 0
    retention_deleted_alert_files: int = 0
    retention_deleted_archive_files: int = 0
    archive_lag_sec: float = 0.0
    last_gatekeeper_run_iso: str = ""
    last_retention_run_iso: str = ""
    signal_source_counts: Counter[str] = field(default_factory=Counter)
    _ticks_window: list[int] = field(default_factory=list, repr=False)

    def increment(self, name: str, amount: int = 1) -> None:
        setattr(self, name, getattr(self, name) + amount)

    def add_signal_source(self, source: str) -> None:
        self.signal_source_counts[source] += 1

    def record_tick(self, timestamp_ms: int) -> None:
        self.ticks_ingested += 1
        self._ticks_window.append(timestamp_ms)
        cutoff = timestamp_ms - 60_000
        while self._ticks_window and self._ticks_window[0] < cutoff:
            self._ticks_window.pop(0)
        self.ticks_per_minute = len(self._ticks_window)

    def snapshot(self) -> dict[str, object]:
        return {
            "tracked_markets": self.tracked_markets,
            "ws_reconnects": self.ws_reconnects,
            "ticks_ingested": self.ticks_ingested,
            "ticks_per_minute": self.ticks_per_minute,
            "ticks_dropped_parse_error": self.ticks_dropped_parse_error,
            "ticks_dropped_stale": self.ticks_dropped_stale,
            "ticks_dropped_quote_stale": self.ticks_dropped_quote_stale,
            "market_resolved_ws_events": self.market_resolved_ws_events,
            "alerts_scout": self.alerts_scout,
            "alerts_confirmation": self.alerts_confirmation,
            "alerts_trend": self.alerts_trend,
            "alerts_skipped_cooldown": self.alerts_skipped_cooldown,
            "alerts_skipped_quality": self.alerts_skipped_quality,
            "alerts_skipped_policy": self.alerts_skipped_policy,
            "alerts_skipped_event_dedup": self.alerts_skipped_event_dedup,
            "alerts_dispatched": self.alerts_dispatched,
            "notifier_failures": self.notifier_failures,
            "retention_deleted_shift_events": self.retention_deleted_shift_events,
            "retention_deleted_alert_files": self.retention_deleted_alert_files,
            "retention_deleted_archive_files": self.retention_deleted_archive_files,
            "archive_lag_sec": self.archive_lag_sec,
            "last_gatekeeper_run_iso": self.last_gatekeeper_run_iso,
            "last_retention_run_iso": self.last_retention_run_iso,
            "signal_source_counts": dict(self.signal_source_counts),
        }

    def mark_gatekeeper_run(self) -> None:
        self.last_gatekeeper_run_iso = datetime.now(timezone.utc).isoformat()

    def mark_retention_run(self) -> None:
        self.last_retention_run_iso = datetime.now(timezone.utc).isoformat()


async def log_metrics_forever(metrics: Metrics, interval_sec: int, logger: logging.Logger) -> None:
    while True:
        logger.info("metrics", extra={"extra_data": metrics.snapshot()})
        await asyncio.sleep(interval_sec)
