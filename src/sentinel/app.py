from __future__ import annotations

import asyncio
import logging
import time

from sentinel.config import AppConfig
from sentinel.ingestion.models import MarketResolvedEvent, SubscriptionCommand
from sentinel.ingestion.gatekeeper import Gatekeeper
from sentinel.ingestion.ws_client import MarketWSClient
from sentinel.notifications.base import AgentAlertPolicy, NotificationDispatcher
from sentinel.notifications.discord import DiscordNotifier
from sentinel.notifications.json_webhook import HttpJsonWebhookNotifier, JsonFileNotifier
from sentinel.notifications.telegram import TelegramNotifier
from sentinel.processing.alerts import AlertLevel
from sentinel.processing.detector import ShiftDetector
from sentinel.processing.enrichment import BeforeStateEnricher
from sentinel.processing.stream import LiveStream
from sentinel.storage.archiver import Archiver
from sentinel.storage.retention import RetentionManager
from sentinel.storage.sqlite_store import SQLiteStore
from sentinel.utils.metrics import Metrics, log_metrics_forever


class SentinelApp:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._logger = logging.getLogger(__name__)
        self._store = SQLiteStore(config.storage.sqlite_path)
        self._metrics = Metrics()
        self._tick_queue: asyncio.Queue = asyncio.Queue()
        self._command_queue: asyncio.Queue[SubscriptionCommand] = asyncio.Queue()
        self._time_fn = lambda: int(time.time() * 1000)

    async def run(self) -> None:
        await self._store.initialize()
        dispatcher = NotificationDispatcher(
            notifiers=self._build_notifiers(),
            min_severity=AlertLevel(self._config.notifications.min_severity),
            policy=AgentAlertPolicy(
                allowed_levels={AlertLevel(level) for level in self._config.notifications.allowed_levels},
                excluded_confidences={item.lower() for item in self._config.notifications.excluded_confidences},
                excluded_signal_sources={item.lower() for item in self._config.notifications.excluded_signal_sources},
                min_price=self._config.notifications.min_price,
                max_price=self._config.notifications.max_price,
                min_liquidity=self._config.notifications.min_liquidity,
                min_abs_move=self._config.notifications.min_abs_move,
                event_dedup_sec=self._config.notifications.event_dedup_sec,
            ),
            metrics=self._metrics,
            enricher=BeforeStateEnricher(self._store),
        )
        detector = ShiftDetector(self._config.detector, self._store, dispatcher, self._metrics, self._time_fn)
        gatekeeper = Gatekeeper(self._config.gatekeeper, self._store, self._command_queue, self._metrics, self._time_fn)
        archiver = Archiver(self._config.storage, self._store, self._metrics, self._time_fn)
        retention = RetentionManager(
            self._config.storage,
            self._config.notifications,
            self._store,
            self._metrics,
            self._time_fn,
        )

        async def on_market_resolved(event: MarketResolvedEvent) -> None:
            asset_ids = event.asset_ids
            if not asset_ids and event.winning_asset_id:
                asset_ids = [event.winning_asset_id]
            await self._store.mark_market_resolved(asset_ids, now_ms=event.timestamp_ms or self._time_fn())
            for asset_id in asset_ids:
                detector.remove_asset(asset_id)

        ws_client = MarketWSClient(
            self._config.websocket,
            self._tick_queue,
            self._command_queue,
            self._metrics,
            on_market_resolved=on_market_resolved,
        )

        await gatekeeper.run_once()

        tasks = [
            asyncio.create_task(gatekeeper.run_forever(), name="gatekeeper"),
            asyncio.create_task(ws_client.run_forever(), name="ws_client"),
            asyncio.create_task(detector.ingest_loop(LiveStream(self._tick_queue)), name="ingest"),
            asyncio.create_task(detector.scan_loop(), name="scan"),
            asyncio.create_task(archiver.run_forever(), name="archiver"),
            asyncio.create_task(retention.run_forever(), name="retention"),
            asyncio.create_task(log_metrics_forever(self._metrics, self._config.metrics.log_interval_sec, self._logger), name="metrics"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._store.close()

    def _build_notifiers(self) -> list[object]:
        channels = set(self._config.notifications.enabled_channels)
        notifiers: list[object] = []

        if "json_file" in channels:
            notifiers.append(JsonFileNotifier(self._config.notifications.json_file_dir))
        if "json_webhook" in channels and self._config.notifications.json_webhook_url:
            notifiers.append(
                HttpJsonWebhookNotifier(
                    self._config.notifications.json_webhook_url,
                    timeout_sec=self._config.notifications.json_webhook_timeout_sec,
                    max_retries=self._config.notifications.json_webhook_max_retries,
                    retry_backoff_sec=self._config.notifications.json_webhook_retry_backoff_sec,
                    bearer_token=self._config.notifications.json_webhook_bearer_token,
                    hmac_secret=self._config.notifications.json_webhook_hmac_secret,
                )
            )
        if "discord" in channels and self._config.notifications.discord_webhook_url:
            notifiers.append(DiscordNotifier(self._config.notifications.discord_webhook_url))
        if "telegram" in channels and self._config.notifications.telegram_bot_token and self._config.notifications.telegram_chat_id:
            notifiers.append(
                TelegramNotifier(
                    self._config.notifications.telegram_bot_token,
                    self._config.notifications.telegram_chat_id,
                )
            )
        return notifiers
