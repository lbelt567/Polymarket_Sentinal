from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class CircuitBreakerState:
    daily_drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    cooldown_until: datetime | None = None

    def is_halted(self, now: datetime, *, max_daily_drawdown_pct: float, consecutive_loss_limit: int) -> bool:
        if self.cooldown_until is not None and now < self.cooldown_until:
            return True
        if self.daily_drawdown_pct >= max_daily_drawdown_pct:
            return True
        if self.consecutive_losses >= consecutive_loss_limit:
            return True
        return False
