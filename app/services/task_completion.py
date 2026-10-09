"""Classify persisted task success from actual Agent quality and goal coverage.

A terminal FINAL_ANSWER event is not proof that the user's scientific task
succeeded. Keep the existing completed/failed storage contract.
"""
from __future__ import annotations

RUNTIME_PROTOCOL_VERSION = "2026-10-09-plan-schema-and-task-coverage-v1"

UNSUCCESSFUL_QUALITY = frozenset({
    "EXECUTION_FAILED", "INSUFFICIENT_EVIDENCE", "CONFLICTING_EVIDENCE",
})
INCOMPLETE_COVERAGE = frozenset({"PARTIAL", "UNSATISFIED", "UNVERIFIABLE"})


def scientific_task_status(final_event_data: dict) -> str:
    state = final_event_data.get("state") or {}
    if not isinstance(state, dict):
        return "completed"
    quality = state.get("quality_status")
    if quality in UNSUCCESSFUL_QUALITY:
        return "failed"
    # An honestly returned empty result is a completed inquiry, not a
    # fabricated scientific conclusion.
    if quality == "NO_DATA":
        return "completed"
    # Old/minimal event producers can omit goal_coverage altogether.
    coverage = state.get("goal_coverage") or {}
    if isinstance(coverage, dict) and coverage.get("status") in INCOMPLETE_COVERAGE:
        return "failed"
    return "completed"
