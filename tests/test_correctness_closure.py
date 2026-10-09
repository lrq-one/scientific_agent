"""Correctness invariants checked offline; no semantic classifier/LLM score."""
from types import SimpleNamespace
from pathlib import Path

import pandas as pd
import pytest

from app.agents.runtime import DecisionRuntime
from app.agents.plan_protocol import plan_action_signature, prepare_replacement
from app.agents.goal_coverage import assess_goal_coverage
from app.models.schemas import (AgentDecision, Capability, CompletionCondition, Evidence, FollowUpDecision,
                               PlanStep, QueryScope, ScientificAgentState, TaskRefinementPatch, ToolResult)
from app.services.followup import StateSufficiencyResolver, build_provenance
from app.services.query_scope import UnverifiedScope, validate_scope
from app.tools.file_tools import FileAnalysisService


def runtime():
    value = DecisionRuntime.__new__(DecisionRuntime)
    value.owner = SimpleNamespace()
    value._check = lambda config: None
    value.events = []
    value._emit = lambda config, event, message, **data: value.events.append((event, data))
    return value


def failed_plan():
    return ScientificAgentState(user_id="u", thread_id="thread", task_id="task", conversation_id="conversation",
        goal="read authorized rows", plan_id="original", plan_version=1,
        allowed_tools=["execute_readonly_sql", "search_schema"], available_tools=["database"],
        errors=["unknown column"], consecutive_failures=1,
        plan=[PlanStep(step_id="old", goal="execute", selected_tools=["execute_readonly_sql"],
            required_capabilities=[Capability.DATABASE], query_scope=QueryScope(), status="failed",
            completion_condition=CompletionCondition(kind="EXECUTED_ROWS", required_tools=["execute_readonly_sql"], require_scope_match=True),
            observations=[{"tool": "execute_readonly_sql", "success": False, "tool_call_id": "failed-call"}])],
        tool_calls=[{"tool": "execute_readonly_sql", "tool_call_id": "failed-call"}],
        observations=[ToolResult(success=False, error="unknown column")])


def test_failed_cosmetic_replan_cannot_reset_budget_and_finishes_without_llm():
    state, run = failed_plan(), runtime()
    proposal = state.plan[0].model_copy(deep=True)
    proposal.step_id, proposal.goal = "renamed", "rephrased same action"
    for _ in range(2):
        run._apply_plan(state, AgentDecision(action="REPLAN", plan=[proposal]), {})
    assert state.plan_id == "original" and state.no_progress_replan_count == 2
    assert state.consecutive_failures == 3 and state.replan_count == 0
    assert all(event == "NO_PROGRESS_REPLAN" for event, _ in run.events)
    assert run.events[-1][1]["recoverable"] is False
    assert run.events[-1][1]["attempted_paths"] == ["execute_readonly_sql"]
    bounded = ScientificAgentState.model_validate(run.decision(run._return(state), {})["agent"])
    assert bounded.decision.action == "FINISH" and bounded.blocking_issues
    assert (bounded.task_id, bounded.thread_id, bounded.conversation_id) == ("task", "thread", "conversation")


def test_action_signature_ignores_order_ids_and_prose_but_preserves_dependencies():
    first = PlanStep(step_id="a", goal="discover", selected_tools=["search_schema"])
    second = PlanStep(step_id="b", goal="execute", selected_tools=["execute_readonly_sql"], depends_on=["a"])
    renamed = [second.model_copy(update={"step_id": "y", "goal": "different prose", "depends_on": ["x"]}),
               first.model_copy(update={"step_id": "x"})]
    assert plan_action_signature([first, second]) == plan_action_signature(renamed)
    renamed[0].depends_on = []
    assert plan_action_signature([first, second]) != plan_action_signature(renamed)


def test_recovery_with_new_schema_path_preserves_global_facts_and_resets_no_progress():
    state, run = failed_plan(), runtime()
    state.no_progress_replan_count = 1
    state.evidence = [Evidence(evidence_id="fact", claim="verified earlier rows", value=[{"n": 2}],
        source_type="database", source="db", tool_call_id="prior")]
    run._apply_plan(state, AgentDecision(action="REPLAN", plan=[
        PlanStep(step_id="schema", goal="inspect actual schema", selected_tools=["search_schema"], required_capabilities=[Capability.DATABASE]),
        PlanStep(step_id="retry", goal="correct and execute", selected_tools=["execute_readonly_sql"],
                 required_capabilities=[Capability.DATABASE], depends_on=["schema"])]), {})
    assert state.replan_count == 1 and state.no_progress_replan_count == 0
    assert state.plan[1].status == "blocked" and state.evidence[0].evidence_id == "fact"
    assert state.observations[0].success is False  # failed audit is never erased


