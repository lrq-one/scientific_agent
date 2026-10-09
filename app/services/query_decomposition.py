"""Evidence-only analysis of independent SQL populations.

This module does not author SQL or create a second planner.  It reports whether
the retrieved schema can prove the two lineage paths needed to separate model
prediction error from training membership coverage.
"""
from __future__ import annotations

from typing import Any


def _has_edge(relationships: list[dict[str, Any]], left: str, right: str) -> bool:
    return any({str(item.get("source_table")), str(item.get("target_table"))} == {left, right}
               for item in relationships)


def analyze_training_error_coverage(*, goal: str, schema: dict[str, Any], relationships: list[dict[str, Any]]) -> dict[str, Any]:
    text = (goal or "").lower()
    asks_error = any(token in text for token in ("误差", "mae", "rmse", "prediction", "预测"))
    asks_coverage = any(token in text for token in ("覆盖", "训练", "membership", "training"))
    lineage = {
        "model_run_to_dataset_version": (
            "model_runs" in schema and "experiments" in schema and "dataset_versions" in schema and
            (_has_edge(relationships, "model_runs", "experiments") or _has_edge(relationships, "experiments", "dataset_versions"))
        ),
        "training_membership_to_dataset_version": (
            "training_memberships" in schema and "dataset_versions" in schema and
            _has_edge(relationships, "training_memberships", "dataset_versions")
        ),
    }
    applicable = asks_error and asks_coverage
    verified = applicable and all(lineage.values())
    return {
        "applicable": applicable,
        "recommended_independent_populations": ["prediction_error", "training_coverage"] if applicable else [],
        "lineage_verified": verified,
        "lineage": lineage,
        "reason": ("Both independent population paths are represented in retrieved schema/relationships; keep them as separate scope-bound queries."
                   if verified else "Do not merge error and coverage populations until both model_run→experiment→dataset_version and membership→dataset_version paths are retrieved."),
    }
