from types import SimpleNamespace
import pytest
from app.models.schemas import (ResourceSummary, QueryScope, ScientificAgentState, PlanStep,
                                AgentDecision, Capability, CompletionCondition)
from app.services.resource_grounding import resolve_binding, resolve_scope, ask_sufficiency
from app.services.query_scope import validate_scope
from app.agents.plan_protocol import prepare_replacement, refresh_steps, satisfied
from app.agents.runtime import DecisionRuntime
from app.agents.planning_policy import PlanningPolicy
from app.services.followup import workflow_query

V1, V2 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"

def test_schema_alternatives_are_not_all_required_for_sql_candidate():
    from app.agents.plan_protocol import condition_for
    step = PlanStep(step_id="one", goal="Prepare grounded query", selected_tools=[
        "search_schema", "get_table_schema", "text_to_sql"], required_capabilities=[Capability.DATABASE])
    step.completion_condition = condition_for(step)
    assert step.completion_condition.required_tools == ["text_to_sql"]
    step.observations = [{"tool": "search_schema", "success": True}]
    assert not satisfied(step)
    step.observations.append({"tool": "text_to_sql", "success": True})
    assert satisfied(step)
SCHEMA = {"dataset_versions": [{"name": "id", "type": "uuid"}, {"name": "version", "type": "text"}],
          "training_memberships": [{"name": "dataset_version_id", "type": "uuid"}, {"name": "split", "type": "text"}],
          "training_molecules": [{"name": "dataset_version", "type": "text"}]}

def resources(one=False):
    return ResourceSummary(authorized_datasources=["authorized"], available_files=["first.csv", "second.csv"],
        resource_metadata={"dataset_versions": [
            {"id": V1, "version": "release-october", "dataset_id": "dataset", "datasource_id": "authorized"},
            *([] if one else [{"id": V2, "version": "evaluation-B", "dataset_id": "dataset", "datasource_id": "authorized"}])],
            "model_runs": [{"id": "run-id", "run_name": "prediction-nightly", "experiment_id": "experiment", "datasource_id": "authorized"}],
            "experiments": [{"id": "experiment", "dataset_version_id": V1}],
            "files": [{"filename": "first.csv", "columns": ["observed_rt", "predicted_rt"]}]})

@pytest.mark.parametrize("query,version,kind", [
    ("统计 release-october", "release-october", "LABEL"),
    ("统计 evaluation-B", "evaluation-B", "LABEL"),
    ("统计 " + V1, "release-october", "UUID"),
    ("比较 prediction-nightly", "release-october", "LABEL")])
def test_metadata_identities_without_version_naming_patterns(query, version, kind):
    b = resolve_binding(query, resources())
    assert b.dataset_version == version and b.version_identifier_type == kind
    if "nightly" in query: assert b.run_ids == ["run-id"]

def test_previous_unique_and_ambiguous_metadata():
    assert resolve_binding("再看覆盖", resources(), {"dataset_version": "evaluation-B"}).dataset_version == "evaluation-B"
    assert resolve_binding("看覆盖", resources(one=True)).dataset_version == "release-october"
    assert resolve_binding("看覆盖", resources()).dataset_version is None
    assert resolve_binding("看覆盖", resources(), explicit_version="not-authorized").dataset_version is None
    assert resolve_binding("看覆盖", resources()).ambiguous_fields == ["dataset_version"]

def test_router_hint_calls_identity_matching_without_file_variable_shadowing():
    from app.agents.request_router import RequestRouter
    hint = RequestRouter().context_hint("evaluation-B first.csv", resources())
    assert hint["dataset_versions"] == ["evaluation-B"] and hint["mentioned_files"] == ["first.csv"]

@pytest.mark.parametrize("fields,query,needed", [
    (["dataset_version"], "release-october", False), (["dataset_version"], "覆盖", True),
    (["run_id"], "prediction-nightly", False), (["files"], "比较文件", True),
    (["files"], "first.csv", False), (["prediction_column"], "first.csv", False),
    (["dataset_version", "molecule_id"], "release-october", True),
    (["split"], "release-october train split", False)])
def test_pre_ask_is_fact_based_not_default_selection(fields, query, needed):
    r = resources(); b = resolve_binding(query, r)
    state = ScientificAgentState(user_id="u", thread_id="t", goal=query, resource_summary=r,
        resource_binding=b, query_scope=resolve_scope(query, b))
    result = ask_sufficiency(AgentDecision(action="ASK_USER", missing_information=fields), state)
    assert result["necessary"] is needed

