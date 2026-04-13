from __future__ import annotations

import argparse
import json

from pipeline.config import load_pipeline_config
from pipeline.db import PipelineDB


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="pipeline.yaml")
    args = parser.parse_args()

    config = load_pipeline_config(args.config)
    db = PipelineDB(config.database.url)
    db.initialize()
    print(json.dumps(db.get_dead_letters(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
