"""Offline contracts: no provider, browser, or live service calls."""
from types import SimpleNamespace
import asyncio
import json
from pathlib import Path

import pytest

from app.models.schemas import (AgentDecision, Capability, Evidence, GroundedResponse,
    PlanStep, QueryScope, ResourceSummary, ScientificAgentState, ToolResult)
from app.agents.plan_protocol import prepare_replacement
from app.agents.goal_coverage import assess_goal_coverage


@pytest.fixture(autouse=True)
def no_paid_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("real LLM forbidden in this offline suite")
    monkeypatch.setattr("app.agents.decision_node.configured_llm", forbidden)
    monkeypatch.setattr("langchain_openai.ChatOpenAI", forbidden)


def resources():
    return ResourceSummary(authorized_datasources=["db"], resource_metadata={
        "datasets": [{"id": "dataset", "name": "sample", "datasource_id": "db"}],
        "dataset_versions": [
            {"id": "id-a", "version": "v_a", "dataset_id": "dataset", "datasource_id": "db"},
            {"id": "id-b", "version": "v_b", "dataset_id": "dataset", "datasource_id": "db"}]})


def population_state():
    from app.models.schemas import PopulationRequest
    from app.services.resource_grounding import bind_populations, resolve_binding, resolve_scope
    query = "统计 db v_a train split 中 fused-ring 数量；并统计授权范围内全部版本 fused-ring 总数。"
    state = ScientificAgentState(user_id="u", thread_id="t", goal=query, user_request=query,
        resource_summary=resources(), grounding_ready=True, datasource_id="db")
    state.resource_binding = resolve_binding(query, state.resource_summary)
    state.query_scope = resolve_scope(query, state.resource_binding)
    state.populations = bind_populations(state, [
        PopulationRequest(source_text="v_a train split 中 fused-ring 数量", query_scope=QueryScope(
            dataset_version="v_a", split="train", filters={"structure_type": "fused-ring"})),
        PopulationRequest(source_text="授权范围内全部版本 fused-ring 总数", query_scope=QueryScope(
            all_versions=True, filters={"structure_type": "fused-ring"}))])
    return state


def test_population_contract_exists_and_default_is_backward_compatible():
    state = ScientificAgentState(user_id="u", thread_id="t", goal="old task")
    assert state.populations == []
    assert PlanStep(step_id="1", goal="old").population_id is None


def test_independent_train_and_all_version_survive_plan_replacement():
    state = population_state()
    proposals = [PlanStep(step_id=str(i + 1), goal=p.source_text, population_id=p.population_id,
                         selected_tools=["execute_readonly_sql"]) for i, p in enumerate(state.populations)]
    _, prepared = prepare_replacement(state, proposals)
    assert prepared[0].query_scope.dataset_version == "v_a"
    assert prepared[0].query_scope.split == "train"
    assert prepared[1].query_scope.dataset_version is None
    assert prepared[1].query_scope.split is None and prepared[1].query_scope.all_versions
    assert prepared[1].query_scope.authorized_version_ids == ["id-a", "id-b"]


def test_only_one_population_is_partial_not_satisfied():
    state = population_state()
    p = state.populations[0]
    state.tool_calls = [{"tool": "execute_readonly_sql", "tool_call_id": "q"}]
    state.observations = [ToolResult(success=True, source="db", data=[{"n": 2}], metadata={
        "population_id": p.population_id, "scope_validation": {"verified": True,
        "effective_query_scope": p.query_scope.model_dump(mode="json")}})]
    coverage = assess_goal_coverage(state)
    assert coverage.status == "PARTIAL"
    assert coverage.missing_populations == [state.populations[1].population_id]


def test_single_scope_compatibility():
    scope = QueryScope(dataset_version="v_a", split="train")
    state = ScientificAgentState(user_id="u", thread_id="t", goal="old", query_scope=scope)
    _, steps = prepare_replacement(state, [PlanStep(step_id="1", goal="old")])
    assert steps[0].query_scope == scope


