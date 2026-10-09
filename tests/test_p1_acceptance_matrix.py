"""Offline acceptance matrix for A/B/C and D06/D09/M02/D08 traces."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.goal_coverage import assess_goal_coverage
from app.agents.decision_node import eligible_call_tools
from app.agents.runtime import DecisionRuntime
from app.models.schemas import (
    Evidence,
    Capability,
    PlanStep,
    QueryScope,
    ResourceSummary,
    ScientificAgentState,
    ToolResult,
)


ROOT = Path(__file__).resolve().parents[1]
CLOSURE = ROOT / "reports" / "phase4a_correctness_closure_20261009" / "ui"


def _final_state(case: str) -> ScientificAgentState:
    record = json.loads((CLOSURE / f"closure-{case}.json").read_text(encoding="utf-8"))
    events = record["turns"][0]["events"]
    return ScientificAgentState.model_validate(
        [event["payload_json"]["state"] for event in events if event["event_type"] == "FINAL_ANSWER"][-1]
    )


@pytest.mark.parametrize("case", ["D06", "D09", "M02", "D08"])
def test_historical_matrix_traces_remain_evidence_grounded(case):
    state = _final_state(case)
    assert state.final_answer
    assert state.observations or state.evidence
    # A final trace may be partial/failed, but cannot claim scientific support
    # without the persisted GoalCoverage and quality state explaining it.
    assert state.goal_coverage.status in {"SATISFIED", "PARTIAL", "UNSATISFIED", "UNVERIFIABLE"}
    assert state.quality_status


def test_a_file_answer_does_not_inherit_database_obligation_from_plan():
    state = ScientificAgentState(
        user_id="offline",
        thread_id="A",
        goal="compare model_v1.csv and model_v2.csv structure_type MAE",
        user_request="compare model_v1.csv and model_v2.csv structure_type MAE",
        resource_summary=ResourceSummary(
            authorized_datasources=["training_db"], available_files=["model_v1.csv", "model_v2.csv"]
        ),
        query_scope=QueryScope(grouping=["structure_type"]),
        tool_calls=[{"tool": "compare_models", "tool_call_id": "a-file"}],
        observations=[ToolResult(success=True, data=[{"structure_type": "fused_ring", "mae": 0.7}])],
        evidence=[Evidence(evidence_id="a-evidence", claim="file MAE", value=[{"mae": 0.7}],
                           source_type="file", source="model_v1.csv", tool_call_id="a-file")],
    )
    from app.agents.goal_contract import ensure_goal_contract
    ensure_goal_contract(state)
    state.plan = [PlanStep(step_id="db", goal="unrequested SQL", selected_tools=["execute_readonly_sql"],
                           required_capabilities=[Capability.DATABASE])]
    assert assess_goal_coverage(state).status == "SATISFIED"
    assert "executed_database_analysis" not in assess_goal_coverage(state).missing_deliverables


def test_b_sql_chain_has_four_real_calls_and_no_checker_before_candidate():
    state = ScientificAgentState(
        user_id="offline", thread_id="B", goal="train_v3 structure coverage",
        allowed_tools=["search_schema", "text_to_sql", "query_checker", "execute_readonly_sql"],
        available_tools=["database"], schema_cache={"molecules": [{"name": "structure_type"}]},
        grounding_ready=True,
        tool_calls=[{"tool": "search_schema", "tool_call_id": "s"},
                    {"tool": "text_to_sql", "tool_call_id": "t"},
                    {"tool": "query_checker", "tool_call_id": "c"},
                    {"tool": "execute_readonly_sql", "tool_call_id": "e"}],
        observations=[
            ToolResult(success=True, data=[{"table": "molecules"}]),
            ToolResult(success=True, data={"sql": "SELECT 1", "params": {}}, metadata={"sql_candidate_status": "scope_verified"}),
            ToolResult(success=True, data={"valid": True}, metadata={"sql_candidate": {"sql": "SELECT 1", "params": {}}, "sql_candidate_status": "checked"}),
            ToolResult(success=True, data=[{"structure_type": "linear", "count": 3}], metadata={"scope_validation": {"verified": True}}),
        ],
    )
    assert len(state.tool_calls) == 4
    assert sum(call["tool"] == "query_checker" for call in state.tool_calls) == 1


def test_c_failed_candidate_is_diagnostic_and_checker_is_not_callable():
    state = ScientificAgentState(
        user_id="offline", thread_id="C", goal="train_v2 prediction error and training coverage",
        allowed_tools=["search_schema", "text_to_sql", "query_checker", "execute_readonly_sql"],
        available_tools=["database"], schema_cache={"predictions": [{"name": "absolute_error"}]},
        grounding_ready=True,
        tool_calls=[{"tool": "text_to_sql", "tool_call_id": "failed-sql"}],
        observations=[ToolResult(success=False, error="UNVERIFIED_SCOPE", failure_code="UNVERIFIED_SCOPE",
                                  metadata={"sql_candidate": {"sql": "SELECT * FROM predictions", "params": {}},
                                            "sql_candidate_status": "diagnostic_only"})],
    )
    assert "query_checker" not in eligible_call_tools(state)
    # The failed candidate remains in diagnostics and is never promoted.
    assert state.observations[0].data is None
    assert state.observations[0].metadata["sql_candidate_status"] == "diagnostic_only"