def plan_state():
    return ScientificAgentState(user_id="u", thread_id="t", goal="read and count", allowed_tools=["search_schema", "execute_readonly_sql"], available_tools=["database"],
        plan=[PlanStep(step_id="discover", goal="inspect schema", selected_tools=["search_schema"],
            required_capabilities=[Capability.DATABASE], status="running", plan_id="old",
            query_scope=QueryScope(), completion_condition=CompletionCondition(kind="SCHEMA"),
            observations=[{"tool": "search_schema", "success": True, "tool_call_id": "call", "plan_id": "old"}])], plan_id="old", plan_version=1)

def test_observation_survives_step_identity_change_and_replacement_does_not_mutate():
    state = plan_state(); proposal = state.plan[0].model_copy(deep=True); proposal.step_id = "new-schema"
    _, prepared = prepare_replacement(state, [proposal])
    assert prepared[0].observations == state.plan[0].observations
    assert prepared[0].status == "completed" and state.plan[0].status == "running"

def test_partial_tool_success_is_not_goal_completion():
    step = PlanStep(step_id="analysis", goal="query coverage", selected_tools=["search_schema", "execute_readonly_sql"],
        completion_condition=CompletionCondition(kind="EXECUTED_ROWS", required_tools=["execute_readonly_sql"], require_scope_match=True),
        observations=[{"tool": "search_schema", "success": True}])
    assert not satisfied(step)
    step.observations.append({"tool": "execute_readonly_sql", "success": True, "scope_verified": False})
    assert not satisfied(step)
    step.observations[-1]["scope_verified"] = True
    assert satisfied(step)

def test_structured_completion_enum_excludes_partial_schema_success(monkeypatch):
    import asyncio
    import app.agents.decision_node as node
    captured = {}
    class FakeLLM:
        def with_structured_output(self, schema, **kwargs):
            captured.update(schema)
            return self
        async def ainvoke(self, *args, **kwargs):
            return AgentDecision(action="FINISH")
    monkeypatch.setattr(node, "configured_llm", lambda: FakeLLM())
    step = PlanStep(step_id="analysis", goal="query", selected_tools=["execute_readonly_sql"],
        completion_condition=CompletionCondition(kind="EXECUTED_ROWS", required_tools=["execute_readonly_sql"], require_scope_match=True),
        observations=[{"tool":"search_schema", "success":True}])
    asyncio.run(node.structured_call(AgentDecision, "unit test", {}, tool_names=["execute_readonly_sql"], plan=[step]))
    assert captured['properties']['completed_step_ids']['maxItems'] == 0
    assert captured['$defs']['PlanStep']['properties']['step_id']['enum'] == ['1','2','3','4','5','6']
    assert captured['$defs']['PlanStep']['properties']['depends_on']['items']['enum'] == ['1','2','3','4','5','6']
    step.observations.append({"tool":"execute_readonly_sql", "success":True, "scope_verified":True})
    asyncio.run(node.structured_call(AgentDecision, "unit test", {}, tool_names=["execute_readonly_sql"], plan=[step]))
    assert captured['properties']['completed_step_ids']['items']['enum'] == ['analysis']

def test_partial_population_masks_only_illegal_completion_not_tool_choice(monkeypatch):
    import asyncio
    import app.agents.decision_node as node
    from app.models.schemas import ToolResult
    captured={}
    async def fake(schema,system,payload,**kwargs):
        captured.update(kwargs)
        captured['payload']=payload
        return AgentDecision(action='REPLAN'), {}
    monkeypatch.setattr(node,'structured_call',fake)
    state=ScientificAgentState(user_id='u',thread_id='t',goal='compare populations',grounding_ready=True,query_scope=QueryScope(whole_dataset=True,comparison_target=['train']),
        tool_calls=[{'tool':'execute_readonly_sql','tool_call_id':'tool-1'}], observations=[ToolResult(success=True,source='db',data=[{'count':2}],metadata={
            'scope_validation':{'scope_coverage':{'whole_dataset':False,'splits':['train']}}})])
    asyncio.run(node.DecisionNode().decide(state,[],''))
    assert captured['payload']['pending_executed_scope']==['whole_dataset']
    assert set(captured['action_names'])=={'REPLAN','ASK_USER','REFUSE'}
    assert captured['call_tool_names']==[]

def test_sql_callability_requires_actual_schema_not_metadata_or_success_label():
    from app.agents.decision_node import eligible_call_tools
    state=ScientificAgentState(user_id='u',thread_id='t',goal='analyze authorized data',allowed_tools=['search_schema','get_table_schema','text_to_sql'])
    assert eligible_call_tools(state)==['get_table_schema','search_schema']
    state.schema_cache={'molecules':[{'name':'id','type':'uuid'}]}
    assert 'text_to_sql' in eligible_call_tools(state)

