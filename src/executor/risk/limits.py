from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from contracts.analysis import TradeCandidate
from contracts.common import PortfolioSnapshot
from contracts.execution import RiskCheckResult
from executor.risk.circuit_breaker import CircuitBreakerState
from pipeline.config import ExecutorSection


@dataclass(slots=True)
class RiskEngine:
    config: ExecutorSection

    def evaluate(
        self,
        candidate: TradeCandidate,
        portfolio: PortfolioSnapshot,
        breaker: CircuitBreakerState,
    ) -> list[RiskCheckResult]:
        now = datetime.now(tz=timezone.utc)
        checks: list[RiskCheckResult] = []
        last_price = Decimal(str(candidate.market_snapshot.get("last_price") or "0"))
        sector = str(candidate.market_snapshot.get("sector") or "unknown")
        quote_age_sec = int(candidate.market_snapshot.get("quote_age_sec") or 0)
        sector_exposure = portfolio.sector_exposure.get(sector, Decimal("0"))

        checks.append(
            RiskCheckResult(
                name="candidate_eligible",
                passed=bool(candidate.broker_eligible),
                measured_value=str(candidate.broker_eligible).lower(),
                threshold="true",
                reason="Candidate is broker eligible." if candidate.broker_eligible else "Candidate is not broker eligible.",
            )
        )
        checks.append(
            RiskCheckResult(
                name="candidate_not_expired",
                passed=candidate.expiry_at > now,
                measured_value=candidate.expiry_at.isoformat(),
                threshold="future",
                reason="Candidate is still inside execution window." if candidate.expiry_at > now else "Candidate expired before execution.",
            )
        )
        checks.append(
            RiskCheckResult(
                name="quote_freshness",
                passed=quote_age_sec <= self.config.stale_quote_sec,
                measured_value=str(quote_age_sec),
                threshold=str(self.config.stale_quote_sec),
                reason="Quote freshness passes." if quote_age_sec <= self.config.stale_quote_sec else "Quote is stale.",
            )
        )
        checks.append(
            RiskCheckResult(
                name="max_position_pct",
                passed=candidate.max_notional <= portfolio.equity * Decimal(str(self.config.max_position_pct)),
                measured_value=str(candidate.max_notional),
                threshold=str(portfolio.equity * Decimal(str(self.config.max_position_pct))),
                reason="Candidate fits max position sizing." if candidate.max_notional <= portfolio.equity * Decimal(str(self.config.max_position_pct)) else "Candidate breaches max position sizing.",
            )
        )
        checks.append(
            RiskCheckResult(
                name="cash_available",
                passed=candidate.max_notional <= portfolio.cash,
                measured_value=str(candidate.max_notional),
                threshold=str(portfolio.cash),
                reason="Cash is available." if candidate.max_notional <= portfolio.cash else "Not enough cash for the proposed notional.",
            )
        )
        checks.append(
            RiskCheckResult(
                name="max_gross_invested",
                passed=(portfolio.gross_exposure + candidate.max_notional) <= portfolio.equity * Decimal(str(self.config.max_gross_invested_pct)),
                measured_value=str(portfolio.gross_exposure + candidate.max_notional),
                threshold=str(portfolio.equity * Decimal(str(self.config.max_gross_invested_pct))),
                reason="Gross exposure stays inside limit." if (portfolio.gross_exposure + candidate.max_notional) <= portfolio.equity * Decimal(str(self.config.max_gross_invested_pct)) else "Gross exposure would exceed limit.",
            )
        )
        checks.append(
            RiskCheckResult(
                name="max_sector_exposure",
                passed=(sector_exposure + candidate.max_notional) <= portfolio.equity * Decimal(str(self.config.max_sector_exposure_pct)),
                measured_value=str(sector_exposure + candidate.max_notional),
                threshold=str(portfolio.equity * Decimal(str(self.config.max_sector_exposure_pct))),
                reason="Sector exposure stays inside limit." if (sector_exposure + candidate.max_notional) <= portfolio.equity * Decimal(str(self.config.max_sector_exposure_pct)) else "Sector exposure would exceed limit.",
            )
        )
        checks.append(
            RiskCheckResult(
                name="circuit_breaker",
                passed=not breaker.is_halted(
                    now,
                    max_daily_drawdown_pct=self.config.max_daily_drawdown_pct,
                    consecutive_loss_limit=self.config.consecutive_loss_limit,
                ),
                measured_value=f"drawdown={breaker.daily_drawdown_pct},losses={breaker.consecutive_losses}",
                threshold=f"drawdown<{self.config.max_daily_drawdown_pct},losses<{self.config.consecutive_loss_limit}",
                reason="Circuit breaker is open." if not breaker.is_halted(now, max_daily_drawdown_pct=self.config.max_daily_drawdown_pct, consecutive_loss_limit=self.config.consecutive_loss_limit) else "Circuit breaker is active.",
            )
        )
        checks.append(
            RiskCheckResult(
                name="positive_price",
                passed=last_price > 0,
                measured_value=str(last_price),
                threshold=">0",
                reason="Last price is positive." if last_price > 0 else "Candidate lacks a valid market price.",
            )
        )
        return checks
