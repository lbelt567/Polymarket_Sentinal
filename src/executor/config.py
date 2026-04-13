from __future__ import annotations

from pipeline.config import ExecutorSection, PipelineConfig, load_pipeline_config


def load_executor_config(config_path: str = "pipeline.yaml") -> tuple[PipelineConfig, ExecutorSection]:
    config = load_pipeline_config(config_path)
    return config, config.executor