@pytest.mark.parametrize("answer", ["整个数据库不存在 topology 字段。", "The database has no topology column."])
def test_top_k_does_not_support_global_absence(answer):
    from app.services.grounded_response import validate_public_claims
    facts = {"schema_retrieval": [{"complete": False, "retrieved_tables": ["molecules"]}]}
    assert validate_public_claims(GroundedResponse(answer=answer), facts)


@pytest.mark.parametrize("answer", ["MAE 的改善具有统计显著性。", "The MAE improvement is statistically significant."])
def test_descriptive_difference_is_not_a_significance_test(answer):
    from app.services.grounded_response import validate_public_claims
    facts = {"evidence": [{"evidence_id": "file", "value": {"mae_left": 1, "mae_right": .7}}]}
    assert validate_public_claims(GroundedResponse(answer=answer), facts)


SCHEMA = {"training_molecules": [{"name": k, "type": "text"} for k in
    ("dataset_version", "split", "structure_type", "molecule_id")]}


@pytest.mark.parametrize("index,sql", [
    (0, "SELECT count(*) AS n FROM training_molecules WHERE dataset_version='v_a' AND split='train' AND structure_type='fused-ring'"),
    (1, "SELECT count(*) AS n FROM training_molecules WHERE dataset_version IN ('v_a','v_b') AND structure_type='fused-ring'")])
def test_each_population_proves_its_own_actual_sql(index, sql):
    from app.services.query_scope import validate_scope
    scope = population_state().populations[index].query_scope
    checked = validate_scope(sql, {}, scope, SCHEMA)
    assert checked["verified"] and checked["effective_query_scope"] == scope.model_dump(mode="json")


@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM training_molecules WHERE dataset_version='v_b' AND split='train' AND structure_type='fused-ring'",
    "SELECT count(*) FROM training_molecules WHERE dataset_version='secret' AND split='train' AND structure_type='fused-ring'",
    "SELECT count(*) FROM training_molecules WHERE dataset_version='v_a' AND structure_type='fused-ring'"])
def test_wrong_version_unauthorized_version_or_split_rejected(sql):
    from app.services.query_scope import validate_scope
    with pytest.raises(ValueError):
        validate_scope(sql, {}, population_state().populations[0].query_scope, SCHEMA)


@pytest.mark.parametrize("predicate", ["dataset_version='v_a'", "dataset_version IN ('v_a','secret')", "1=1"])
def test_all_version_cannot_use_narrowed_or_unbounded_authorization(predicate):
    from app.services.query_scope import validate_scope
    with pytest.raises(ValueError):
        validate_scope("SELECT count(*) FROM training_molecules WHERE structure_type='fused-ring' AND " + predicate,
                       {}, population_state().populations[1].query_scope, SCHEMA)


def test_model_cannot_invent_or_broaden_population_intent():
    from app.models.schemas import PopulationRequest
    from app.services.resource_grounding import bind_populations
    state = population_state()
    with pytest.raises(ValueError, match="intent"):
        bind_populations(state, [PopulationRequest(source_text="v_a train split 中 fused-ring 数量",
            query_scope=QueryScope(all_versions=True))])
    with pytest.raises(ValueError, match="conflicts"):
        bind_populations(state, [PopulationRequest(source_text="v_a train split 中 fused-ring 数量",
            query_scope=QueryScope(dataset_version="secret", filters={"structure_type": "fused-ring"}))])


def test_forged_step_scope_is_replaced_by_its_trusted_population_not_global():
    state = population_state()
    _, steps = prepare_replacement(state, [PlanStep(step_id="1", goal="count", selected_tools=["execute_readonly_sql"],
        population_id=state.populations[0].population_id, query_scope=QueryScope(all_versions=True))])
    assert steps[0].query_scope == state.populations[0].query_scope
    with pytest.raises(ValueError, match="population_id"):
        prepare_replacement(state, [PlanStep(step_id="2", goal="count", selected_tools=["execute_readonly_sql"], population_id="forged")])


@pytest.mark.parametrize("verified,wrong_scope", [(False, False), (True, True)])
def test_id_without_verified_exact_scope_cannot_complete_population(verified, wrong_scope):
    from app.services.query_scope import missing_population_coverage
    state = population_state()
    p = state.populations[0]
    result = ToolResult(success=True, data=[{"n": 2}], metadata={"population_id": p.population_id,
        "scope_validation": {"verified": verified, "effective_query_scope":
            (state.query_scope if wrong_scope else p.query_scope).model_dump(mode="json")}})
    assert p.population_id in missing_population_coverage(state, [result])


