from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

from sentinel.config import WebsocketConfig
from sentinel.ingestion.models import MarketResolvedEvent, PriceTick, SubscriptionCommand, parse_float, parse_ts_ms
from sentinel.utils.metrics import Metrics


class MarketWSClient:
    def __init__(
        self,
        config: WebsocketConfig,
        output_queue: asyncio.Queue[PriceTick],
        command_queue: asyncio.Queue[SubscriptionCommand],
        metrics: Metrics,
        on_market_resolved,
    ) -> None:
        self._config = config
        self._output_queue = output_queue
        self._command_queue = command_queue
        self._metrics = metrics
        self._on_market_resolved = on_market_resolved
        self._logger = logging.getLogger(__name__)
        self._active_asset_ids: set[str] = set()

    async def run_forever(self) -> None:
        delay = self._config.reconnect_base_delay_sec
        while True:
            try:
                await self._run_connection()
                delay = self._config.reconnect_base_delay_sec
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - network/runtime path
                self._metrics.increment("ws_reconnects")
                self._logger.warning("ws_reconnect", extra={"extra_data": {"error": str(exc), "delay_sec": delay}})
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 2, self._config.reconnect_max_delay_sec)

    async def _run_connection(self) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("websockets is required for live websocket ingestion") from exc

        async with websockets.connect(self._config.url, ping_interval=None) as websocket:
            if self._active_asset_ids:
                await self._send_subscription(websocket)
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(websocket))
            command_task = asyncio.create_task(self._command_loop(websocket))
            try:
                async for raw_message in websocket:
                    try:
                        ticks, resolved_events = self.parse_message(raw_message, self._config)
                    except Exception:  # pragma: no cover - defensive parse guard
                        self._metrics.increment("ticks_dropped_parse_error")
                        continue
                    for tick in ticks:
                        await self._output_queue.put(tick)
                    for resolved_event in resolved_events:
                        self._metrics.increment("market_resolved_ws_events")
                        await self._on_market_resolved(resolved_event)
            finally:
                heartbeat_task.cancel()
                command_task.cancel()
                await asyncio.gather(heartbeat_task, command_task, return_exceptions=True)

    async def _heartbeat_loop(self, websocket) -> None:
        while True:
            await asyncio.sleep(self._config.heartbeat_interval_sec)
            await websocket.send("PING")

    async def _command_loop(self, websocket) -> None:
        while True:
            command = await self._command_queue.get()
            if command.operation == "subscribe":
                self._active_asset_ids.update(command.asset_ids)
            elif command.operation == "unsubscribe":
                self._active_asset_ids.difference_update(command.asset_ids)
            await self._send_subscription(websocket)

    async def _send_subscription(self, websocket) -> None:
        payload = {
            "assets_ids": sorted(self._active_asset_ids),
            "type": "market",
            "custom_feature_enabled": self._config.custom_feature_enabled,
        }
        await websocket.send(json.dumps(payload))

    @staticmethod
    def parse_message(raw_message: str | bytes, config: WebsocketConfig) -> tuple[list[PriceTick], list[MarketResolvedEvent]]:
        raw_payload = json.loads(raw_message)
        items = raw_payload if isinstance(raw_payload, list) else [raw_payload]
        ticks: list[PriceTick] = []
        resolved_events: list[MarketResolvedEvent] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            event_type = str(item.get("event_type") or "")
            timestamp_ms = parse_ts_ms(item.get("timestamp"))

            if event_type in {"book", "best_bid_ask"}:
                best_bid, best_ask = MarketWSClient._extract_best_bid_ask(item)
                if best_bid is None or best_ask is None:
                    continue
                spread = best_ask - best_bid
                if spread > config.max_spread_for_midpoint:
                    continue
                ticks.append(
                    PriceTick(
                        asset_id=str(item.get("asset_id")),
                        price=(best_bid + best_ask) / 2,
                        best_bid=best_bid,
                        best_ask=best_ask,
                        spread=spread,
                        signal_source="midpoint_book",
                        quote_ts_ms=timestamp_ms,
                        timestamp_ms=timestamp_ms,
                    )
                )
            elif event_type == "price_change":
                for change in item.get("price_changes", []):
                    best_bid = parse_float(change.get("best_bid"), default=-1.0)
                    best_ask = parse_float(change.get("best_ask"), default=-1.0)
                    if best_bid < 0 or best_ask < 0:
                        continue
                    spread = best_ask - best_bid
                    if spread > config.max_spread_for_midpoint:
                        continue
                    ticks.append(
                        PriceTick(
                            asset_id=str(change.get("asset_id")),
                            price=(best_bid + best_ask) / 2,
                            best_bid=best_bid,
                            best_ask=best_ask,
                            spread=spread,
                            signal_source="midpoint_change",
                            quote_ts_ms=timestamp_ms,
                            timestamp_ms=timestamp_ms,
                        )
                    )
            elif event_type == "last_trade_price":
                ticks.append(
                    PriceTick(
                        asset_id=str(item.get("asset_id")),
                        price=parse_float(item.get("price")),
                        best_bid=None,
                        best_ask=None,
                        spread=None,
                        signal_source="last_trade",
                        quote_ts_ms=None,
                        timestamp_ms=timestamp_ms,
                    )
                )
            elif event_type == "market_resolved":
                resolved_events.append(
                    MarketResolvedEvent(
                        market_id=str(item.get("market") or item.get("id") or ""),
                        winning_asset_id=str(item.get("winning_asset_id") or ""),
                        winning_outcome=str(item.get("winning_outcome") or ""),
                        timestamp_ms=timestamp_ms,
                        asset_ids=[str(asset_id) for asset_id in item.get("assets_ids", [])],
                    )
                )
        return ticks, resolved_events

    @staticmethod
    def _extract_best_bid_ask(item: dict[str, Any]) -> tuple[float | None, float | None]:
        if "best_bid" in item and "best_ask" in item:
            return parse_float(item.get("best_bid")), parse_float(item.get("best_ask"))
        bids = item.get("bids") or []
        asks = item.get("asks") or []
        if not bids or not asks:
            return None, None
        return parse_float(bids[0].get("price")), parse_float(asks[0].get("price"))
