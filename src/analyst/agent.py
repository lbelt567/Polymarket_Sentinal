from __future__ import annotations


def score_candidate(passed_filters: int, failed_filters: int, confidence_weight: float) -> float:
    return round((passed_filters * 1.5) - (failed_filters * 1.0) + confidence_weight, 4)


def build_stage_summary(candidate_count: int, rejected_count: int) -> str:
    return f"Analyst produced {candidate_count} candidates and rejected {rejected_count} instruments."