def test_schema_prerequisite_does_not_install_fixed_plan_or_expand_authorization():
    from app.agents.decision_node import eligible_call_tools
    state=ScientificAgentState(user_id='u',thread_id='t',goal='analyze authorized data',allowed_tools=['get_table_schema','text_to_sql'],
        plan=[PlanStep(step_id='1',goal='prepare query',selected_tools=['text_to_sql'])])
    assert eligible_call_tools(state)==[]
    assert state.plan[0].selected_tools==['text_to_sql']
    state.schema_cache={'molecules':[{'name':'id','type':'uuid'}]}
    assert eligible_call_tools(state)==['text_to_sql']

def test_registry_metadata_owns_capabilities_but_does_not_choose_tools(monkeypatch):
    import asyncio
    import app.agents.decision_node as node
    captured={}
    class FakeLLM:
        def with_structured_output(self,schema,**kwargs): captured.update(schema); return self
        async def ainvoke(self,*args,**kwargs):
            return {'action':'REPLAN','plan':[{'step_id':'1','goal':'compare and query','selected_tools':['compare_models','search_schema']}]}
    monkeypatch.setattr(node,'configured_llm',lambda:FakeLLM())
    result,_=asyncio.run(node.structured_call(AgentDecision,'unit',{},tool_names=['compare_models','search_schema'],
        tool_capabilities={'compare_models':'file','search_schema':'database'}))
    assert 'required_capabilities' not in captured['$defs']['PlanStep']['properties']
    assert result.plan[0].selected_tools==['compare_models','search_schema']
    assert set(result.plan[0].required_capabilities)=={Capability.FILE,Capability.DATABASE}
    assert PlanningPolicy.validate_plan(result.plan,{'compare_models','search_schema'},{'file','database'},
        {'compare_models':'file','search_schema':'database'})
    with pytest.raises(ValueError):
        PlanningPolicy.validate_plan(result.plan,{'compare_models','search_schema'},{'database'},
            {'compare_models':'file','search_schema':'database'})

def test_dependency_lifecycle_and_completion_are_runtime_owned():
    state = plan_state(); state.plan.append(PlanStep(step_id="execute", goal="execute", selected_tools=["execute_readonly_sql"], depends_on=["discover"]))
    refresh_steps(state)
    assert [s.status for s in state.plan] == ["completed", "pending"]
    state.plan[0].observations = []; state.plan[0].status = "pending"
    refresh_steps(state)
    assert state.plan[1].status == "blocked"

def test_output_step_ids_exclude_blocked_dependencies_even_for_shared_tools():
    from app.agents.decision_node import eligible_plan_steps
    state=plan_state()
    state.plan[0].observations=[]
    state.plan.append(PlanStep(step_id='2',goal='later schema',depends_on=['discover'],selected_tools=['search_schema']))
    refresh_steps(state)
    assert [s.step_id for s in eligible_plan_steps(state)]==['discover']
    state.plan[0].observations=[{'tool':'search_schema','success':True}]
    refresh_steps(state)
    assert [s.step_id for s in eligible_plan_steps(state)]==['2']

@pytest.mark.parametrize("bad", ["missing", "dangling", "cycle", "capability"])
def test_invalid_plan_is_transactional_and_preserves_old_facts(bad):
    state = plan_state(); proposal = state.plan[0].model_copy(deep=True)
    proposal.depends_on = ["missing"] if bad in {"missing", "dangling"} else [proposal.step_id] if bad == "cycle" else []
    if bad == "capability": proposal.required_capabilities = [Capability.FILE]
    original = state.model_dump_json(); runtime = DecisionRuntime.__new__(DecisionRuntime)
    runtime.owner = SimpleNamespace(tool_registry=SimpleNamespace(specs={"search_schema": SimpleNamespace(required_capability="database")}))
    with pytest.raises(ValueError): runtime._apply_plan(state, AgentDecision(action="REPLAN", plan=[proposal]), {})
    assert state.model_dump_json() == original

def test_identical_plan_preserves_facts_but_reports_no_progress():
    state = plan_state(); state.consecutive_failures = 2
    runtime = DecisionRuntime.__new__(DecisionRuntime); runtime.owner = SimpleNamespace()
    emitted = []; runtime._emit = lambda *a, **kw: emitted.append(a[1])
    runtime._apply_plan(state, AgentDecision(action="REPLAN", plan=state.plan), {})
    assert state.plan_id == "old" and state.plan_version == 1 and state.replan_count == 0
    assert state.plan[0].observations and state.consecutive_failures == 3 and emitted == ["NO_PROGRESS_REPLAN"]
    assert state.no_progress_replan_count == 1 and state.control_observations[-1]["success"] is False

