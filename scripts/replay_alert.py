from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.config import load_pipeline_config
from pipeline.db import PipelineDB
from researcher.server import ResearcherIngressService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--config", default="pipeline.yaml")
    parser.add_argument("--skip-hmac", action="store_true")
    args = parser.parse_args()

    config = load_pipeline_config(args.config)
    db = PipelineDB(config.database.url)
    db.initialize()
    service = ResearcherIngressService(db, config)

    body = Path(args.path).read_bytes()
    headers = {"x-sentinel-alert-id": json.loads(body)["alert_id"]}
    if not args.skip_hmac and config.researcher.webhook_hmac_secret:
        import hashlib
        import hmac

        digest = hmac.new(config.researcher.webhook_hmac_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["x-sentinel-signature"] = f"sha256={digest}"
    result = service.handle_alert(body, headers, replay_source=args.path)
    print(json.dumps(result.body, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
