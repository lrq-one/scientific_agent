"""P1.4 deterministic executor tests; safe path only, no provider calls."""
from __future__ import annotations

from app.agents.runtime import DecisionRuntime
from app.models.schemas import ScientificAgentState, ToolResult
from app.tools.registry import ToolRegistry


class _Owner:
    def __init__(self):
        self.tool_registry = ToolRegistry()


def _runtime():
    runtime = object.__new__(DecisionRuntime)
    runtime.owner = _Owner()
    return runtime


def _database_state():
    state = ScientificAgentState(
        user_id="offline",
        thread_id="deterministic",
        goal="count structure_type for train_v3",
        user_request="count structure_type for train_v3",
        allowed_tools=["search_schema", "text_to_sql", "query_checker", "execute_readonly_sql"],
        available_tools=["database"],
        grounding_ready=True,
    )
    state.schema_cache = {"molecules": [{"name": "structure_type"}]}
    return state


def test_unique_sql_generation_after_schema_is_deterministic():
    runtime = _runtime()
    state = _database_state()
    state.tool_calls = [{"tool": "search_schema", "tool_call_id": "schema-1"}]
    state.observations = [ToolResult(success=True, data=[{"table": "molecules"}])]
    decision = runtime._deterministic_decision(state, {"configurable": {}})
    assert decision is not None
    assert decision.tool_name == "text_to_sql"
    assert decision.tool_arguments == {"goal": state.goal}


def test_checker_and_execute_are_each_deterministic_only_after_prerequisite():
    runtime = _runtime()
    state = _database_state()
    sql = "SELECT structure_type, count(*) FROM molecules GROUP BY structure_type"
    state.tool_calls = [
        {"tool": "search_schema", "tool_call_id": "schema-1"},
        {"tool": "text_to_sql", "tool_call_id": "sql-1"},
    ]
    state.observations = [
        ToolResult(success=True, data=[{"table": "molecules"}]),
        ToolResult(success=True, data={"sql": sql, "params": {}}, metadata={"sql_candidate_status": "scope_verified"}),
    ]
    decision = runtime._deterministic_decision(state, {"configurable": {}})
    assert decision is not None and decision.tool_name == "query_checker"
    state.tool_calls.append({"tool": "query_checker", "tool_call_id": "check-1"})
    state.observations.append(ToolResult(success=True, data={"valid": True}, metadata={
        "sql_candidate": {"sql": sql, "params": {}}, "sql_candidate_status": "checked",
    }))
    decision = runtime._deterministic_decision(state, {"configurable": {}})
    assert decision is not None and decision.tool_name == "execute_readonly_sql"
    assert decision.tool_arguments == {"sql": sql, "params": {}}


def test_ambiguous_file_arguments_return_to_llm_instead_of_guessing():
    runtime = _runtime()
    state = ScientificAgentState(
        user_id="offline",
        thread_id="ambiguous",
        goal="compare model files",
        allowed_tools=["calculate_metrics", "compare_models"],
        available_tools=["file"],
        available_files=["model_v1.csv", "model_v2.csv", "model_v3.csv"],
        grounding_ready=True,
    )
    assert runtime._deterministic_decision(state, {"configurable": {}}) is None


def test_deterministic_executor_keeps_auditable_decision_source_contract():
    runtime = _runtime()
    state = _database_state()
    state.tool_calls = [{"tool": "search_schema", "tool_call_id": "schema-1"}]
    state.observations = [ToolResult(success=True, data=[{"table": "molecules"}])]
    decision = runtime._deterministic_decision(state, {"configurable": {}})
    assert decision.reason_summary.startswith("deterministic_executor")
