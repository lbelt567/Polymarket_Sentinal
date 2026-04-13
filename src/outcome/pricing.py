from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from contracts.execution import OutcomeHorizon
from pipeline.outcomes import OutcomePriceProvider


@dataclass(slots=True)
class StaticOutcomePriceProvider(OutcomePriceProvider):
    prices: dict[tuple[str, str], Decimal]
    benchmarks: dict[str, Decimal] | None = None

    def get_price(self, symbol: str, at: datetime) -> Decimal:
        key = (symbol, at.isoformat())
        if key not in self.prices:
            raise KeyError(f"price not found for {symbol} at {at.isoformat()}")
        return self.prices[key]

    def get_benchmark_return(self, at: datetime, horizon: OutcomeHorizon | str) -> Decimal | None:
        if not self.benchmarks:
            return None
        key = horizon.value if isinstance(horizon, OutcomeHorizon) else horizon
        return self.benchmarks.get(key)
