from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol


@dataclass(slots=True)
class MarketDataSnapshot:
    symbol: str
    last_price: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    avg_daily_dollar_volume: Decimal = Decimal("0")
    session: str = "closed"
    tradable: bool = False
    halted: bool = False
    quote_age_sec: int = 0
    sector: str = "unknown"
    history: list[Decimal] = field(default_factory=list)

    @property
    def spread_bps(self) -> Decimal:
        if self.bid is None or self.ask is None or self.last_price == 0:
            return Decimal("0")
        return ((self.ask - self.bid) / self.last_price) * Decimal("10000")


class MarketDataProvider(Protocol):
    def get_snapshot(self, symbol: str) -> MarketDataSnapshot:
        ...


@dataclass(slots=True)
class StaticMarketDataProvider:
    snapshots: dict[str, MarketDataSnapshot]

    def get_snapshot(self, symbol: str) -> MarketDataSnapshot:
        if symbol not in self.snapshots:
            raise KeyError(f"snapshot not found for {symbol}")
        return self.snapshots[symbol]
