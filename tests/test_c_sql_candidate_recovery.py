"""Offline C replay and P0 SQLCandidate recovery contract.

These tests use in-memory state and a fixture schema only.  They do not call
Qwen or PostgreSQL and never mutate persisted task records.
"""
from __future__ import annotations

import asyncio
import threading
from time import monotonic

import pytest

from app.agents.decision_node import eligible_call_tools
from app.agents.goal_coverage import assess_goal_coverage
from app.agents.runtime import DecisionRuntime
from app.models.schemas import (Capability, Evidence, AgentDecision, PlanStep, QueryScope,
                                ResourceSummary, SQLCandidate, ScientificAgentState, ToolResult)
from app.services.query_scope import ScopeViolation, UnverifiedScope, validate_scope
from app.services.text2sql import SQLScopeValidationError
from app.tools.dispatcher import ToolDispatcher, ToolExecutionContext
from app.tools.registry import ToolChoice


SCHEMA = {
    "predictions": [
        {"name": "model_run_id", "type": "uuid"},
        {"name": "molecule_id", "type": "text"},
        {"name": "absolute_error", "type": "double precision"},
    ],
    "model_runs": [{"name": "id", "type": "uuid"}, {"name": "experiment_id", "type": "uuid"}],
    "experiments": [{"name": "id", "type": "uuid"}, {"name": "dataset_version_id", "type": "uuid"}],
    "dataset_versions": [{"name": "id", "type": "uuid"}, {"name": "version", "type": "text"}],
    "molecules": [{"name": "molecule_id", "type": "text"}, {"name": "structure_type", "type": "text"}],
    "training_memberships": [
        {"name": "dataset_version_id", "type": "uuid"},
        {"name": "molecule_id", "type": "text"},
        {"name": "split", "type": "text"},
    ],
}

V2_ID = "11000000-0000-0000-0000-000000000002"


def _failed_state() -> ScientificAgentState:
    scope = QueryScope(dataset_version="train_v2", dataset_version_id=V2_ID)
    candidate = {
        "sql": "SELECT p.absolute_error FROM predictions p LEFT JOIN training_memberships tm ON tm.molecule_id=p.molecule_id WHERE tm.dataset_version_id=%(dataset_version_id)s",
        "params": {"dataset_version_id": V2_ID},
    }
    result = ToolResult(
        success=False,
        error="UNVERIFIED_SCOPE: outer/cross/implicit joins require population proof beyond this checker",
        failure_code="UNVERIFIED_SCOPE",
        failed_stage="sql_scope_validation",
        metadata={
            "sql_candidate": candidate,
            "sql_candidate_status": "diagnostic_only",
            "scope_validation": {"verified": False, "status": "UNVERIFIED", "failure_code": "UNVERIFIED_SCOPE"},
        },
    )
    step = PlanStep(step_id="query", goal="generate and validate SQL", status="failed",
                    selected_tools=["text_to_sql", "query_checker"],
                    required_capabilities=[Capability.DATABASE])
    call = {
        "tool_call_id": "sql-1", "tool": "text_to_sql", "step_id": "query", "plan_id": "plan-1",
        "query_scope": scope.model_dump(mode="json"),
    }
    return ScientificAgentState(
        user_id="offline", thread_id="c-replay", goal="analyze prediction error and train_v2 coverage",
        user_request="analyze prediction error and train_v2 coverage",
        allowed_tools=["search_schema", "text_to_sql", "query_checker", "execute_readonly_sql"],
        available_tools=["database"], schema_cache=SCHEMA, query_scope=scope,
        plan=[step], plan_id="plan-1", relationships_cache=[
            {"source_table": "predictions", "source_column": "model_run_id", "target_table": "model_runs", "target_column": "id"},
            {"source_table": "model_runs", "source_column": "experiment_id", "target_table": "experiments", "target_column": "id"},
            {"source_table": "experiments", "source_column": "dataset_version_id", "target_table": "dataset_versions", "target_column": "id"},
            {"source_table": "training_memberships", "source_column": "dataset_version_id", "target_table": "dataset_versions", "target_column": "id"},
        ],
        tool_calls=[call], observations=[result], consecutive_failures=1,
    )


