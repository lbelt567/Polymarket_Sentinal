from __future__ import annotations

from decimal import Decimal
from math import sqrt


def sma(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) < period or period <= 0:
        return None
    window = values[-period:]
    return sum(window, Decimal("0")) / Decimal(period)


def rsi(values: list[Decimal], period: int = 14) -> Decimal | None:
    if len(values) <= period:
        return None
    gains = Decimal("0")
    losses = Decimal("0")
    for prev, current in zip(values[-period - 1 : -1], values[-period:]):
        change = current - prev
        if change > 0:
            gains += change
        else:
            losses -= change
    if losses == 0:
        return Decimal("100")
    rs = gains / losses if losses else Decimal("0")
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def realized_volatility(values: list[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    returns: list[float] = []
    for prev, current in zip(values[:-1], values[1:]):
        if prev == 0:
            continue
        returns.append(float((current - prev) / prev))
    if not returns:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    return Decimal(str(sqrt(variance)))