def test_replan_preserves_verified_population_fact_and_scope():
    state = population_state()
    p = state.populations[0]
    state.plan = [PlanStep(step_id="old", goal="count A", selected_tools=["execute_readonly_sql"],
        population_id=p.population_id, query_scope=p.query_scope, status="completed",
        observations=[{"tool": "execute_readonly_sql", "success": True, "scope_verified": True, "tool_call_id": "q",
                       "population_id": p.population_id, "effective_query_scope": p.query_scope.model_dump(mode="json")}],
        evidence_ids=["ev-1"])]
    proposal = state.plan[0].model_copy(update={"step_id": "new"})
    _, steps = prepare_replacement(state, [proposal])
    assert steps[0].status == "completed" and steps[0].evidence_ids == ["ev-1"]
    assert steps[0].population_id == p.population_id and steps[0].query_scope == p.query_scope


def fake_runtime():
    from app.agents.runtime import DecisionRuntime
    from app.agents.scientific_agent import ScientificAgent
    from app.tools.registry import ToolRegistry
    run = DecisionRuntime.__new__(DecisionRuntime)
    run.owner = SimpleNamespace(tool_registry=ToolRegistry(), skills=SimpleNamespace(execution_context=lambda _: ""),
        _record_tool=lambda *args: ScientificAgent._record_tool(None, *args),
        _add_evidence=lambda *args: ScientificAgent._add_evidence(None, *args))
    run._check = lambda _: None
    run._emit = lambda *args, **kwargs: None
    run._await = lambda coroutine, _: asyncio.run(coroutine)
    return run


def test_rejected_plan_fake_decision_schema_observation_enables_text2sql(monkeypatch):
    from app.agents.decision_node import DecisionNode, eligible_call_tools
    run = fake_runtime()
    state = ScientificAgentState(user_id="u", thread_id="t", goal="analyze", allowed_tools=["search_schema", "text_to_sql"], available_tools=["database"])
    state.decision = AgentDecision(action="REPLAN", plan=[PlanStep(step_id="1", goal="SQL", selected_tools=["text_to_sql"], required_capabilities=[Capability.DATABASE])])
    state = ScientificAgentState.model_validate(run.update_plan(run._return(state), {})["agent"])
    assert len(state.plan) == 2 and not state.observations
    schema_step = next(step for step in state.plan if "search_schema" in step.selected_tools)
    sql_step = next(step for step in state.plan if "text_to_sql" in step.selected_tools)
    assert schema_step.step_id in sql_step.depends_on
    assert state.control_observations[-1]["success"] is True
    captured = []
    async def decision(schema, system, payload, **kwargs):
        captured.append(payload)
        assert payload["currently_callable_tools"] == ["search_schema"]
        return AgentDecision(action="CALL_TOOL", tool_name="search_schema",
                             step_id=schema_step.step_id, tool_arguments={"query": "molecules"}), {}
    monkeypatch.setattr("app.agents.decision_node.structured_call", decision)
    run.decider = DecisionNode()
    state = ScientificAgentState.model_validate(run.decision(run._return(state), {})["agent"])
    assert state.decision_valid and state.decision.tool_name == "search_schema"
    async def tool(choice, context):
        return ToolResult(success=True, source="db", data=[{"table": "training_molecules", "columns": SCHEMA["training_molecules"]}])
    run.owner.tool_dispatcher = SimpleNamespace(execute=tool)
    state = ScientificAgentState.model_validate(run.execute_tool(run._return(state), {})["agent"])
    state = ScientificAgentState.model_validate(run.observation(run._return(state), {})["agent"])
    assert "text_to_sql" in eligible_call_tools(state) and not state.evidence
    assert state.schema_cache == SCHEMA and state.consecutive_failures == 0


