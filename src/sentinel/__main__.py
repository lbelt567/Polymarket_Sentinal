from __future__ import annotations

import asyncio

from sentinel.app import SentinelApp
from sentinel.config import load_config
from sentinel.utils.logging import configure_logging


def main() -> None:
    config = load_config()
    configure_logging(config.logging)
    app = SentinelApp(config)
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