@pytest.mark.parametrize("split", ["train", "validation", "test"])
def test_sql_keeps_split_label_and_join_scope(split):
    q = QueryScope(dataset_version="release-october", dataset_version_id=V1, split=split)
    sql = "SELECT COUNT(*) FROM training_memberships tm JOIN dataset_versions dv ON dv.id=tm.dataset_version_id WHERE dv.version=%(version)s AND tm.split=%(split)s"
    assert validate_scope(sql, {"version": "release-october", "split": split}, q, SCHEMA)["verified"]
    with pytest.raises(ValueError, match="split"):
        validate_scope(sql.split(" AND")[0], {"version": "release-october"}, q, SCHEMA)

def test_uuid_binding_and_wrong_value_domain():
    q = QueryScope(dataset_version="release-october", dataset_version_id=V1)
    sql = "SELECT COUNT(*) FROM training_memberships tm WHERE tm.dataset_version_id=%(id)s"
    assert validate_scope(sql, {"id": V1}, q, SCHEMA)["verified"]
    with pytest.raises(ValueError, match="UUID"):
        validate_scope(sql, {"id": "release-october"}, q, SCHEMA)
    with pytest.raises(ValueError, match="version"):
        validate_scope(sql, {"id": V2}, q, SCHEMA)

def test_authorized_run_lineage_can_prove_version_without_redundant_join():
    binding = resolve_binding('compare prediction-nightly', resources())
    scope = resolve_scope('compare prediction-nightly', binding)
    schema = {'model_runs':[{'name':'id','type':'uuid'}, {'name':'run_name','type':'text'}]}
    assert validate_scope("SELECT * FROM model_runs WHERE run_name IN ('prediction-nightly')", {}, scope, schema)['verified']
    with pytest.raises(ValueError):
        validate_scope("SELECT * FROM model_runs WHERE run_name IN ('unrelated')", {}, scope, schema)
    scope.filters.clear()
    with pytest.raises(ValueError):
        validate_scope("SELECT * FROM model_runs WHERE run_name IN ('prediction-nightly')", {}, scope, schema)

def test_refinement_does_not_reuse_a_prior_versions_run_identity_proof():
    original = resolve_scope('prediction-nightly', resolve_binding('prediction-nightly', resources()))
    changed = resolve_scope('evaluation-B', resolve_binding('evaluation-B', resources()), original.model_dump())
    assert changed.dataset_version == 'evaluation-B'
    assert 'version_bound_run_ids' not in changed.filters
    conflicting = resolve_scope('prediction-nightly evaluation-B', resolve_binding('prediction-nightly evaluation-B', resources()))
    assert 'version_bound_run_ids' not in conflicting.filters

def test_scope_cannot_be_satisfied_by_unused_cte_or_bypassing_or():
    q = QueryScope(dataset_version="release-october", split="train")
    for sql in ["WITH unused AS (SELECT * FROM training_memberships WHERE split='train') SELECT * FROM training_molecules WHERE dataset_version='release-october'",
                "SELECT * FROM training_memberships tm JOIN dataset_versions dv ON dv.id=tm.dataset_version_id WHERE dv.version='release-october' AND (tm.split='train' OR 1=1)"]:
        with pytest.raises(ValueError): validate_scope(sql, {}, q, SCHEMA)

def test_version_in_train_scalar_count_cannot_prove_unbounded_total_count():
    q=QueryScope(dataset_version='release-october',whole_dataset=True,comparison_target=['train'])
    sql="""WITH selected_version AS (SELECT id FROM dataset_versions WHERE version='release-october'),
    train_count AS (SELECT COUNT(*) AS n FROM training_molecules tm JOIN selected_version v ON tm.dataset_version_id=v.id WHERE tm.split='train'),
    whole_count AS (SELECT COUNT(*) AS n FROM training_molecules tm)
    SELECT (SELECT n FROM train_count), (SELECT n FROM whole_count)"""
    with pytest.raises(ValueError,match='population branch'):
        validate_scope(sql,{},q,SCHEMA)
    scoped=sql.replace('whole_count AS (SELECT COUNT(*) AS n FROM training_molecules tm)',
        'whole_count AS (SELECT COUNT(*) AS n FROM training_molecules tm JOIN selected_version v ON tm.dataset_version_id=v.id)')
    assert validate_scope(scoped,{},q,SCHEMA)['verified']