def test_runtime_sql_step_dispatch_observation_evidence_use_effective_scope():
    state, run = population_state(), fake_runtime()
    p = state.populations[1]
    sql = "SELECT count(*) AS n FROM training_molecules WHERE dataset_version IN ('v_a','v_b') AND structure_type='fused-ring'"
    state.schema_cache = SCHEMA
    state.allowed_tools, state.available_tools = ["execute_readonly_sql"], ["database"]
    state.plan = [PlanStep(step_id="1", goal="all", population_id=p.population_id, query_scope=p.query_scope, selected_tools=["execute_readonly_sql"])]
    state.decision = AgentDecision(action="CALL_TOOL", tool_name="execute_readonly_sql", step_id="1", tool_arguments={"sql": sql})
    state.user_request += "\n" + sql  # explicitly supplied SQL has valid lineage
    async def execute(choice, context):
        from app.services.query_scope import validate_scope
        assert context.population_id == p.population_id and context.query_scope == p.query_scope
        assert context.dataset_version is None
        return ToolResult(success=True, source="db", data=[{"n": 10}], metadata={"sql": sql, "params": {},
            "scope_validation": validate_scope(sql, {}, context.query_scope, SCHEMA)})
    run.owner.tool_dispatcher = SimpleNamespace(execute=execute)
    state = ScientificAgentState.model_validate(run.execute_tool(run._return(state), {})["agent"])
    state = ScientificAgentState.model_validate(run.observation(run._return(state), {})["agent"])
    assert state.tool_calls[-1]["population_id"] == p.population_id
    assert state.evidence[-1].population_id == p.population_id and state.evidence[-1].query_scope == p.query_scope
    assert state.evidence[-1].dataset_version is None


def test_unverified_sql_result_does_not_produce_trusted_evidence():
    state, run = population_state(), fake_runtime()
    state.tool_calls = [{"tool": "execute_readonly_sql", "tool_call_id": "q"}]
    state.observations = [ToolResult(success=True, source="db", data=[{"n": 2}], metadata={"scope_validation": {"verified": False}})]
    result = ScientificAgentState.model_validate(run.observation(run._return(state), {})["agent"])
    assert not result.evidence


@pytest.mark.parametrize("answer", [
    "仅在当前检索结果中未发现 topology，无法确定整个数据库是否不存在该字段。",
    "MAE 从 1 降到 0.7，属于描述性改善，不能说明统计显著性。"])
def test_qualified_limits_and_descriptive_actual_values_are_allowed(answer):
    from app.services.grounded_response import validate_public_claims
    assert validate_public_claims(GroundedResponse(answer=answer), {"schema_retrieval": [{"complete": False}]}) == []


def test_no_evidence_cannot_supply_fictional_claim_id():
    from app.models.schemas import GroundedClaim
    from app.services.grounded_response import validate_public_claims
    response = GroundedResponse(answer="result", claims=[GroundedClaim(text="n=2", evidence_ids=["invented"])])
    assert validate_public_claims(response, {"evidence": []})


def test_fictional_public_evidence_id_is_rejected_even_without_structured_claim():
    from app.services.grounded_response import validate_public_claims
    assert validate_public_claims(GroundedResponse(answer="结果由 ev-999 支持。"), {"evidence": []})


