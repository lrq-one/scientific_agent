"""P1.3 deterministic RecoveryPolicy matrix."""
from __future__ import annotations

import pytest

from app.models.schemas import PlanStep, RecoveryPolicy, ScientificAgentState
from app.services.recovery import recovery_policy_for


@pytest.mark.parametrize(
    ("code", "action", "budget", "recoverable"),
    [
        ("INVALID_ARGUMENT", "repair_arguments", 1, True),
        ("SCHEMA_MISMATCH", "retrieve_schema", 1, True),
        ("SQL_CANDIDATE_LINEAGE", "establish_sql_candidate", 1, True),
        ("UNVERIFIED_SCOPE", "targeted_sql_repair", 1, True),
        ("SCOPE_VIOLATION", "safe_reject", 0, False),
        ("TIMEOUT", "bounded_retry", 1, True),
        ("PLAN_DEPENDENCY", "repair_plan_dependency", 1, True),
        ("NO_PROGRESS_REPLAN", "no_progress_stop", 0, False),
        ("DATABASE_STORAGE_CORRUPTION", "stop", 0, False),
        ("USER_INPUT_REQUIRED", "ask_user", 0, True),
    ],
)
def test_recovery_policy_maps_failures_to_one_bounded_action(code, action, budget, recoverable):
    policy = recovery_policy_for(
        code,
        failed_stage="sql_scope_validation",
        context={"population_id": "p-v3"},
        previous_attempt={"tool": "text_to_sql"},
    )
    assert policy.recovery_action == action
    assert policy.retry_budget == budget
    assert policy.recoverable is recoverable
    assert policy.context["population_id"] == "p-v3"
    assert policy.previous_attempt["tool"] == "text_to_sql"


def test_nonrecoverable_override_cannot_reenable_scope_or_storage_retry():
    for code in ("SCOPE_VIOLATION", "DATABASE_STORAGE_CORRUPTION"):
        policy = recovery_policy_for(code, recoverable=True)
        assert policy.retry_budget == 0
        assert policy.recoverable is False


def test_recovery_policy_is_persistable_on_step_and_state_without_rewriting_observations():
    policy = recovery_policy_for(
        "UNVERIFIED_SCOPE",
        context={"query_scope": {"dataset_version": "train_v3"}},
        previous_attempt={"tool": "text_to_sql", "tool_call_id": "call-1"},
    )
    step = PlanStep(step_id="sql", goal="repair SQL", allowed_tools=["text_to_sql"], recovery_policy=policy)
    state = ScientificAgentState(user_id="offline", thread_id="recovery", goal="coverage", plan=[step])
    state.recovery_policy = policy
    state.recovery_history.append(policy)
    state.tool_calls = [{"tool": "text_to_sql", "tool_call_id": "call-1"}]
    assert state.plan[0].recovery_policy.recovery_action == "targeted_sql_repair"
    assert state.recovery_history[0].previous_attempt["tool_call_id"] == "call-1"
    assert state.tool_calls[0]["tool_call_id"] == "call-1"