def test_version_in_one_union_branch_cannot_prove_other_population():
    q=QueryScope(dataset_version='release-october',whole_dataset=True)
    sql="SELECT COUNT(*) FROM training_molecules tm JOIN dataset_versions dv ON dv.id=tm.dataset_version_id WHERE dv.version='release-october' UNION ALL SELECT COUNT(*) FROM training_molecules"
    with pytest.raises(ValueError,match='population branch'):
        validate_scope(sql,{},q,SCHEMA)

def test_feature_population_without_split_column_cannot_borrow_sibling_version():
    q=QueryScope(dataset_version='release-october',whole_dataset=True,comparison_target=['train'])
    schema={**SCHEMA,'molecular_features':[{'name':'molecule_id','type':'uuid'},{'name':'is_fused_ring','type':'boolean'}]}
    sql="""WITH train_count AS (SELECT COUNT(*) AS n FROM training_memberships tm JOIN dataset_versions dv ON dv.id=tm.dataset_version_id WHERE dv.version='release-october' AND tm.split='train'),
    total_count AS (SELECT COUNT(DISTINCT molecule_id) AS n FROM molecular_features WHERE is_fused_ring=true)
    SELECT (SELECT n FROM train_count),(SELECT n FROM total_count)"""
    with pytest.raises(ValueError,match='population branch'):
        validate_scope(sql,{},q,schema)

def test_whole_population_and_conditional_split_counts():
    q = QueryScope(dataset_version="release-october", whole_dataset=True)
    sql = "SELECT COUNT(*) FROM training_memberships tm JOIN dataset_versions dv ON dv.id=tm.dataset_version_id WHERE dv.version='release-october'"
    assert validate_scope(sql, {}, q, SCHEMA)["verified"]
    with pytest.raises(ValueError, match="whole"):
        validate_scope(sql + " AND tm.split='train'", {}, q, SCHEMA)
    assert validate_scope(sql + " UNION ALL " + sql + " AND tm.split='train'", {}, q, SCHEMA)["verified"]

def test_whole_train_comparison_allows_verified_local_parts_but_requires_both():
    from app.services.query_scope import missing_scope_coverage
    from app.models.schemas import ToolResult
    scope = QueryScope(dataset_version='release-october', whole_dataset=True, comparison_target=['train'])
    base = "SELECT COUNT(*) FROM training_memberships tm JOIN dataset_versions dv ON dv.id=tm.dataset_version_id WHERE dv.version='release-october'"
    partial = validate_scope(base+" AND tm.split='train'", {}, scope, SCHEMA)
    assert partial['partial_scope'] and not partial['scope_coverage']['whole_dataset']
    assert partial['effective_query_scope']['split']=='train' and not partial['effective_query_scope']['whole_dataset']
    train = ToolResult(success=True, source='db',data=[{'count':2}],metadata={'scope_validation':partial})
    assert missing_scope_coverage(scope,[train]) == ['whole_dataset']
    whole = ToolResult(success=True,source='db',data=[{'count':30}],metadata={'scope_validation':validate_scope(base,{},scope,SCHEMA)})
    assert missing_scope_coverage(scope,[whole]) == ['train']
    assert missing_scope_coverage(scope,[train,whole]) == []
    with pytest.raises(ValueError): validate_scope(base+" AND tm.split='test'",{},scope,SCHEMA)

def test_verified_old_dependency_is_retained_as_a_real_node_not_dangling():
    from app.agents.plan_protocol import retain_verified_prerequisites
    state = plan_state(); refresh_steps(state)
    proposal = PlanStep(step_id="new", goal="query", depends_on=["discover"], selected_tools=["execute_readonly_sql"])
    closed = retain_verified_prerequisites(state, [proposal])
    assert {s.step_id for s in closed} == {"new", "discover"}
    assert PlanningPolicy.validate_plan(closed, set(state.allowed_tools), {"database"})
    assert state.plan[0].observations

def test_version_refinement_preserves_logical_goal_and_split():
    context = {"task": {"intent_json": {"goal": "count release-october train split by structure_type", "query_scope": {"dataset_version": "release-october", "split": "train"}}}}
    from app.models.schemas import TaskRefinementPatch
    goal, version = workflow_query("换 evaluation-B", "REFINE_PREVIOUS_TASK", context, TaskRefinementPatch(dataset_version="evaluation-B"))
    assert "release-october" not in goal and "train split" in goal and version == "evaluation-B"