def test_replacement_does_not_promote_failed_observations_or_reuse_other_file_outcome():
    state = failed_plan()
    _, prepared = prepare_replacement(state, state.plan)
    assert prepared[0].status == "pending" and not prepared[0].observations
    state.plan = [PlanStep(step_id="one", goal="analyze first.csv", selected_tools=["calculate_metrics"],
        query_scope=QueryScope(), observations=[{"tool": "calculate_metrics", "success": True}])]
    _, prepared = prepare_replacement(state, [PlanStep(step_id="two", goal="analyze second.csv", selected_tools=["calculate_metrics"])])
    assert not prepared[0].observations


def data_state(sql, rows, dimension="structure_type"):
    return ScientificAgentState(user_id="u", thread_id="t", goal="categorical coverage",
        query_scope=QueryScope(grouping=[dimension]),
        tool_calls=[{"tool": "execute_readonly_sql", "tool_call_id": "q"}],
        observations=[ToolResult(success=True, data=rows, metadata={"sql": sql})])


@pytest.mark.parametrize("sql", ["SELECT ring_count AS subgroup,count(*) FROM f GROUP BY ring_count",
                                  "SELECT ring_count AS subgroup,count(*) FROM f GROUP BY 1"])
def test_numeric_grouping_cannot_satisfy_categorical_goal(sql):
    coverage = assess_goal_coverage(data_state(sql, [{"subgroup": 0, "n": 24}]))
    assert coverage.status == "PARTIAL" and coverage.missing_dimensions == ["structure_type"]


@pytest.mark.parametrize("group", ["structure_type", "1", "subgroup"])
def test_real_grouping_projection_and_alias_are_recognized(group):
    sql = "SELECT structure_type AS subgroup,count(*) FROM molecules GROUP BY " + group
    assert assess_goal_coverage(data_state(sql, [{"subgroup": "linear", "n": 8}])).status == "SATISFIED"


def test_empty_and_unexecuted_results_are_unverifiable_not_completed_science():
    assert assess_goal_coverage(data_state("SELECT structure_type FROM m", [])).status == "UNVERIFIABLE"
    state = data_state("", [{"structure_type": "linear"}])
    state.tool_calls[0]["tool"] = "get_table_schema"
    assert assess_goal_coverage(state).status == "UNVERIFIABLE"


def test_output_label_resolves_only_explicit_original_schema_dimension():
    state = data_state("SELECT split,count(*) AS structure_count FROM m GROUP BY split",
                       [{"split": "train", "structure_count": 61}], dimension="split")
    state.query_scope.grouping = []
    state.user_request = "count structures grouped by split"
    state.schema_cache = {"m": [{"name": "split"}, {"name": "structure_type"}]}
    state.requested_dimensions = ["structure_count_by_split"]
    coverage = assess_goal_coverage(state)
    assert coverage.required_dimensions == ["split"] and coverage.status == "SATISFIED"
    # The executed columns must not determine what the user required.
    state.observations[0].data = [{"structure_count": 150}]
    state.observations[0].metadata["sql"] = "SELECT count(*) AS structure_count FROM m"
    assert assess_goal_coverage(state).missing_dimensions == ["split"]


def test_unresolved_or_ambiguous_dimension_description_is_not_silently_dropped():
    state = data_state("SELECT ring_count,count(*) FROM m GROUP BY ring_count", [{"ring_count": 2}])
    state.query_scope.grouping = []
    state.requested_dimensions = ["requested_unknown_group"]
    assert assess_goal_coverage(state).status == "PARTIAL"
    state.schema_cache = {"m": [{"name": "split"}, {"name": "structure_type"}]}
    state.user_request = "group by split and structure_type"
    state.requested_dimensions = ["counts_by_split_and_structure_type"]
    assert assess_goal_coverage(state).missing_dimensions == ["counts_by_split_and_structure_type", "structure_type"]


def test_partial_mixed_result_and_missing_export_are_reported():
    state = data_state("", [{"structure_type": "linear"}])
    state.tool_calls[0]["tool"] = "read_csv"
    state.plan = [PlanStep(step_id="db", goal="database coverage", selected_tools=["execute_readonly_sql"], required_capabilities=[Capability.DATABASE]),
                  PlanStep(step_id="export", goal="export", selected_tools=["save_result_table"])]
    coverage = assess_goal_coverage(state)
    assert coverage.status == "PARTIAL"
    assert set(coverage.missing_deliverables) == {"artifact", "executed_database_analysis"}


