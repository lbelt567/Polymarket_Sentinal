from __future__ import annotations

from decimal import Decimal

from contracts.analysis import FilterResult
from pipeline.config import AnalystSection
from analyst.tools.market_data import MarketDataSnapshot


def evaluate_filters(snapshot: MarketDataSnapshot, config: AnalystSection) -> list[FilterResult]:
    filters: list[FilterResult] = []
    filters.append(
        FilterResult(
            name="tradable",
            passed=snapshot.tradable and not snapshot.halted,
            value=str(snapshot.tradable and not snapshot.halted).lower(),
            reason="Broker indicates instrument is tradable and not halted." if snapshot.tradable and not snapshot.halted else "Instrument is not tradable or is halted.",
        )
    )
    filters.append(
        FilterResult(
            name="regular_hours",
            passed=snapshot.session == "regular",
            value=snapshot.session,
            reason="Regular session is open." if snapshot.session == "regular" else "Trading is limited to regular market hours in v1.",
        )
    )
    filters.append(
        FilterResult(
            name="price_band",
            passed=Decimal(str(config.min_price)) <= snapshot.last_price <= Decimal(str(config.max_price)),
            value=str(snapshot.last_price),
            reason="Price is inside configured band." if Decimal(str(config.min_price)) <= snapshot.last_price <= Decimal(str(config.max_price)) else "Price is outside configured band.",
        )
    )
    filters.append(
        FilterResult(
            name="spread_bps",
            passed=snapshot.spread_bps <= Decimal(config.max_spread_bps),
            value=f"{snapshot.spread_bps:.4f}",
            reason="Spread is below max threshold." if snapshot.spread_bps <= Decimal(config.max_spread_bps) else "Spread is too wide for v1 execution.",
        )
    )
    filters.append(
        FilterResult(
            name="avg_dollar_volume",
            passed=snapshot.avg_daily_dollar_volume >= Decimal(str(config.min_avg_dollar_volume)),
            value=str(snapshot.avg_daily_dollar_volume),
            reason="Average dollar volume meets minimum." if snapshot.avg_daily_dollar_volume >= Decimal(str(config.min_avg_dollar_volume)) else "Average dollar volume is below minimum.",
        )
    )
    return filters
