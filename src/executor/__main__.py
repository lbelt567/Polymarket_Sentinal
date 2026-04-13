from __future__ import annotations

import argparse
from decimal import Decimal

from contracts.common import PortfolioSnapshot
from executor.server import run_server
from executor.worker import ApprovalWorker, ExecutorWorker, StaticExecutionContextProvider
from pipeline.config import load_pipeline_config
from pipeline.db import PipelineDB


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["server", "worker", "approval"], default="server", nargs="?")
    args = parser.parse_args()

    config = load_pipeline_config()
    if args.mode == "server":
        run_server(config)
        return

    db = PipelineDB(config.database.url)
    db.initialize()
    snapshot = PortfolioSnapshot(
        account_id="paper",
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        buying_power=Decimal("100000"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
    )
    context = StaticExecutionContextProvider({str(snapshot.snapshot_id): snapshot})
    if args.mode == "approval":
        ApprovalWorker(db, config).run_forever()
    else:
        ExecutorWorker(db, config, context_provider=context).run_forever()


if __name__ == "__main__":
    main()
