from __future__ import annotations

import argparse
from decimal import Decimal

from outcome.pricing import StaticOutcomePriceProvider
from outcome.worker import OutcomeWorker
from pipeline.config import load_pipeline_config
from pipeline.db import PipelineDB


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    config = load_pipeline_config()
    db = PipelineDB(config.database.url)
    db.initialize()
    worker = OutcomeWorker(db, config, StaticOutcomePriceProvider(prices={}))
    if args.once:
        worker.process_next()
    else:
        worker.run_forever()


if __name__ == "__main__":
    main()