def context(rows=None, complete=False):
    return {"task": {"id": "task", "intent_json": {"query_scope": {"split": "train"}, "datasource_id": "db"}},
        "events": [{"event_type": "TOOL_FINISHED", "payload_json": {"tool": "execute_readonly_sql",
            "result": {"success": True, "source": "db", "data": [] if rows is None else rows,
                       "metadata": {"sql": "SELECT structure_type FROM molecules", "params": {},
                                    "rows_complete": complete}}}}],
        "evidence": [], "assistant_message": {"content": "saved answer"}}


def test_subgroup_missing_from_preview_is_insufficient_and_never_inferred_zero():
    decision = FollowUpDecision(interaction_type="EVIDENCE_QUERY", requested_content=["raw_rows"],
        refinement_patch=TaskRefinementPatch(subgroup="fused_ring"))
    result = StateSufficiencyResolver().resolve(decision, context([{"structure_type": "linear"}]))
    assert result.action == "INSUFFICIENT" and not result.requires_execution
    assert result.missing_content == ["raw_rows"] and result.scope_issues


def test_present_subgroup_reuses_rows_and_explicit_refinement_executes():
    decision = FollowUpDecision(interaction_type="EVIDENCE_QUERY", requested_content=["raw_rows"],
        refinement_patch=TaskRefinementPatch(subgroup="fused_ring"))
    resolver = StateSufficiencyResolver()
    assert resolver.resolve(decision, context([{"structure_type": "fused_ring", "n": 2}])).action == "REUSE"
    decision.interaction_type = "TASK_REFINEMENT"
    assert resolver.resolve(decision, context()).requires_execution


@pytest.mark.parametrize("patch", [TaskRefinementPatch(split="test"), TaskRefinementPatch(datasource_id="other"),
                                   TaskRefinementPatch(filters={"structure_type": "linear"})])
def test_wrong_scope_cannot_silently_reuse_existing_answer(patch):
    decision = FollowUpDecision(interaction_type="RESULT_EXPLANATION", requested_content=["answer"], refinement_patch=patch)
    assert StateSufficiencyResolver().resolve(decision, context()).action == "INSUFFICIENT"
    decision.interaction_type = "TASK_REFINEMENT"
    assert StateSufficiencyResolver().resolve(decision, context()).requires_execution


def test_full_rows_request_distinguishes_preview_from_complete_empty_result():
    decision = FollowUpDecision(interaction_type="PROVENANCE_QUERY", requested_content=["raw_rows"],
        refinement_patch=TaskRefinementPatch(full_rows=True))
    resolver = StateSufficiencyResolver()
    assert resolver.resolve(decision, context([{"id": 1}], complete=False)).action == "INSUFFICIENT"
    assert resolver.resolve(decision, context([], complete=True)).action == "REUSE"


def test_executed_sql_does_not_borrow_params_from_unrelated_candidate():
    saved = context([{"id": 1}])
    del saved["events"][0]["payload_json"]["result"]["metadata"]["params"]
    saved["events"].insert(0, {"event_type": "TOOL_FINISHED", "payload_json": {"tool": "text_to_sql",
        "result": {"success": True, "data": {"sql": "SELECT * FROM other", "params": {"version": "old"}}}}})
    provenance = build_provenance(saved)
    assert not provenance.params_recorded and provenance.sql_params == {}


def test_unsupported_filter_operator_is_not_empty_result(monkeypatch):
    service = FileAnalysisService()
    monkeypatch.setattr(service, "read_table", lambda path: pd.DataFrame({"x": [1, 2]}))
    invalid = service.filter_samples(Path("file.csv"), {"x": {"gt": 1}})
    assert not invalid.success and invalid.outcome == "UNSUPPORTED_OPERATION" and invalid.data is None
    empty = service.filter_samples(Path("file.csv"), {"x": 3})
    assert empty.success and empty.outcome == "EMPTY_RESULT"


def test_metrics_require_only_metric_columns(monkeypatch):
    service = FileAnalysisService()
    monkeypatch.setattr(service, "read_table", lambda path: pd.DataFrame({"observed_rt": [1.0, 3.0], "predicted_rt": [2.0, 3.0]}))
    result = service.calculate_metrics(Path("file.csv"))
    assert result.success and result.data["mae"] == 0.5


def test_refusal_is_terminal_without_grounded_response_or_empirical_claim():
    run = runtime()
    state = failed_plan()
    state.decision = AgentDecision(action="REFUSE")
    final = ScientificAgentState.model_validate(run.refuse(run._return(state), {})["agent"])
    assert final.runtime_status == "refused" and final.quality_status == "REFUSED"
    assert final.claims == [] and run.events[-1][1]["llm_telemetry"] == {"llm_called": False, "fallback": False}


