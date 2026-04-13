from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from contracts.execution import OutcomeHorizon, OutcomeObservation


def schedule_default_outcomes(trace_id: UUID, symbol: str, entry_price: Decimal, start_at: datetime | None = None) -> list[OutcomeObservation]:
    base_time = start_at or datetime.now(tz=timezone.utc)
    return [
        OutcomeObservation(
            trace_id=trace_id,
            symbol=symbol,
            horizon=OutcomeHorizon.T_PLUS_15M,
            scheduled_for=base_time + timedelta(minutes=15),
            entry_price=entry_price,
        ),
        OutcomeObservation(
            trace_id=trace_id,
            symbol=symbol,
            horizon=OutcomeHorizon.T_PLUS_1H,
            scheduled_for=base_time + timedelta(hours=1),
            entry_price=entry_price,
        ),
        OutcomeObservation(
            trace_id=trace_id,
            symbol=symbol,
            horizon=OutcomeHorizon.NEXT_SESSION_CLOSE,
            scheduled_for=base_time + timedelta(days=1),
            entry_price=entry_price,
        ),
    ]


class OutcomePriceProvider(Protocol):
    def get_price(self, symbol: str, at: datetime) -> Decimal:
        ...

    def get_benchmark_return(self, at: datetime, horizon: OutcomeHorizon | str) -> Decimal | None:
        ...


@dataclass(slots=True)
class ObservedOutcome:
    exit_price: Decimal
    return_pct: Decimal
    benchmark_return_pct: Decimal | None
    label: str


def classify_outcome(entry_price: Decimal, exit_price: Decimal, benchmark_return_pct: Decimal | None) -> ObservedOutcome:
    if entry_price == 0:
        return ObservedOutcome(exit_price=exit_price, return_pct=Decimal("0"), benchmark_return_pct=benchmark_return_pct, label="flat")
    return_pct = ((exit_price - entry_price) / entry_price) * Decimal("100")
    benchmark = benchmark_return_pct or Decimal("0")
    alpha = return_pct - benchmark
    if alpha > Decimal("0.25"):
        label = "positive_alpha"
    elif alpha < Decimal("-0.25"):
        label = "negative_alpha"
    else:
        label = "flat"
    return ObservedOutcome(
        exit_price=exit_price,
        return_pct=return_pct.quantize(Decimal("0.0001")),
        benchmark_return_pct=benchmark_return_pct,
        label=label,
    )