def test_failed_text2sql_candidate_is_diagnostic_and_query_checker_is_not_callable():
    state = _failed_state()
    callable_tools = eligible_call_tools(state)
    assert "text_to_sql" in callable_tools
    assert "query_checker" not in callable_tools

    runtime = DecisionRuntime.__new__(DecisionRuntime)
    state.decision = AgentDecision(
        action="CALL_TOOL", tool_name="query_checker", step_id="query",
        tool_arguments={"sql": state.observations[0].metadata["sql_candidate"]["sql"],
                        "params": state.observations[0].metadata["sql_candidate"]["params"]},
    )
    with pytest.raises(ValueError, match="diagnostic-only"):
        runtime._arguments(state)


def test_successful_text2sql_candidate_unlocks_checker_without_using_failed_metadata():
    state = _failed_state()
    state.tool_calls.append({
        "tool_call_id": "sql-2", "tool": "text_to_sql", "step_id": "query", "plan_id": "plan-1",
        "query_scope": state.query_scope.model_dump(mode="json"),
    })
    state.observations.append(ToolResult(
        success=True,
        data={"sql": "SELECT 1", "params": {}},
        source="training_db",
        metadata={"sql_candidate_status": "scope_verified"},
    ))
    assert "query_checker" in eligible_call_tools(state)


def test_scope_failure_classes_are_distinct_and_still_fail_closed():
    scope = QueryScope(dataset_version="train_v2", dataset_version_id=V2_ID)
    missing_version = "SELECT count(*) FROM training_memberships tm"
    with pytest.raises(ScopeViolation, match="SCOPE_VIOLATION"):
        validate_scope(missing_version, {}, scope, SCHEMA)

    hard_to_prove = (
        "SELECT p.absolute_error FROM predictions p "
        "LEFT JOIN training_memberships tm ON tm.molecule_id=p.molecule_id "
        "WHERE tm.dataset_version_id=%(dataset_version_id)s"
    )
    with pytest.raises(UnverifiedScope, match="UNVERIFIED_SCOPE"):
        validate_scope(hard_to_prove, {"dataset_version_id": V2_ID}, scope, SCHEMA)


def test_failed_candidate_metadata_is_diagnostic_for_both_scope_failure_classes():
    for error, code in [(ScopeViolation("missing version"), "SCOPE_VIOLATION"),
                        (UnverifiedScope("outer join"), "UNVERIFIED_SCOPE")]:
        failure = SQLScopeValidationError(error, SQLCandidate(sql="SELECT 1"), {})
        assert failure.metadata["sql_candidate_status"] == "diagnostic_only"
        assert failure.metadata["scope_validation"]["failure_code"] == code
        assert failure.metadata["recovery"]["candidate_role"] == "diagnostic_only"


def test_scope_recovery_is_one_targeted_repair_then_deterministic_stop():
    state = _failed_state()
    runtime = DecisionRuntime.__new__(DecisionRuntime)
    events = []
    runtime._emit = lambda _config, kind, _message, **payload: events.append((kind, payload))
    first, telemetry = runtime._directed_scope_recovery(state, {"configurable": {}})
    assert first.action == "CALL_TOOL" and first.tool_name == "text_to_sql"
    assert telemetry["llm_called"] is False
    assert "diagnostic_only" in first.tool_arguments["repair_feedback"]
    assert "relationships" in first.tool_arguments["repair_feedback"]
    assert "resource_binding" in first.tool_arguments["repair_feedback"]
    assert any(kind == "RECOVERY_DECISION" for kind, _ in events)

    second_call = dict(state.tool_calls[0], tool_call_id="sql-2")
    second_result = state.observations[0].model_copy(deep=True)
    state.tool_calls.append(second_call)
    state.observations.append(second_result)
    state.consecutive_failures = 2
    terminal, terminal_telemetry = runtime._directed_scope_recovery(state, {"configurable": {}})
    assert terminal.action == "FINISH"
    assert terminal_telemetry["llm_called"] is False
    assert state.replan_count == 0


def test_runtime_recovery_does_not_call_decision_llm_for_the_repair_hop():
    state = _failed_state()
    runtime = DecisionRuntime.__new__(DecisionRuntime)
    runtime.decider = type("NeverCalled", (), {
        "decide": lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("decision LLM must not run"))
    })()
    events = []
    config = {"configurable": {"_run": {
        "cancel": threading.Event(), "deadline": monotonic() + 5, "emit": events.append,
    }}}
    output = runtime.decision({"agent": state.model_dump(mode="json")}, config)
    resumed = ScientificAgentState.model_validate(output["agent"])
    assert resumed.decision.action == "CALL_TOOL"
    assert resumed.decision.tool_name == "text_to_sql"
    agent_decisions = [event for event in events if event.event == "AGENT_DECISION"]
    assert agent_decisions and agent_decisions[-1].data["llm_telemetry"]["llm_called"] is False