def test_empty_outer_exception_keeps_type_correlation_and_redacts_key():
    from app.api.routes import execution_failure
    failure = execution_failure(TimeoutError(), "resume", task_id="task", thread_id="thread")
    assert failure["error"] == "TimeoutError" and failure["failure_code"] == "EXECUTION_TIMEOUT"
    assert failure["task_id"] == "task" and failure["thread_id"] == "thread"
    secret = "sk-" + "a" * 60
    assert secret not in execution_failure(ValueError(secret), "tool")["reason_summary"]


SCHEMA = {"dataset_versions": [{"name": "id", "type": "uuid"}, {"name": "version", "type": "text"}],
          "training_memberships": [{"name": "molecule_id", "type": "text"}, {"name": "dataset_version_id", "type": "uuid"}, {"name": "split", "type": "text"}],
          "molecules": [{"name": "molecule_id", "type": "text"}, {"name": "structure_type", "type": "text"}]}


@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM molecules m CROSS JOIN dataset_versions v WHERE v.version='v1'",
    "SELECT count(*) FROM molecules m JOIN dataset_versions v ON 1=1 WHERE v.version='v1'",
    "SELECT count(*) FROM molecules m LEFT JOIN dataset_versions v ON m.molecule_id=v.id WHERE v.version='v1'",
    "SELECT count(*) FROM training_memberships t JOIN dataset_versions v ON v.id=t.dataset_version_id WHERE NOT(v.version='v1')",
])
def test_unsupported_population_proofs_report_unverified_scope(sql):
    with pytest.raises(UnverifiedScope, match="UNVERIFIED_SCOPE"):
        validate_scope(sql, {}, QueryScope(dataset_version="v1"), SCHEMA)


def test_real_join_scope_remains_supported_and_cte_train_is_not_whole():
    sql = "SELECT count(*) FROM training_memberships t JOIN dataset_versions v ON v.id=t.dataset_version_id JOIN molecules m ON m.molecule_id=t.molecule_id WHERE v.version='v1'"
    assert validate_scope(sql, {}, QueryScope(dataset_version="v1"), SCHEMA)["verified"]
    subset = "WITH selected AS (SELECT t.molecule_id FROM training_memberships t JOIN dataset_versions v ON v.id=t.dataset_version_id WHERE v.version='v1' AND t.split='train') SELECT count(*) FROM molecules m JOIN selected s ON s.molecule_id=m.molecule_id"
    with pytest.raises(ValueError, match="whole dataset"):
        validate_scope(subset, {}, QueryScope(dataset_version="v1", whole_dataset=True), SCHEMA)


def test_model_version_label_cannot_prove_dataset_version():
    schema = {**SCHEMA, "model_versions": [{"name": "version", "type": "text"}]}
    with pytest.raises(ValueError, match="dataset version"):
        validate_scope("SELECT version FROM model_versions WHERE version='v1'", {}, QueryScope(dataset_version="v1"), schema)


def test_metadata_only_query_cannot_prove_whole_population():
    with pytest.raises(ValueError, match="whole dataset"):
        validate_scope("SELECT id FROM dataset_versions WHERE version='v1'", {}, QueryScope(dataset_version="v1", whole_dataset=True), SCHEMA)


def test_typed_split_refinement_changes_population_without_turning_it_into_comparison():
    from app.models.schemas import ResourceSummary
    from app.agents.request_router import RequestRouter
    from app.tools.registry import ToolRegistry
    from app.services.followup import workflow_query
    saved = {"task": {"intent_json": {"goal": "count train split rows", "query_scope": {"split": "train"}}}}
    goal, _ = workflow_query("change to test split", "REFINE_PREVIOUS_TASK", saved, TaskRefinementPatch(split="test"))
    assert "train split" not in goal
    state = ScientificAgentState(user_id="u", thread_id="t", goal=goal, grounding_ready=True,
        resource_summary=ResourceSummary(authorized_datasources=["db"]),
        conversation_context={"current_user_query": "change to test split", "previous_query_scope": {"split": "train"},
            "follow_up_decision": {"interaction_type": "TASK_REFINEMENT", "refinement_patch": {"split": "test"}}})
    run = runtime()
    run.owner = SimpleNamespace(router=RequestRouter(), tool_registry=ToolRegistry())
    run._ground_resources(state)
    assert state.query_scope.split == "test" and state.query_scope.comparison_target == []
