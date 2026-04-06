from __future__ import annotations

import asyncio
from pathlib import Path

from sentinel.config import NotificationsConfig, StorageConfig
from sentinel.processing.alerts import ShiftAlert, ShiftSummary, SignalQuality
from sentinel.storage.retention import RetentionManager
from sentinel.storage.sqlite_store import SQLiteStore
from sentinel.utils.metrics import Metrics


def _alert(timestamp_ms: int) -> ShiftAlert:
    return ShiftAlert(
        alert_id=f"alert-{timestamp_ms}",
        alert_level="confirmation",
        alert_level_label="Confirmed Shift",
        timestamp_iso="2026-04-01T14:32:00+00:00",
        timestamp_ms=timestamp_ms,
        market={
            "asset_id": "asset-1",
            "market_slug": "market-slug",
            "question": "Question?",
            "category": "Economics",
        },
        shift=ShiftSummary(
            direction="up",
            delta_pct=10.0,
            signed_delta_pct=10.0,
            price_start=0.50,
            price_end=0.55,
            price_current=0.55,
            window_sec=300,
            ticks_in_window=12,
        ),
        signal_quality=SignalQuality(
            signal_source="midpoint_change",
            best_bid=0.54,
            best_ask=0.56,
            spread=0.02,
            quote_age_sec=1.0,
            liquidity_bucket="high",
            confidence="moderate",
        ),
    )


def test_retention_manager_prunes_old_state(tmp_path: Path) -> None:
    async def runner() -> None:
        now_ms = 1_000_000_000_000
        store = SQLiteStore(":memory:")
        await store.initialize()
        await store.insert_shift_event(_alert(now_ms - (40 * 86_400_000)))
        await store.insert_shift_event(_alert(now_ms - (5 * 86_400_000)))

        alerts_dir = tmp_path / "alerts"
        alerts_dir.mkdir()
        old_alert = alerts_dir / f"{now_ms - (40 * 86_400_000)}_asset-1.json"
        new_alert = alerts_dir / f"{now_ms - (5 * 86_400_000)}_asset-1.json"
        old_alert.write_text("{}")
        new_alert.write_text("{}")

        old_archive_dir = tmp_path / "archive" / "2001" / "07" / "15"
        old_archive_dir.mkdir(parents=True)
        (old_archive_dir / "01.parquet").write_text("old")
        (old_archive_dir / "01.markets.json").write_text("old")

        new_archive_dt_dir = tmp_path / "archive" / "2001" / "09" / "05"
        new_archive_dt_dir.mkdir(parents=True)
        (new_archive_dt_dir / "01.parquet").write_text("new")
        (new_archive_dt_dir / "01.markets.json").write_text("new")

        manager = RetentionManager(
            StorageConfig(
                sqlite_path=":memory:",
                hot_retention_hours=48,
                shift_event_retention_days=30,
                archive_dir=str(tmp_path / "archive"),
                archive_retention_days=30,
                archive_interval_sec=3600,
            ),
            NotificationsConfig(
                enabled_channels=["json_file"],
                min_severity="confirmation",
                allowed_levels=["confirmation", "trend"],
                excluded_confidences=["weak"],
                excluded_signal_sources=["last_trade"],
                min_price=0.10,
                max_price=0.98,
                min_liquidity=25_000,
                min_abs_move=0.02,
                event_dedup_sec=900,
                json_file_dir=str(alerts_dir),
                json_file_retention_days=30,
                json_webhook_url="",
                discord_webhook_url="",
                telegram_bot_token="",
                telegram_chat_id="",
            ),
            store,
            Metrics(),
            time_fn=lambda: now_ms,
        )

        await manager.prune_once()

        rows = store._query("SELECT COUNT(*) AS count FROM shift_events")
        assert rows[0]["count"] == 1
        assert not old_alert.exists()
        assert new_alert.exists()
        assert not (old_archive_dir / "01.parquet").exists()
        assert not (old_archive_dir / "01.markets.json").exists()
        assert (new_archive_dt_dir / "01.parquet").exists()
        assert (new_archive_dt_dir / "01.markets.json").exists()

    asyncio.run(runner())