def test_c_queries_can_be_split_into_two_scope_verifiable_populations():
    scope = QueryScope(dataset_version="train_v2", dataset_version_id=V2_ID)
    prediction_sql = (
        "SELECT m.structure_type, AVG(p.absolute_error) AS mae "
        "FROM predictions p JOIN model_runs mr ON mr.id=p.model_run_id "
        "JOIN experiments e ON e.id=mr.experiment_id "
        "JOIN dataset_versions dv ON dv.id=e.dataset_version_id "
        "JOIN molecules m ON m.molecule_id=p.molecule_id "
        "WHERE dv.id=%(dataset_version_id)s GROUP BY m.structure_type"
    )
    coverage_sql = (
        "SELECT m.structure_type, tm.split, COUNT(DISTINCT tm.molecule_id) AS molecule_count "
        "FROM training_memberships tm JOIN molecules m ON m.molecule_id=tm.molecule_id "
        "WHERE tm.dataset_version_id=%(dataset_version_id)s GROUP BY m.structure_type, tm.split"
    )
    for sql in (prediction_sql, coverage_sql):
        result = validate_scope(sql, {"dataset_version_id": V2_ID}, scope, SCHEMA)
        assert result["verified"] is True and result["partial_scope"] is False


def test_a_file_goal_and_b_train_v3_sql_goal_remain_satisfied():
    file_state = ScientificAgentState(
        user_id="offline", thread_id="a", goal="compare model files by structure_type",
        requested_dimensions=["structure_type"], schema_cache={"rows": [{"name": "structure_type"}]},
        plan=[PlanStep(step_id="1", goal="compare files", selected_tools=["compare_models"],
                       required_capabilities=[Capability.FILE])],
        tool_calls=[{"tool_call_id": "file-1", "tool": "compare_models"}],
        observations=[ToolResult(success=True, data=[{"structure_type": "fused_ring", "mae_left": 0.75}])],
        evidence=[Evidence(evidence_id="ev-a", claim="file comparison", value=[{"structure_type": "fused_ring"}],
                           source_type="file", source="model_v1.csv", tool_call_id="file-1")],
    )
    assert assess_goal_coverage(file_state).status == "SATISFIED"

    db_scope = QueryScope(dataset_version="train_v3")
    db_state = ScientificAgentState(
        user_id="offline", thread_id="b", goal="count train_v3 structure_type coverage",
        requested_dimensions=["structure_type"], datasource_id="training_db", schema_cache={"rows": [{"name": "structure_type"}]},
        query_scope=db_scope,
        plan=[PlanStep(step_id="1", goal="execute coverage", selected_tools=["execute_readonly_sql"],
                       required_capabilities=[Capability.DATABASE])],
        tool_calls=[{"tool_call_id": "sql-b", "tool": "execute_readonly_sql"}],
        observations=[ToolResult(success=True, source="training_db",
                                  data=[{"structure_type": "linear", "molecule_count": 3}],
                                  metadata={"scope_validation": {"verified": True}})],
        evidence=[Evidence(evidence_id="ev-b", claim="train_v3 coverage", value=[{"structure_type": "linear"}],
                           source_type="database", source="training_db", tool_call_id="sql-b")],
    )
    assert assess_goal_coverage(db_state).status == "SATISFIED"


def _append_scope_failure(state, *, population_id=None, sql=None):
    candidate = {
        "sql": sql or state.observations[0].metadata["sql_candidate"]["sql"],
        "params": {"dataset_version_id": V2_ID},
    }
    state.tool_calls.append({
        "tool_call_id": f"sql-{len(state.tool_calls) + 1}", "tool": "text_to_sql",
        "step_id": "query", "plan_id": "plan-1", "population_id": population_id,
        "query_scope": state.query_scope.model_dump(mode="json"),
    })
    original = state.observations[0]
    validation = {"verified": False, "status": "UNVERIFIED", "failure_code": "UNVERIFIED_SCOPE"}
    if original.metadata.get("scope_validation", {}).get("reason"):
        validation["reason"] = original.metadata["scope_validation"]["reason"]
    state.observations.append(ToolResult(
        success=False,
        error=original.error,
        failure_code="UNVERIFIED_SCOPE", failed_stage="sql_scope_validation",
        metadata={
            "sql_candidate": candidate, "sql_candidate_status": "diagnostic_only",
            "scope_validation": validation,
        },
    ))


