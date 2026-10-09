import asyncio
import threading
from time import monotonic
from types import SimpleNamespace

import pytest

from app.agents.runtime import DecisionRuntime
from app.agents.planning_policy import PlanningPolicy
from app.models.schemas import AgentDecision, Capability, Evidence, PlanStep, ScientificAgentState, ToolResult
from app.agents.scientific_agent import ScientificAgent
from app.agents.decision_node import eligible_call_tools, structured_call
from app.services.grounded_response import contains_persisted_sql
from app.services.execution_context import execution_identity


def test_graph_async_calls_keep_application_loop_and_identity():
    async def scenario():
        application_loop = asyncio.get_running_loop()
        runtime = DecisionRuntime.__new__(DecisionRuntime)
        config = {"configurable": {"_run": {
            "loop": application_loop, "deadline": monotonic() + 5, "cancel": threading.Event(),
        }}}
        token = execution_identity.set({"task_id": "same-task", "conversation_id": "same-conversation"})
        async def observe():
            return asyncio.get_running_loop(), execution_identity.get()
        try:
            for _ in range(3):
                loop, identity = await asyncio.to_thread(runtime._await, observe(), config)
                assert loop is application_loop
                assert identity == execution_identity.get()
        finally:
            execution_identity.reset(token)
    asyncio.run(scenario())


def test_evidence_conflicts_compare_same_request_not_same_tool():
    state = ScientificAgentState(user_id="test", thread_id="thread", goal="inspect recorded data",
        tool_calls=[{"tool_call_id": "lookup", "tool": "execute_readonly_sql", "arguments": {"sql": "SELECT id FROM model_runs"}},
                    {"tool_call_id": "analysis", "tool": "execute_readonly_sql", "arguments": {"sql": "SELECT structure_type FROM molecules"}}],
        evidence=[Evidence(evidence_id="ev-1", claim="execute_readonly_sql 的实际返回结果", value=[{"id": "run-1"}],
                           source_type="database", source="training_db", tool_call_id="lookup"),
                  Evidence(evidence_id="ev-2", claim="execute_readonly_sql 的实际返回结果", value=[{"structure_type": "cyclic"}],
                           source_type="database", source="training_db", tool_call_id="analysis")])
    assert not any("矛盾" in issue for issue in ScientificAgent._evidence_quality_issues(state))
    state.tool_calls[1]["arguments"] = state.tool_calls[0]["arguments"]
    assert any("矛盾" in issue for issue in ScientificAgent._evidence_quality_issues(state))


def test_wire_tool_choices_obey_plan_scope_and_observed_dependencies():
    state = ScientificAgentState(user_id="test", thread_id="thread", goal="analysis",
        allowed_tools=["search_schema", "text_to_sql", "query_checker", "execute_readonly_sql"],
        plan=[PlanStep(step_id="schema", goal="discover", selected_tools=["search_schema"], status="running"),
              PlanStep(step_id="query", goal="generate", selected_tools=["text_to_sql"], depends_on=["schema"]),
              PlanStep(step_id="execute", goal="execute", selected_tools=["execute_readonly_sql"], depends_on=["query"])])
    assert eligible_call_tools(state) == ["search_schema"]
    state.plan[0].observations.append({"tool": "search_schema", "success": True})
    assert eligible_call_tools(state) == ["search_schema"]
    state.schema_cache={"molecules":[{"name":"id","type":"uuid"}]}
    assert eligible_call_tools(state) == ["search_schema", "text_to_sql"]
    assert "query_checker" not in eligible_call_tools(state)


def test_export_rows_must_reference_real_results_not_generated_assertions():
    runtime = DecisionRuntime.__new__(DecisionRuntime)
    state = ScientificAgentState(user_id="test", thread_id="thread", goal="export results",
        observations=[ToolResult(success=True, data=[{"structure_type": "cyclic", "n": 2}])],
        tool_calls=[{"tool_call_id": "tool-1", "tool": "execute_readonly_sql"}],
        decision=AgentDecision(action="CALL_TOOL", tool_name="save_result_table",
                               tool_arguments={"rows": [{"finding": "coverage caused errors"}]}))
    with pytest.raises(ValueError, match="lineage"):
        runtime._arguments(state)
    state.decision.input_refs = {"rows": "observation:tool-1:data"}
    assert runtime._arguments(state)["rows"] == state.observations[0].data


def test_rejected_call_cannot_partially_complete_its_own_step():
    runtime = DecisionRuntime.__new__(DecisionRuntime)
    state = ScientificAgentState(user_id="test", thread_id="thread", goal="execute query",
        plan=[PlanStep(step_id="query", goal="generate/check/execute", status="running",
            selected_tools=["execute_readonly_sql"], observations=[{"tool": "query_checker", "success": True}])])
    decision = AgentDecision(action="CALL_TOOL", tool_name="execute_readonly_sql", step_id="query", completed_step_ids=["query"])
    events = []
    config = {"configurable": {"_run": {"emit": events.append}}}
    with pytest.raises(ValueError, match="active CALL_TOOL step"):
        runtime._validate_progress(state, decision, config)
    assert state.plan[0].status == "running" and events == []
    decision.completed_step_ids = []
    runtime._validate_progress(state, decision, config)
    assert state.plan[0].status == "running"


