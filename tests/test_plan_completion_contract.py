"""Strict, evidence-backed Plan completion and dependency regression.

No PostgreSQL or model calls: the runtime must never promote a successful
preliminary tool into completion of a multi-tool PlanStep.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.decision_node import eligible_call_tools, eligible_plan_steps, structured_call
from app.agents.plan_protocol import satisfied
from app.agents.runtime import DecisionRuntime
from app.models.schemas import AgentDecision, PlanStep, ScientificAgentState


def _state():
    planning = PlanStep(
        step_id="1", goal="generate and validate SQL",
        selected_tools=["text_to_sql", "query_checker"], status="running",
        observations=[{"tool_call_id": "tool-1", "tool": "text_to_sql", "success": True}],
    )
    execute = PlanStep(
        step_id="2", goal="run verified SQL",
        selected_tools=["execute_readonly_sql"], depends_on=["1"],
    )
    return ScientificAgentState(
        user_id="unit", thread_id="unit", goal="count samples",
        allowed_tools=["text_to_sql", "query_checker", "execute_readonly_sql"],
        schema_cache={"predictions": [{"name": "id", "type": "text"}]},
        plan=[planning, execute],
    )


def test_partial_sql_generation_cannot_unlock_sql_execution():
    state = _state()
    assert not satisfied(state.plan[0])
    assert [s.step_id for s in eligible_plan_steps(state)] == ["1"]
    assert "execute_readonly_sql" not in eligible_call_tools(state)


def test_even_incorrect_completed_status_cannot_unlock_unverified_step():
    state = _state()
    state.plan[0].status = "completed"
    assert not satisfied(state.plan[0])
    assert not eligible_plan_steps(state)
    assert "execute_readonly_sql" not in eligible_call_tools(state)


def test_runtime_rejects_partial_completion_without_mutating_step():
    runtime = DecisionRuntime.__new__(DecisionRuntime)
    state = _state()
    decision = AgentDecision(action="FINISH", completed_step_ids=["1"])
    captured = []
    config = {"configurable": {"_run": {"emit": captured.append}}}
    with pytest.raises(ValueError, match="completion condition not met"):
        runtime._validate_progress(state, decision, config)
    assert [s.status for s in state.plan] == ["running", "pending"]
    assert captured == []


def test_partial_dependency_cannot_be_bypassed_by_model_completion_hint():
    runtime = DecisionRuntime.__new__(DecisionRuntime)
    state = _state()
    decision = AgentDecision(
        action="CALL_TOOL", tool_name="execute_readonly_sql",
        step_id="2", completed_step_ids=["1"],
    )
    captured = []
    config = {"configurable": {"_run": {"emit": captured.append}}}
    with pytest.raises(ValueError, match="plan dependency not completed"):
        runtime._validate_progress(state, decision, config)
    assert [s.status for s in state.plan] == ["running", "pending"]
    assert captured == []


def test_validated_sql_observation_unlocks_dependent_execution_step():
    runtime = DecisionRuntime.__new__(DecisionRuntime)
    state = _state()
    state.plan[0].observations.append(
        {"tool_call_id": "tool-2", "tool": "query_checker", "success": True}
    )
    assert satisfied(state.plan[0])
    assert [step.step_id for step in eligible_plan_steps(state)] == ["1", "2"]
    decision = AgentDecision(
        action="CALL_TOOL", tool_name="execute_readonly_sql",
        step_id="2", completed_step_ids=["1"],
    )
    captured = []
    config = {"configurable": {"_run": {"emit": captured.append}}}
    runtime._validate_progress(state, decision, config)
    assert state.plan[0].status == "completed"
    assert state.plan[1].status == "running"
    assert state.current_step == 1


def test_structured_decision_schema_only_offers_fully_satisfied_steps(monkeypatch):
    contracts = []

    class FakeModel:
        def with_structured_output(self, contract, **kwargs):
            contracts.append(contract)
            return self

        async def ainvoke(self, *args, **kwargs):
            return AgentDecision(action="ANSWER").model_dump(mode="json")

    monkeypatch.setattr(
        "app.agents.decision_node.configured_llm",
        lambda **kwargs: FakeModel(),
    )
    state = _state()
    asyncio.run(structured_call(
        AgentDecision, "test", {}, tool_names=["text_to_sql", "query_checker"],
        plan=state.plan,
    ))
    assert contracts[-1]["properties"]["completed_step_ids"]["maxItems"] == 0
    state.plan[0].observations.append({"tool": "query_checker", "success": True})
    asyncio.run(structured_call(
        AgentDecision, "test", {}, tool_names=["text_to_sql", "query_checker"],
        plan=state.plan,
    ))
    assert contracts[-1]["properties"]["completed_step_ids"]["items"]["enum"] == ["1"]
