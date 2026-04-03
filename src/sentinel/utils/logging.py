from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from sentinel.config import LoggingConfig


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra_data"):
            payload["extra"] = record.extra_data
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def configure_logging(config: LoggingConfig) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, config.level.upper(), logging.INFO))

    formatter = JsonFormatter()
    handlers: list[logging.Handler] = []

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)

    if config.file:
        log_path = Path(config.file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    for handler in handlers:
        root.addHandler(handler)