def test_scope_recovery_budget_is_per_candidate_population_context():
    runtime = DecisionRuntime.__new__(DecisionRuntime)
    runtime._emit = lambda *_args, **_kwargs: None
    state = _failed_state()
    first, _ = runtime._directed_scope_recovery(state, {"configurable": {}})
    assert first.action == "CALL_TOOL"

    # The same candidate and population consumes its one repair hop.
    _append_scope_failure(state)
    terminal, telemetry = runtime._directed_scope_recovery(state, {"configurable": {}})
    assert terminal.action == "FINISH"
    assert telemetry["repair_attempt"] == 2

    # A different population is a separate verified context and gets its own hop.
    _append_scope_failure(state, population_id="coverage")
    next_context, telemetry = runtime._directed_scope_recovery(state, {"configurable": {}})
    assert next_context.action == "CALL_TOOL"
    assert telemetry["repair_attempt"] == 1


def test_scope_recovery_budget_resets_for_changed_sql_but_not_goal_rewrite():
    runtime = DecisionRuntime.__new__(DecisionRuntime)
    runtime._emit = lambda *_args, **_kwargs: None
    state = _failed_state()
    _append_scope_failure(state)
    state.goal = "rewritten natural language request with the same SQL context"
    terminal, _ = runtime._directed_scope_recovery(state, {"configurable": {}})
    assert terminal.action == "FINISH"

    _append_scope_failure(state, sql="SELECT p.absolute_error FROM predictions p WHERE p.model_run_id=%(run_id)s")
    repaired, telemetry = runtime._directed_scope_recovery(state, {"configurable": {}})
    assert repaired.action == "CALL_TOOL"
    assert telemetry["repair_attempt"] == 1


def test_scope_recovery_allows_second_population_after_first_population_succeeds():
    runtime = DecisionRuntime.__new__(DecisionRuntime)
    runtime._emit = lambda *_args, **_kwargs: None
    state = _failed_state()
    state.tool_calls[0]["population_id"] = "prediction_error"
    # A successful candidate does not erase the first diagnostic event, but a
    # later independent population still receives an independent budget.
    state.tool_calls.append({"tool_call_id": "sql-success", "tool": "text_to_sql",
                             "population_id": "prediction_error", "query_scope": state.query_scope.model_dump(mode="json")})
    state.observations.append(ToolResult(success=True, data={"sql": "SELECT 1", "params": {}},
                                         metadata={"sql_candidate_status": "scope_verified"}))
    _append_scope_failure(state, population_id="training_coverage")
    repaired, telemetry = runtime._directed_scope_recovery(state, {"configurable": {}})
    assert repaired.action == "CALL_TOOL"
    assert telemetry["repair_attempt"] == 1


def test_query_checker_and_executor_record_checked_and_executed_candidate_states():
    class FakeDatabase:
        def check_query(self, sql, params):
            return ToolResult(success=True, data={"valid": True}, source="training_db")

        def execute(self, sql, params):
            return ToolResult(success=True, data=[{"value": 1}], source="training_db",
                              metadata={"sql": sql, "params": params})

    dispatcher = ToolDispatcher(workspace=None, storage=None, files=None, mcp=None,
                                artifacts=None,
                                database_factory=lambda *_args: FakeDatabase())
    resources = ResourceSummary(authorized_datasources=["training_db"])
    checked = asyncio.run(dispatcher._execute(
        ToolChoice(tool="query_checker", arguments={"sql": "SELECT 1", "params": {}}, reason="offline"),
        ToolExecutionContext(user_id="u", thread_id="t", resources=resources,
                             datasource_id="training_db")))
    executed = asyncio.run(dispatcher._execute(
        ToolChoice(tool="execute_readonly_sql", arguments={"sql": "SELECT 1", "params": {}}, reason="offline"),
        ToolExecutionContext(user_id="u", thread_id="t", resources=resources,
                             datasource_id="training_db")))
    assert checked.metadata["sql_candidate_status"] == "checked"
    assert executed.metadata["sql_candidate_status"] == "executed"
