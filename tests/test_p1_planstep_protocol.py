"""P1.2 PlanStep execution-contract tests; no provider or database calls."""
from __future__ import annotations

import pytest

from app.agents.plan_protocol import condition_for, prepare_replacement, satisfied
from app.agents.planning_policy import PlanningPolicy
from app.models.schemas import Capability, CompletionCondition, PlanStep, QueryScope, ScientificAgentState


def test_allowed_tools_is_canonical_and_legacy_tool_fields_round_trip():
    from_legacy = PlanStep(step_id="legacy", goal="read", selected_tools=["read_csv"])
    assert from_legacy.allowed_tools == ["read_csv"]
    assert from_legacy.preferred_tools == ["read_csv"]
    from_protocol = PlanStep(step_id="protocol", goal="read", allowed_tools=["read_csv"])
    assert from_protocol.selected_tools == ["read_csv"]
    assert from_protocol.allowed_tools == ["read_csv"]


def test_optional_tools_do_not_become_completion_obligations():
    step = PlanStep(
        step_id="query",
        goal="inspect",
        allowed_tools=["search_schema", "query_checker"],
        optional_tools=["query_checker"],
    )
    assert condition_for(step).required_tools == []
    assert not satisfied(step)
    step.observations = [{"tool": "search_schema", "success": True, "tool_call_id": "schema-1"}]
    assert satisfied(step)


def test_completion_predicate_requires_real_observation_and_scope():
    step = PlanStep(
        step_id="execute",
        goal="execute scoped SQL",
        allowed_tools=["execute_readonly_sql"],
        completion_predicate=CompletionCondition(
            kind="EXECUTED_ROWS", required_tools=["execute_readonly_sql"], require_scope_match=True
        ),
        query_scope=QueryScope(dataset_version="train_v3"),
    )
    assert not satisfied(step)
    step.observations = [{"tool": "execute_readonly_sql", "success": True, "scope_verified": False}]
    assert not satisfied(step)
    step.observations = [{
        "tool": "execute_readonly_sql",
        "success": True,
        "scope_verified": True,
        "effective_query_scope": step.query_scope.model_dump(mode="json"),
        "population_id": None,
    }]
    assert satisfied(step)


def test_required_inputs_and_predicate_must_be_within_step_scope():
    step = PlanStep(
        step_id="rows",
        goal="process rows",
        allowed_tools=["filter_samples", "save_result_table"],
        optional_tools=["save_result_table"],
        required_inputs=["rows"],
        completion_predicate=CompletionCondition(required_tools=["filter_samples"]),
        required_capabilities=[Capability.FILE],
    )
    assert PlanningPolicy.validate_plan([step], {"filter_samples", "save_result_table"}, {"file"})
    invalid = step.model_copy(update={"completion_predicate": CompletionCondition(required_tools=["save_result_table"])})
    with pytest.raises(ValueError, match="cannot require optional"):
        PlanningPolicy.validate_plan([invalid], {"filter_samples", "save_result_table"}, {"file"})


def test_replacement_persists_protocol_fields_and_resets_observations():
    state = ScientificAgentState(
        user_id="offline",
        thread_id="p1-plan",
        goal="read",
        query_scope=QueryScope(),
        available_tools=["file"],
        allowed_tools=["read_csv"],
    )
    proposal = PlanStep(
        step_id="read",
        goal="read input",
        allowed_tools=["read_csv"],
        optional_tools=[],
        required_inputs=["filename"],
        completion_predicate=CompletionCondition(required_tools=["read_csv"]),
        status="completed",
        observations=[{"tool": "read_csv", "success": True}],
    )
    _, prepared = prepare_replacement(state, [proposal])
    assert prepared[0].allowed_tools == ["read_csv"]
    assert prepared[0].required_inputs == ["filename"]
    assert prepared[0].completion_predicate is not None
    assert prepared[0].status == "pending"
    assert prepared[0].observations == []
