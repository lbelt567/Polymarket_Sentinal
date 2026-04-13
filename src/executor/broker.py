from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from contracts.execution import TradeIntent


@dataclass(slots=True)
class BrokerOrderResult:
    broker_order_id: str
    status: str
    avg_fill_price: Decimal | None
    submitted_at: datetime
    raw_response: dict[str, object]


class BrokerClient(Protocol):
    broker_name: str

    def place_order(self, intent: TradeIntent, client_order_id: str) -> BrokerOrderResult:
        ...


@dataclass(slots=True)
class PaperBrokerClient:
    broker_name: str = "paper"

    def place_order(self, intent: TradeIntent, client_order_id: str) -> BrokerOrderResult:
        now = datetime.now(tz=timezone.utc)
        return BrokerOrderResult(
            broker_order_id=f"{self.broker_name}-{client_order_id}",
            status="submitted",
            avg_fill_price=intent.limit_price,
            submitted_at=now,
            raw_response={
                "client_order_id": client_order_id,
                "symbol": intent.symbol,
                "side": intent.side.value,
                "limit_price": str(intent.limit_price),
                "submitted_at": now.isoformat(),
            },
        )