def test_new_sql_uses_existing_generator_instead_of_controller_authorship():
    runtime = DecisionRuntime.__new__(DecisionRuntime)
    state = ScientificAgentState(user_id="test", thread_id="thread", goal="count actual data",
        decision=AgentDecision(action="CALL_TOOL", tool_name="execute_readonly_sql", tool_arguments={"sql": "SELECT COUNT(*) FROM molecules"}))
    with pytest.raises(ValueError, match="SQLCandidate lineage"):
        runtime._arguments(state)
    state.tool_calls = [{"tool_call_id": "tool-1", "tool": "text_to_sql"}]
    state.observations = [ToolResult(success=True, data={"sql": "SELECT COUNT(*) FROM molecules", "params": {}})]
    assert runtime._arguments(state)["sql"] == state.observations[0].data["sql"]
    state.observations = []; state.tool_calls = []
    state.user_request = "请校验这个旧查询: SELECT COUNT(*) FROM molecules"
    assert runtime._arguments(state)["sql"] == "SELECT COUNT(*) FROM molecules"


def test_sql_response_accepts_formatting_but_not_literal_or_scope_changes():
    original = "SELECT structure_type as grp FROM molecules WHERE dataset_version = 'train_v3'"
    assert contains_persisted_sql(f"```sql\n{original.replace('as grp', 'AS grp')}\n```", original)
    assert not contains_persisted_sql(f"```sql\n{original.replace('train_v3', 'train_v2')}\n```", original)
    assert not contains_persisted_sql("SQL is a query language", original)


def test_free_plan_validates_exact_identifiers_and_dependencies():
    plan = [
        PlanStep(step_id="inspect-data", goal="inspect actual schema", selected_tools=["inspect_table"], required_capabilities=[Capability.FILE]),
        PlanStep(step_id="measure", goal="measure observed error", depends_on=["inspect-data"], selected_tools=["calculate_metrics"]),
    ]
    assert PlanningPolicy.validate_plan(plan, {"inspect_table", "calculate_metrics"}, {"file"}) == plan
    plan[1].depends_on = ["id:inspect-data"]
    with pytest.raises(ValueError, match="unknown dependency"):
        PlanningPolicy.validate_plan(plan, {"inspect_table", "calculate_metrics"}, {"file"})
    plan[1].depends_on = ["inspect-data"]
    plan[0].selected_tools = ["name:inspect_table"]
    with pytest.raises(ValueError, match="invalid tool identifiers"):
        PlanningPolicy.validate_plan(plan, {"inspect_table", "calculate_metrics"}, {"file"})


@pytest.mark.asyncio
async def test_wire_completion_ids_only_allow_observed_steps(monkeypatch):
    contracts = []
    class Model:
        def with_structured_output(self, schema, **kwargs):
            contracts.append(schema)
            return self
        async def ainvoke(self, messages, config=None):
            return AgentDecision(action="ANSWER").model_dump(mode="json")
    monkeypatch.setattr("app.agents.decision_node.configured_llm", lambda **kwargs: Model())
    pending = PlanStep(step_id="unstarted", goal="read", selected_tools=["read_csv"])
    observed = PlanStep(step_id="observed", goal="read", selected_tools=["read_csv"],
        status="running", observations=[{"tool_call_id": "tool-1", "tool": "read_csv", "success": True}])
    await structured_call(AgentDecision, "test", {}, tool_names=["read_csv"], plan=[pending, observed])
    properties = contracts[-1]["properties"]
    assert properties["completed_step_ids"]["items"]["enum"] == ["observed"]
    assert properties["plan"]["maxItems"] == 6
    await structured_call(AgentDecision, "test", {}, tool_names=["read_csv"], plan=[pending])
    assert contracts[-1]["properties"]["completed_step_ids"]["maxItems"] == 0


def test_call_tool_echoed_plan_does_not_erase_observations():
    async def scenario():
        runtime = DecisionRuntime.__new__(DecisionRuntime)
        specs = {name: SimpleNamespace(model_dump=lambda: {}) for name in ("inspect_table", "calculate_metrics")}
        runtime.owner = SimpleNamespace(tool_registry=SimpleNamespace(specs=specs),
                                        skills=SimpleNamespace(execution_context=lambda _: ""))
        state = ScientificAgentState(user_id="test", thread_id="thread", goal="evaluate recorded results",
            allowed_tools=list(specs), available_tools=["file"], tool_call_count=1,
            tool_calls=[{"tool_call_id": "tool-1", "tool": "inspect_table", "success": True}],
            observations=[ToolResult(success=True, data={"columns": ["observed_rt", "predicted_rt"]})],
            plan=[PlanStep(step_id="inspect", goal="inspect", selected_tools=["inspect_table"], status="running",
                           observations=[{"tool_call_id": "tool-1", "tool": "inspect_table", "success": True}]),
                  PlanStep(step_id="measure", goal="measure", selected_tools=["calculate_metrics"], depends_on=["inspect"])])
        async def decide(*_):
            return AgentDecision(action="CALL_TOOL", tool_name="calculate_metrics", step_id="measure",
                completed_step_ids=["inspect"], plan=[step.model_copy(update={"status": "pending", "observations": []}) for step in state.plan]), {"fallback": False}
        runtime.decider = SimpleNamespace(decide=decide)
        events = []
        config = {"configurable": {"_run": {"loop": asyncio.get_running_loop(), "deadline": monotonic() + 5,
                                             "cancel": threading.Event(), "emit": events.append}}}
        output = await asyncio.to_thread(runtime.decision, runtime._return(state), config)
        updated = runtime._state(output)
        assert updated.decision.action == "CALL_TOOL"
        assert updated.plan[0].status == "completed"
        assert updated.plan[0].observations == state.plan[0].observations
        assert updated.plan[1].status == "running"
        assert updated.observations == state.observations
        assert not any(event.event == "PLAN_CREATED" for event in events)
    asyncio.run(scenario())
