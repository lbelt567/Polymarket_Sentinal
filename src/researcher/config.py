from __future__ import annotations

from pipeline.config import PipelineConfig, ResearcherSection, load_pipeline_config


def load_researcher_config(config_path: str = "pipeline.yaml") -> tuple[PipelineConfig, ResearcherSection]:
    config = load_pipeline_config(config_path)
    return config, config.researcher
