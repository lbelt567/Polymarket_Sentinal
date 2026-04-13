from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AppSection:
    env: str
    log_level: str


@dataclass(slots=True)
class DatabaseSection:
    url: str
    pool_size: int = 10


@dataclass(slots=True)
class QueueSection:
    lease_sec: int
    max_attempts: int
    retry_backoff_sec: int


@dataclass(slots=True)
class ResearcherSection:
    http_host: str
    http_port: int
    webhook_bearer_token: str = ""
    webhook_hmac_secret: str = ""
    prompt_version: str = "v1"
    source_freshness_sec: int = 21600


@dataclass(slots=True)
class AnalystSection:
    ruleset_version: str = "v1"
    min_avg_dollar_volume: float = 2_000_000
    max_spread_bps: int = 50
    min_price: float = 5.0
    max_price: float = 500.0


@dataclass(slots=True)
class ExecutorSection:
    approval_mode: str = "manual_all"
    risk_ruleset_version: str = "v1"
    max_position_pct: float = 0.05
    max_sector_exposure_pct: float = 0.20
    max_gross_invested_pct: float = 0.80
    max_daily_drawdown_pct: float = 0.02
    consecutive_loss_limit: int = 3
    cooldown_minutes: int = 60
    stale_quote_sec: int = 30
    approval_timeout_minutes: int = 15


@dataclass(slots=True)
class BrokerSection:
    provider: str = "alpaca"
    paper: bool = True
    api_key: str = ""
    api_secret: str = ""


@dataclass(slots=True)
class NotificationsSection:
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


@dataclass(slots=True)
class PipelineConfig:
    app: AppSection
    database: DatabaseSection
    queue: QueueSection
    researcher: ResearcherSection
    analyst: AnalystSection
    executor: ExecutorSection
    broker: BrokerSection
    notifications: NotificationsSection


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda match: os.getenv(match.group(1), ""), value)
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_env(item) for key, item in value.items()}
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("PyYAML is required to load pipeline.yaml") from exc

    loaded = yaml.safe_load(path.read_text()) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a YAML object at the top level")
    return loaded


def load_pipeline_config(
    config_path: str | Path = "pipeline.yaml",
    env_path: str | Path | None = ".env",
) -> PipelineConfig:
    if env_path:
        try:
            from dotenv import load_dotenv
        except ImportError:  # pragma: no cover - dependency guard
            load_dotenv = None
        if load_dotenv is not None:
            load_dotenv(Path(env_path), override=False)

    raw = _resolve_env(_load_yaml(Path(config_path)))
    return PipelineConfig(
        app=AppSection(**raw["app"]),
        database=DatabaseSection(**raw["database"]),
        queue=QueueSection(**raw["queue"]),
        researcher=ResearcherSection(**raw["researcher"]),
        analyst=AnalystSection(**raw["analyst"]),
        executor=ExecutorSection(**raw["executor"]),
        broker=BrokerSection(**raw["broker"]),
        notifications=NotificationsSection(**raw["notifications"]),
    )
