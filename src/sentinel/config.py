from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class GatekeeperConfig:
    gamma_api_url: str
    endpoint: str
    poll_interval_sec: int
    min_volume_24h: float
    min_liquidity: float
    max_markets: int
    excluded_categories: list[str]
    excluded_tag_slugs: list[str]
    stale_timeout_sec: int
    page_size: int = 100
    order_by: str = "volume24hr"
    ascending: bool = False


@dataclass(slots=True)
class WebsocketConfig:
    url: str
    heartbeat_interval_sec: int
    reconnect_base_delay_sec: int
    reconnect_max_delay_sec: int
    custom_feature_enabled: bool
    max_spread_for_midpoint: float
    max_quote_age_sec: int


@dataclass(slots=True)
class ThresholdConfig:
    window_sec: int
    delta_pct: float
    min_ticks: int


@dataclass(slots=True)
class DetectorConfig:
    check_interval_sec: int
    buffer_max_age_sec: int
    bar_interval_sec: int
    cooldown_sec: int
    stale_threshold_sec: int
    max_spread_for_detection: float
    max_quote_age_sec: int
    warmup_grace_sec: int
    thresholds: dict[str, ThresholdConfig]


@dataclass(slots=True)
class StorageConfig:
    sqlite_path: str
    hot_retention_hours: int
    archive_dir: str
    archive_interval_sec: int


@dataclass(slots=True)
class NotificationsConfig:
    enabled_channels: list[str]
    min_severity: str
    allowed_levels: list[str]
    excluded_confidences: list[str]
    excluded_signal_sources: list[str]
    min_price: float
    max_price: float
    min_liquidity: float
    event_dedup_sec: int
    json_file_dir: str
    json_webhook_url: str
    discord_webhook_url: str
    telegram_bot_token: str
    telegram_chat_id: str


@dataclass(slots=True)
class MetricsConfig:
    log_interval_sec: int


@dataclass(slots=True)
class LoggingConfig:
    level: str
    file: str


@dataclass(slots=True)
class AppConfig:
    gatekeeper: GatekeeperConfig
    websocket: WebsocketConfig
    detector: DetectorConfig
    storage: StorageConfig
    notifications: NotificationsConfig
    metrics: MetricsConfig
    logging: LoggingConfig


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
        raise RuntimeError("PyYAML is required to load config.yaml") from exc

    loaded = yaml.safe_load(path.read_text()) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a YAML object at the top level")
    return loaded


def load_config(config_path: str | Path = "config.yaml", env_path: str | Path | None = ".env") -> AppConfig:
    if env_path:
        try:
            from dotenv import load_dotenv
        except ImportError:  # pragma: no cover - dependency guard
            load_dotenv = None
        if load_dotenv is not None:
            load_dotenv(Path(env_path), override=False)

    raw = _resolve_env(_load_yaml(Path(config_path)))

    return AppConfig(
        gatekeeper=GatekeeperConfig(**raw["gatekeeper"]),
        websocket=WebsocketConfig(**raw["websocket"]),
        detector=DetectorConfig(
            **{k: v for k, v in raw["detector"].items() if k != "thresholds"},
            thresholds={name: ThresholdConfig(**payload) for name, payload in raw["detector"]["thresholds"].items()},
        ),
        storage=StorageConfig(**raw["storage"]),
        notifications=NotificationsConfig(**raw["notifications"]),
        metrics=MetricsConfig(**raw["metrics"]),
        logging=LoggingConfig(**raw["logging"]),
    )
