from __future__ import annotations

from pipeline.config import AnalystSection, PipelineConfig, load_pipeline_config


def load_analyst_config(config_path: str = "pipeline.yaml") -> tuple[PipelineConfig, AnalystSection]:
    config = load_pipeline_config(config_path)
    return config, config.analyst