@pytest.mark.parametrize("case", ["D09", "D06", "M02", "D08"])
def test_recorded_state_replay_no_execution_or_history_rewrite(case):
    from app.agents.scientific_agent import ScientificAgent
    from app.agents.runtime import DecisionRuntime
    path = Path(__file__).resolve().parents[1] / f"reports/phase4a_final_blocker_resolution_20261009/ui/blocker-{case}.json"
    before = path.read_bytes()
    record = json.loads(before)
    state = ScientificAgentState.model_validate([e["payload_json"]["state"] for e in record["turns"][0]["events"] if e["event_type"] == "FINAL_ANSWER"][-1])
    identity = state.task_id, state.thread_id, state.conversation_id
    if case in {"D09", "D06"}:
        proposal = AgentDecision.model_validate(next(
            e["payload_json"]["decision"] for e in record["turns"][0]["events"]
            if e["event_type"] == "AGENT_DECISION"
        ))
        state.plan, state.schema_cache = [], {}
        original_evidence = list(state.evidence)
        original_tool_calls = list(state.tool_calls)
        original_observations = list(state.observations)
        # The old contract rejected missing schema; the new contract must
        # produce an authorized, reachable schema prerequisite instead.
        fake_runtime()._apply_plan(state, proposal, {})
        schema_steps = [
            step for step in state.plan
            if {"get_table_schema", "search_schema"} & set(step.selected_tools or step.preferred_tools)
            and not step.depends_on
        ]
        sql_steps = [
            step for step in state.plan
            if "text_to_sql" in (step.selected_tools or step.preferred_tools)
        ]
        assert schema_steps and sql_steps
        # The planner may put schema lookup and SQL in one step, separate
        # independent steps, or depend on the injected prerequisite. All
        # are valid ONLY if a schema tool can run before text_to_sql.
        from app.agents.decision_node import eligible_call_tools
        initial_callable = set(eligible_call_tools(state))
        assert initial_callable & {"get_table_schema", "search_schema"}
        assert "text_to_sql" not in initial_callable
        assert all(set(step.selected_tools or step.preferred_tools) <= set(state.allowed_tools)
                   for step in state.plan)
        assert state.control_observations[-1]["success"] is True
        assert state.evidence == original_evidence
        assert state.tool_calls == original_tool_calls
        assert state.observations == original_observations
        assert not state.schema_cache
    elif case == "M02":
        assert ScientificAgent._evidence_quality_issues(state) == []
        assert {e.source_type for e in state.evidence} == {"file", "database"}
    else:
        assert assess_goal_coverage(state).status == "SATISFIED"
    assert (state.task_id, state.thread_id, state.conversation_id) == identity
    assert path.read_bytes() == before


def test_both_populations_verified_is_satisfied():
    state = population_state()
    for i, p in enumerate(state.populations):
        state.tool_calls.append({"tool": "execute_readonly_sql", "tool_call_id": str(i)})
        state.observations.append(ToolResult(success=True, source="db", data=[{"n": i + 2}], metadata={
            "population_id": p.population_id, "scope_validation": {"verified": True,
            "effective_query_scope": p.query_scope.model_dump(mode="json")}}))
    assert assess_goal_coverage(state).status == "SATISFIED"


def test_all_version_requirement_cannot_be_omitted_or_use_global_default():
    from app.models.schemas import PopulationRequest
    from app.services.resource_grounding import bind_populations, effective_scope
    from app.agents.decision_node import eligible_call_tools
    state = population_state()
    state.populations = []
    state.requires_population_binding = True
    state.schema_cache = SCHEMA
    state.allowed_tools = ["search_schema", "text_to_sql", "execute_readonly_sql"]
    assert eligible_call_tools(state) == ["search_schema"]
    assert assess_goal_coverage(state).missing_populations == ["unbound_population_requirements"]
    with pytest.raises(ValueError, match="unbound"):
        effective_scope(state)
    with pytest.raises(ValueError, match="all.*bound"):
        bind_populations(state, [PopulationRequest(source_text="v_a train split 中 fused-ring 数量",
            query_scope=QueryScope(dataset_version="v_a", filters={"structure_type": "fused-ring"}))])


def test_all_version_extra_narrowing_is_unverified():
    from app.services.query_scope import validate_scope, UnverifiedScope
    with pytest.raises(UnverifiedScope, match="additional version restriction"):
        validate_scope("SELECT count(*) FROM training_molecules WHERE dataset_version IN ('v_a','v_b') AND dataset_version <> 'v_b' AND structure_type='fused-ring'",
            {}, population_state().populations[1].query_scope, SCHEMA)


def test_authorized_all_version_equijoin_is_valid_not_falsely_rejected():
    from app.services.query_scope import validate_scope
    schema = {"dataset_versions": [{"name": "id", "type": "uuid"}, {"name": "version", "type": "text"}],
        "training_memberships": [{"name": k} for k in ("dataset_version_id", "molecule_id", "split")],
        "molecules": [{"name": k} for k in ("molecule_id", "structure_type")]}
    sql = "SELECT count(DISTINCT m.molecule_id) AS n FROM molecules m JOIN training_memberships tm ON m.molecule_id=tm.molecule_id JOIN dataset_versions d ON tm.dataset_version_id=d.id WHERE d.version IN ('v_a','v_b') AND m.structure_type='fused-ring'"
    assert validate_scope(sql, {}, population_state().populations[1].query_scope, schema)["verified"]


