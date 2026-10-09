"""Offline regression: SQL Plan prerequisites and clean rerun failure reporting.

No Qwen, PostgreSQL, HTTP service or external tool is called.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.plan_protocol import ensure_sql_schema_entrypoint
from app.agents.runtime import DecisionRuntime
from app.agents.decision_node import eligible_call_tools
from app.models.schemas import AgentDecision, Capability, PlanStep, ScientificAgentState


def _state(*, schema=None, allowed=None):
    return ScientificAgentState(
        user_id="unit", thread_id="new-run", goal="real grouped MAE and CSV",
        available_tools=["database"],
        allowed_tools=allowed if allowed is not None
        else ["search_schema", "get_table_schema", "text_to_sql", "query_checker", "execute_readonly_sql"],
        schema_cache=schema or {},
    )


def _proposal():
    return [PlanStep(
        step_id="1", goal="generate SQL", selected_tools=["text_to_sql"],
        required_capabilities=[Capability.DATABASE],
    )]


def test_missing_schema_prerequisite_is_compiled_not_rejected():
    state = _state()
    prepared = ensure_sql_schema_entrypoint(state, _proposal(), set(state.allowed_tools))
    assert len(prepared) == 2
    producer = next(s for s in prepared if "get_table_schema" in s.selected_tools)
    sql = next(s for s in prepared if "text_to_sql" in s.selected_tools)
    assert sql.depends_on == [producer.step_id]
    assert set(producer.selected_tools) <= set(state.allowed_tools)
    assert producer.completion_condition is None  # runtime derives it from tool registry
    assert eligible_call_tools(state.model_copy(update={"plan": prepared})) == [
        "get_table_schema", "search_schema"
    ]
    assert not state.plan  # no in-place modification of source state


def test_runtime_installs_schema_first_without_plan_rejected(monkeypatch):
    from app.tools.registry import ToolRegistry
    state = _state()
    run = DecisionRuntime.__new__(DecisionRuntime)
    run.owner = SimpleNamespace(tool_registry=ToolRegistry())
    events = []
    run._emit = lambda config, kind, message, **kwargs: events.append(kind)
    run._apply_plan(state, AgentDecision(action="REPLAN", plan=_proposal()), {})
    assert len(state.plan) == 2
    assert state.control_observations[-1]["success"] is True
    assert "PLAN_CREATED" in events
    assert "PLAN_REJECTED" not in events
    assert "text_to_sql" not in eligible_call_tools(state)
    assert "get_table_schema" in eligible_call_tools(state)
    producer = next(s for s in state.plan if "get_table_schema" in s.selected_tools)
    producer.observations.append({"tool_call_id": "tool-schema", "tool": "get_table_schema",
                                  "success": True, "goal_satisfied": True})
    state.schema_cache = {"predictions": [{"name": "model_run_id"}]}
    from app.agents.plan_protocol import refresh_steps
    refresh_steps(state)
    assert "text_to_sql" in eligible_call_tools(state)


def test_already_callable_schema_step_is_not_modified():
    state = _state()
    steps = [
        PlanStep(step_id="1", goal="schema", selected_tools=["get_table_schema"]),
        PlanStep(step_id="2", goal="sql", selected_tools=["text_to_sql"], depends_on=["1"]),
    ]
    fixed = ensure_sql_schema_entrypoint(state, steps, set(state.allowed_tools))
    assert fixed is steps
    assert [s.step_id for s in fixed] == ["1", "2"]


def test_blocked_schema_step_gets_independent_authorized_entrypoint():
    state = _state()
    steps = [
        PlanStep(step_id="1", goal="sql", selected_tools=["text_to_sql"],
                 depends_on=["2"]),
        PlanStep(step_id="2", goal="blocked schema", selected_tools=["get_table_schema"],
                 depends_on=["1"]),
    ]
    fixed = ensure_sql_schema_entrypoint(state, steps, set(state.allowed_tools))
    producer = fixed[0]
    assert producer.step_id not in {"1", "2"}
    assert "get_table_schema" in producer.selected_tools
    assert producer.depends_on == []
    assert producer.step_id in fixed[1].depends_on
    assert "get_table_schema" in eligible_call_tools(state.model_copy(update={"plan": fixed}))


def test_no_authorized_schema_tool_fails_closed_without_state_mutation():
    state = _state(allowed=["text_to_sql", "execute_readonly_sql"])
    before = state.model_dump_json()
    with pytest.raises(ValueError, match="no authorized schema tool"):
        ensure_sql_schema_entrypoint(state, _proposal(), set(state.allowed_tools))
    assert state.model_dump_json() == before


def test_schema_cache_already_known_does_not_force_schema_tools():
    state = _state(schema={"predictions": [{"name": "id"}]})
    plan = _proposal()
    assert ensure_sql_schema_entrypoint(state, plan, set(state.allowed_tools)) is plan


def test_failed_rerun_with_no_new_evidence_does_not_reuse_historic_corruption_claim():
    run = DecisionRuntime.__new__(DecisionRuntime)
    run.owner = SimpleNamespace(_evidence_quality_issues=lambda state: [],
                                tool_registry=SimpleNamespace(all=lambda: []))
    run._check = lambda config: None
    emitted = []
    run._emit = lambda config, kind, message, **kwargs: emitted.append((kind, kwargs))
    run.responses = SimpleNamespace(generate=lambda *args, **kwargs:
                                    (_ for _ in ()).throw(AssertionError("No paid model calls for no-Evidence failures")))
    state = _state()
    # Finalize is only reachable from a terminal Decision in the real graph.
    state.decision = AgentDecision(action="FINISH", reason_summary="planning failed")
    state.conversation_context = {"previous_provenance": {
        "errors": ["DataCorrupted: invalid page in old PostgreSQL index"]}}
    state.errors = ["Plan requires inspected schema"]
    state.control_observations = [{
        "action": "REPLAN", "success": False, "failure_code": "PLAN_REJECTED",
        "failed_stage": "planning",
    }]
    final = ScientificAgentState.model_validate(run.finalize({"agent": state.model_dump(mode="json")}, {})["agent"])
    assert final.quality_status == "EXECUTION_FAILED"
    assert final.claims == []
    assert "PLAN_REJECTED" in final.final_answer
    assert "DataCorrupted" not in final.final_answer
    assert "invalid page" not in final.final_answer
    assert [name for name, payload in emitted if name == "FINAL_ANSWER"] == ["FINAL_ANSWER"]
    assert emitted[-1][1]["llm_telemetry"]["llm_called"] is False
