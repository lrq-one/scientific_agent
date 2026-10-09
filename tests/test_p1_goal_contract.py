"""Offline P1.1 GoalContract contract and counterfactuals."""
from __future__ import annotations

import pytest

from app.agents.goal_contract import (
    accept_population_requirements,
    apply_decision_proposal,
    ensure_goal_contract,
    revise_from_hitl,
)
from app.agents.goal_coverage import assess_goal_coverage
from app.models.schemas import (
    AgentDecision,
    Capability,
    Evidence,
    PlanStep,
    PopulationRequirement,
    QueryScope,
    ResourceSummary,
    ScientificAgentState,
    ToolResult,
    GoalContract,
)


def _file_state(goal="compare model_v1.csv and model_v2.csv structure_type MAE"):
    return ScientificAgentState(
        user_id="offline",
        thread_id="p1",
        goal=goal,
        user_request=goal,
        resource_summary=ResourceSummary(
            authorized_datasources=["training_db"],
            available_files=["model_v1.csv", "model_v2.csv"],
        ),
        query_scope=QueryScope(grouping=["structure_type"]),
    )


def test_file_only_contract_does_not_gain_database_from_discovered_resource_or_plan():
    state = _file_state()
    contract = ensure_goal_contract(state)
    assert contract.required_deliverables == ["file_analysis"]
    assert "database_analysis" not in contract.required_deliverables

    state.plan = [
        PlanStep(
            step_id="db",
            goal="extra SQL",
            selected_tools=["execute_readonly_sql"],
            required_capabilities=[Capability.DATABASE],
        )
    ]
    state.tool_calls = [{"tool": "compare_models", "tool_call_id": "file-1"}]
    state.observations = [ToolResult(success=True, data=[{"structure_type": "linear", "mae": 1.0}])]
    state.evidence = [
        Evidence(
            evidence_id="ev-file",
            claim="file MAE",
            value=[{"mae": 1.0}],
            source_type="file",
            source="model_v1.csv",
            tool_call_id="file-1",
        )
    ]
    coverage = assess_goal_coverage(state)
    assert coverage.status == "SATISFIED"
    assert "executed_database_analysis" not in coverage.missing_deliverables


def test_mixed_contract_cannot_be_satisfied_when_plan_omits_database_evidence():
    state = _file_state("compare model_v2.csv and training_db train_v3 prediction error")
    contract = ensure_goal_contract(state)
    assert set(contract.required_deliverables) == {"file_analysis", "database_analysis"}
    state.tool_calls = [{"tool": "compare_models", "tool_call_id": "file-1"}]
    state.observations = [ToolResult(success=True, data=[{"structure_type": "linear", "mae": 1.0}])]
    state.evidence = [
        Evidence(
            evidence_id="ev-file",
            claim="file MAE",
            value=[{"mae": 1.0}],
            source_type="file",
            source="model_v2.csv",
            tool_call_id="file-1",
        )
    ]
    coverage = assess_goal_coverage(state)
    assert coverage.status == "PARTIAL"
    assert "executed_database_analysis" in coverage.missing_deliverables


def test_replan_or_llm_cannot_add_unrequested_deliverable_or_dimension():
    state = _file_state()
    ensure_goal_contract(state)
    with pytest.raises(ValueError, match="GoalContract rejects"):
        apply_decision_proposal(
            state,
            AgentDecision(
                action="REPLAN",
                required_deliverables=["database_analysis"],
                requested_dimensions=["unrequested_dimension"],
            ),
        )
    assert state.goal_contract.contract_version == 1
    assert state.goal_contract.required_deliverables == ["file_analysis"]


def test_llm_can_only_add_a_dimension_that_is_explicitly_present_in_original_request():
    state = _file_state("compare model_v1.csv and model_v2.csv by temperature")
    state.query_scope = QueryScope()
    ensure_goal_contract(state)
    revised = apply_decision_proposal(state, AgentDecision(action="ANSWER", requested_dimensions=["temperature"]))
    assert revised.required_dimensions == ["temperature"]
    assert revised.contract_version == 2
    assert revised.change_source == "validated_proposal"
    assert len(state.goal_contract_history) == 1


def test_population_contract_accepts_original_span_and_rejects_replan_change():
    state = _file_state("check training_db train_v2 fused_ring training coverage and train_v3 fused_ring training coverage")
    state.resource_summary = ResourceSummary(authorized_datasources=["training_db"])
    ensure_goal_contract(state)
    first = PopulationRequirement(
        population_id="p-v2",
        source_text="train_v2 fused_ring training coverage",
        query_scope=QueryScope(dataset_version="train_v2", grouping=["structure_type"]),
    )
    accept_population_requirements(state, [first])
    assert state.goal_contract.population_requirements[0].population_id == "p-v2"
    changed = first.model_copy(
        update={
            "population_id": "p-v3",
            "source_text": "train_v3 fused_ring training coverage",
            "query_scope": QueryScope(dataset_version="train_v3"),
        }
    )
    with pytest.raises(ValueError, match="ordinary Replan"):
        accept_population_requirements(state, [changed])


def test_hitl_version_confirmation_creates_contract_revision_without_changing_goal():
    state = _file_state("analyze training_db structure coverage")
    ensure_goal_contract(state)
    state.query_scope = QueryScope(dataset_version="train_v2")
    revised = revise_from_hitl(state, "confirm use train_v2")
    assert revised.contract_version == 2
    assert revised.dataset_version == "train_v2"
    assert revised.change_source == "hitl"
    assert revised.original_request == state.user_request
    assert state.goal_contract_history[0].dataset_version is None


def test_removing_core_evidence_changes_contract_based_goal_coverage():
    state = _file_state()
    ensure_goal_contract(state)
    state.tool_calls = [{"tool": "compare_models", "tool_call_id": "file-1"}]
    state.observations = [ToolResult(success=True, data=[{"structure_type": "linear", "mae": 1.0}])]
    state.evidence = [
        Evidence(
            evidence_id="ev-file",
            claim="file MAE",
            value=[{"mae": 1.0}],
            source_type="file",
            source="model_v1.csv",
            tool_call_id="file-1",
        )
    ]
    assert assess_goal_coverage(state).status == "SATISFIED"
    state.evidence.clear()
    state.observations.clear()
    assert assess_goal_coverage(state).status == "UNVERIFIABLE"


def test_unfrozen_contract_cannot_be_mutated_by_a_normal_runtime_path():
    state = _file_state()
    state.goal_contract = GoalContract(original_request=state.user_request, frozen=False)
    with pytest.raises(ValueError, match="immutable"):
        ensure_goal_contract(state)