def test_all_versions_does_not_drop_explicit_train_split():
    from app.models.schemas import PopulationRequest
    from app.services.resource_grounding import bind_populations
    from app.services.query_scope import validate_scope
    state = population_state()
    state.populations = []
    old_span = "授权范围内全部版本 fused-ring 总数"
    span = "授权范围内全部版本 train split fused-ring 总数"
    state.user_request = state.goal = state.goal.replace(old_span, span)
    compiled = bind_populations(state, [
        PopulationRequest(source_text="v_a train split 中 fused-ring 数量", query_scope=QueryScope(dataset_version="v_a", split="train", filters={"structure_type": "fused-ring"})),
        PopulationRequest(source_text=span, query_scope=QueryScope(all_versions=True, split="train", filters={"structure_type": "fused-ring"}))])
    scope = compiled[1].query_scope
    assert scope.split == "train" and not scope.whole_dataset
    assert validate_scope("SELECT count(*) FROM training_molecules WHERE dataset_version IN ('v_a','v_b') AND split='train' AND structure_type='fused-ring'", {}, scope, SCHEMA)["verified"]


def test_missing_explicit_population_filter_is_rejected():
    from app.services.resource_grounding import bind_populations
    from app.models.schemas import PopulationRequest
    state = population_state()
    with pytest.raises(ValueError, match="categorical literal"):
        bind_populations(state, [PopulationRequest(source_text="v_a train split 中 fused-ring 数量", query_scope=QueryScope())])


@pytest.mark.asyncio
async def test_dispatch_cache_does_not_cross_population_or_scope(monkeypatch):
    from app.tools.dispatcher import ToolDispatcher, ToolExecutionContext
    from app.tools.registry import ToolChoice
    dispatcher = ToolDispatcher.__new__(ToolDispatcher)
    calls = []
    async def execute(choice, context):
        calls.append(context.population_id)
        return ToolResult(success=True, data={"ok": True})
    monkeypatch.setattr(dispatcher, "_execute", execute)
    context = ToolExecutionContext(user_id="u", thread_id="t", resources=resources(), population_id="A", query_scope=QueryScope(split="train"))
    choice = ToolChoice(tool="get_table_schema", arguments={"table": "*"}, reason="")
    await dispatcher.execute(choice, context)
    assert (await dispatcher.execute(choice, context)).metadata["cached_reuse"]
    context.population_id, context.query_scope = "B", QueryScope(all_versions=True)
    assert not (await dispatcher.execute(choice, context)).metadata.get("cached_reuse")
    assert calls == ["A", "B"]


@pytest.mark.parametrize("p", [.01, .8])
def test_actual_significance_test_requires_supporting_result(p):
    from app.models.schemas import GroundedClaim
    from app.services.grounded_response import validate_public_claims
    facts = {"evidence": [{"evidence_id": "test", "value": {"test_name": "paired-test", "p_value": p, "alpha": .05, "sample_count": 20}}]}
    response = GroundedResponse(answer="差异具有统计显著性。", claims=[GroundedClaim(text="significance", evidence_ids=["test"])])
    assert bool(validate_public_claims(response, facts)) == (p >= .05)


@pytest.mark.asyncio
async def test_existing_composer_retries_deterministic_semantic_rejection(monkeypatch):
    from app.services.grounded_response import GroundedResponseService
    outputs = [GroundedResponse(answer="整个数据库不存在 topology 字段。"),
               GroundedResponse(answer="当前检索结果未发现 topology，无法判断全库情况。")]
    payloads = []
    async def compose(schema, system, payload):
        payloads.append(payload)
        return outputs.pop(0), {"latency_ms": 1, "fallback": False}
    monkeypatch.setattr("app.services.grounded_response.structured_call", compose)
    result, usage = await GroundedResponseService().generate("topology", {"schema_retrieval": [{"complete": False}]})
    assert "无法判断" in result.answer and len(payloads) == 2
    assert payloads[1]["response_validation"]["grounding_issues"]
    assert usage["response_validations"][0]["validator"] == "persisted_contract"
