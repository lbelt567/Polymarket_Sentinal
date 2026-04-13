from __future__ import annotations

import argparse
from decimal import Decimal

from analyst.tools.market_data import StaticMarketDataProvider
from analyst.worker import AnalystWorker, StaticPortfolioProvider
from contracts.common import PortfolioSnapshot
from pipeline.config import load_pipeline_config
from pipeline.db import PipelineDB


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    config = load_pipeline_config()
    db = PipelineDB(config.database.url)
    db.initialize()
    market_data = StaticMarketDataProvider({})
    portfolio = StaticPortfolioProvider(
        PortfolioSnapshot(
            account_id="paper",
            equity=Decimal("100000"),
            cash=Decimal("100000"),
            buying_power=Decimal("100000"),
            gross_exposure=Decimal("0"),
            net_exposure=Decimal("0"),
        )
    )
    worker = AnalystWorker(db, config, market_data=market_data, portfolio_provider=portfolio)
    if args.once:
        worker.process_next()
    else:
        worker.run_forever()


if __name__ == "__main__":
    main()
