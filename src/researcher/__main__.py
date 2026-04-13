from __future__ import annotations

import argparse

from pipeline.config import load_pipeline_config
from pipeline.db import PipelineDB
from researcher.server import run_server
from researcher.worker import ResearcherWorker


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["server", "worker"], default="server", nargs="?")
    args = parser.parse_args()

    config = load_pipeline_config()
    if args.mode == "server":
        run_server(config)
        return

    db = PipelineDB(config.database.url)
    db.initialize()
    ResearcherWorker(db, config).run_forever()


if __name__ == "__main__":
    main()
