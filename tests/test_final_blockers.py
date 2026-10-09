"""Trace-derived closure regression; never calls a real model."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.goal_coverage import assess_goal_coverage
from app.models.schemas import QueryScope, SQLCandidate, ScientificAgentState, GroundedResponse
from app.services.query_scope import UnverifiedScope, validate_scope
from app.services.text2sql import TextToSQLService, SQLScopeValidationError
from app.services.grounded_response import GroundedResponseService, ResponseGroundingCheck, ProcessOnlyResponse
from app.tools.dispatcher import ToolDispatcher, ToolExecutionContext
from app.tools.registry import ToolChoice
from app.models.schemas import ResourceSummary
from app.agents.runtime import DecisionRuntime
from app.models.schemas import AgentDecision
from app.agents.decision_node import eligible_call_tools

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = {"predictions": [{"name": "model_run_id"}, {"name": "molecule_id"}],
          "model_runs": [{"name": "id"}, {"name": "run_name"}],
          "training_memberships": [{"name": "dataset_version_id"}, {"name": "split"}, {"name": "molecule_id"}]}


def bound_scope():
    return QueryScope(dataset_version="v1", filters={"version_bound_run_ids": ["run1"],
                                                    "version_bound_run_labels": ["label1"]})


@pytest.mark.parametrize("predicate", ["p.model_run_id='run1'", "'run1'=p.model_run_id",
                                       "p.model_run_id IN ('run1')", "r.run_name='label1'"])
def test_real_bound_run_predicates_and_reverse_equality_are_supported(predicate):
    sql = "SELECT count(*) FROM predictions p JOIN model_runs r ON p.model_run_id=r.id WHERE " + predicate
    assert validate_scope(sql, {}, bound_scope(), SCHEMA)["verified"]


def test_bound_run_anchor_cannot_be_assigned_to_disconnected_prediction_population():
    sql = "SELECT count(*) FROM predictions p JOIN model_runs r ON r.id=r.id WHERE r.run_name='label1'"
    with pytest.raises(UnverifiedScope, match="not connected"):
        validate_scope(sql, {}, bound_scope(), SCHEMA)


def test_arbitrary_model_run_id_named_column_is_not_a_trusted_run_binding():
    schema = {**SCHEMA, "other": [{"name": "model_run_id"}, {"name": "molecule_id"}]}
    with pytest.raises(ValueError, match="dataset version"):
        validate_scope("SELECT count(*) FROM other WHERE model_run_id='run1'", {}, bound_scope(), schema)


class CandidateModel:
    def __init__(self, sql):
        self.candidate = SQLCandidate(sql=sql, params={"version": "v1"})
    def with_structured_output(self, schema):
        return self
    async def ainvoke(self, prompt, config=None):
        return self.candidate


@pytest.mark.asyncio
async def test_rejected_candidate_preserves_actual_sql_params_and_no_fallback():
    model = CandidateModel("SELECT count(*) FROM training_memberships")
    with pytest.raises(SQLScopeValidationError) as saved:
        await TextToSQLService(model).generate("count", "query", "db", SCHEMA, [],
                                             query_scope=QueryScope(dataset_version="v1", split="train"))
    failure = saved.value
    assert failure.metadata["sql_candidate"]["sql"] == model.candidate.sql
    assert failure.metadata["sql_candidate"]["params"] == {"version": "v1"}
    assert failure.metadata["scope_validation"]["verified"] is False
    assert failure.metadata["llm_telemetry"]["fallback"] is False


@pytest.mark.asyncio
async def test_dispatch_preserves_failed_candidate_as_diagnostic_not_evidence(monkeypatch):
    async def reject(*args, **kwargs):
        assert args[7] == "actual previous SQL and validation"
        raise SQLScopeValidationError(UnverifiedScope("missing lineage"),
                                      SQLCandidate(sql="SELECT count(*) FROM predictions"), {})
    monkeypatch.setattr(TextToSQLService, "generate", reject)
    dispatcher = ToolDispatcher.__new__(ToolDispatcher)
    context = ToolExecutionContext(user_id="u", thread_id="t", resources=ResourceSummary(authorized_datasources=["db"]),
                                   datasource_id="db", schema_cache=SCHEMA,
                                   sql_repair_feedback="actual previous SQL and validation")
    result = await dispatcher._execute(ToolChoice(tool="text_to_sql", arguments={"goal": "count"}, reason=""), context)
    assert not result.success and result.data is None
    assert result.failure_code == "UNVERIFIED_SCOPE" and result.failed_stage == "sql_scope_validation"
    assert result.metadata["sql_candidate"]["sql"]


@pytest.mark.asyncio
async def test_failure_explanation_has_distinct_process_grounding_rules(monkeypatch):
    prompts = []
    outputs = [GroundedResponse(answer="查询未执行，版本范围尚未验证（observation:tool-2）；没有生成 CSV。"),
               ResponseGroundingCheck(supported=True)]
    async def stub(schema, system, payload):
        prompts.append(system)
        if len(prompts) == 1:
            assert schema is ProcessOnlyResponse
            assert schema.model_json_schema()["properties"]["claims"]["maxItems"] == 0
        return outputs.pop(0), {"latency_ms": 1, "fallback": False}
    monkeypatch.setattr("app.services.grounded_response.structured_call", stub)
    response, telemetry = await GroundedResponseService().generate("query and export", {
        "quality_status": "EXECUTION_FAILED", "evidence": [],
        "observations": [{"observation_id": "tool-2", "tool": "text_to_sql",
                          "result": {"success": False, "error": "version range unverified"}}]})
    assert "查询未执行" in response.answer and not response.claims
    assert telemetry["fallback"] is False
    assert "PROCESS FACTS" in prompts[0] and "Do not demand a nonexistent Evidence ID" in prompts[1]


@pytest.mark.asyncio
async def test_partial_mode_still_rejects_fabricated_scientific_results(monkeypatch):
    async def stub(schema, system, payload):
        value = ResponseGroundingCheck(supported=False, issues=["no executed result supports coverage=0"]) if schema is ResponseGroundingCheck else GroundedResponse(answer="coverage is 0")
        return value, {"latency_ms": 1}
    monkeypatch.setattr("app.services.grounded_response.structured_call", stub)
    with pytest.raises(ValueError, match="persisted-fact validation"):
        await GroundedResponseService().generate("coverage", {"quality_status": "EXECUTION_FAILED", "evidence": []})


@pytest.mark.parametrize("case", ["D09", "D08", "M02"])
def test_original_sql_loss_is_reported_not_filled_with_a_fabricated_candidate(case):
    record = json.loads((ROOT / f"reports/phase4a_correctness_closure_20261009/ui/closure-{case}.json").read_text(encoding="utf-8"))
    state = [event["payload_json"]["state"] for event in record["turns"][0]["events"] if event["event_type"] == "FINAL_ANSWER"][-1]
    failed = [result for call, result in zip(state["tool_calls"], state["observations"]) if call["tool"] == "text_to_sql" and not result["success"]]
    assert failed and all(result["data"] is None and "sql_candidate" not in result["metadata"] for result in failed)


def test_d06_uses_original_split_dimension_not_a_substitute_structure_metric():
    record = json.loads((ROOT / "reports/phase4a_correctness_closure_20261009/ui/closure-D06.json").read_text(encoding="utf-8"))
    state = ScientificAgentState.model_validate([event["payload_json"]["state"] for event in record["turns"][0]["events"] if event["event_type"] == "FINAL_ANSWER"][-1])
    result = assess_goal_coverage(state)
    assert result.required_dimensions == ["split"] and result.status == "SATISFIED"
    assert sum(row["structure_count"] for row in state.observations[-1].data) == 150
    # Coverage checks the recorded dimensions; it does not assert the old
    # answer explicitly delivered the total (it omitted it).
    assert "150" not in state.final_answer


@pytest.mark.parametrize("case", ["D09", "D06"])
def test_fresh_trace_uncallable_initial_plan_gets_reachable_schema_prerequisite(case):
    """The Runtime now compiles the prerequisite instead of wasting a replan.

    The historical assertion expected atomic rejection.  That became stale
    when the trusted runtime learned to add an authorised schema producer.  A
    stronger contract is that the repaired plan has a genuinely callable
    entrypoint and every text_to_sql step depends on it.
    """
    record = json.loads((ROOT / f"reports/phase4a_final_blocker_resolution_20261009/ui/blocker-{case}.json").read_text(encoding="utf-8"))
    turn = record["turns"][0]
    final = ScientificAgentState.model_validate([event["payload_json"]["state"] for event in turn["events"] if event["event_type"] == "FINAL_ANSWER"][-1])
    proposal = AgentDecision.model_validate(next(event["payload_json"]["decision"] for event in turn["events"] if event["event_type"] == "AGENT_DECISION"))
    final.plan, final.plan_id, final.schema_cache = [], None, {}
    run = DecisionRuntime.__new__(DecisionRuntime)
    run.owner = SimpleNamespace()
    run._emit = lambda *args, **kwargs: None
    run._apply_plan(final, proposal, {})
    schema_steps = [step for step in final.plan
                    if {"search_schema", "get_table_schema"} & set(step.selected_tools)]
    entry_steps = [step for step in schema_steps if not step.depends_on]
    assert len(entry_steps) == 1
    schema_step = entry_steps[0]
    assert set(eligible_call_tools(final)) & {"search_schema", "get_table_schema"}
    assert "text_to_sql" not in eligible_call_tools(final)
    assert all(schema_step.step_id in step.depends_on for step in final.plan
               if "text_to_sql" in step.selected_tools)
    assert final.plan_id and not final.schema_cache


def test_actual_mixed_file_comparison_satisfies_error_metric_presence_without_db_mae():
    from app.agents.scientific_agent import ScientificAgent
    record = json.loads((ROOT / "reports/phase4a_final_blocker_resolution_20261009/ui/blocker-M02.json").read_text(encoding="utf-8"))
    state = ScientificAgentState.model_validate([event["payload_json"]["state"] for event in record["turns"][0]["events"] if event["event_type"] == "FINAL_ANSWER"][-1])
    assert any("误差指标字段" in issue for issue in state.quality_issues)
    assert ScientificAgent._evidence_quality_issues(state) == []
    file_evidence = next(item for item in state.evidence if item.source_type == "file")
    file_evidence.value = {"row_count": 10}
    assert any("误差指标字段" in issue for issue in ScientificAgent._evidence_quality_issues(state))
