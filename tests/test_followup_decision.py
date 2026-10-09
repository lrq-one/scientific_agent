"""Offline state/composition contracts; real semantic paraphrases are checked by the live smoke."""
import os
import uuid
import pytest
from fastapi.testclient import TestClient

from app.models.schemas import FollowUpDecision, TaskRefinementPatch
from app.services.followup import (ConversationContextResolver, StateSufficiencyResolver,
                                   build_provenance, compose_followup_response, conversation_context_summary)
from conftest import StructuredDecisionStub


def persisted_context(rows=None):
    return {"task": {"id": "analysis-original", "status": "completed", "conversation_id": "c",
                     "intent_json": {"goal": "train_v3 结构覆盖", "datasource_id": "training_db"}},
            "messages": [{"role": "user", "content": "统计 train_v3 结构覆盖"}],
            "assistant_message": {"content": "fused_ring 有 2 条记录。"}, "is_first_substantive": True,
            "events": [{"event_type": "TOOL_FINISHED", "payload_json": {"tool": "execute_readonly_sql",
                "result": {"success": True, "source": "training_db", "data": rows if rows is not None else
                           [{"structure_type": "fused_ring", "count": 2}, {"structure_type": "linear", "count": 8}],
                "metadata": {"sql": "SELECT structure_type, count(*) FROM molecules GROUP BY 1",
                             "params": {"dataset_version": "train_v3"}}}}}],
            "evidence": [{"id": "ev-real", "claim": "fused count", "value_json": 2, "source": "training_db",
                          "tool_call_id": "call-real", "dataset_version": "train_v3"}],
            "claims": [{"id": "claim-real", "status": "supported", "claim_text": "fused count = 2",
                        "evidence_ids_json": ["ev-real"]}], "artifacts": []}


@pytest.mark.parametrize("contents", [["sql", "params"], ["raw_rows"], ["claim", "evidence", "uncertainty"],
                                      ["tools"], ["sql", "params", "raw_rows"], ["error"]])
def test_persisted_state_overrides_llm_execution_suggestion(contents):
    context = persisted_context()
    decision = FollowUpDecision(interaction_type="PROVENANCE_QUERY", requested_content=contents, requires_execution=True)
    resolved = StateSufficiencyResolver().resolve(decision, context)
    assert resolved.action == "REUSE" and not resolved.requires_execution
    answer = compose_followup_response(decision, build_provenance(context), resolved)
    if "sql" in contents:
        assert "SELECT structure_type" in answer
    if "raw_rows" in contents:
        assert "| fused_ring | 2 |" in answer and "| linear | 8 |" in answer
    if "evidence" in contents:
        assert "ev-real" in answer and "claim-real" in answer
    else:
        assert "### Evidence" not in answer
    if "sql" not in contents:
        assert "```sql" not in answer


def test_new_scope_and_rerun_override_llm_no_execution_suggestion():
    resolver = StateSufficiencyResolver()
    patch = TaskRefinementPatch(dataset_version="train_v2", changed_fields=["dataset_version"])
    decision = FollowUpDecision(interaction_type="TASK_REFINEMENT", requested_content=["answer"], refinement_patch=patch)
    assert resolver.resolve(decision, persisted_context()).requires_execution
    assert resolver.resolve(FollowUpDecision(interaction_type="RERUN"), persisted_context()).requires_execution
    same = decision.model_copy(update={"refinement_patch": TaskRefinementPatch(dataset_version="train_v3")})
    assert not resolver.resolve(same, persisted_context()).requires_execution


def test_missing_history_never_guesses_or_silently_runs_query():
    context = persisted_context()
    context["events"] = []
    decision = FollowUpDecision(interaction_type="PROVENANCE_QUERY", requested_content=["sql", "raw_rows"])
    result = StateSufficiencyResolver().resolve(decision, context)
    assert result.action == "INSUFFICIENT" and not result.requires_execution
    answer = compose_followup_response(decision, build_provenance(context), result)
    assert answer.startswith("INSUFFICIENT_EVIDENCE") and "sql" in answer and "raw_rows" in answer
    assert StateSufficiencyResolver().resolve(decision, None).action == "CLARIFY"


def test_zero_rows_are_known_result_not_scientific_absence():
    context = persisted_context(rows=[])
    decision = FollowUpDecision(interaction_type="PROVENANCE_QUERY", requested_content=["raw_rows"])
    result = StateSufficiencyResolver().resolve(decision, context)
    assert result.action == "REUSE" and not result.requires_execution
    assert "不等价于科学对象不存在" in compose_followup_response(decision, build_provenance(context), result)


def test_file_rows_reuse_preserves_anomalies_and_source_without_sql():
    context = persisted_context()
    rows = [{"molecule_id": "M006", "predicted_rt": "not_recorded", "observed_rt": None}]
    context["events"] = [{"event_type": "TOOL_FINISHED", "payload_json": {
        "tool": "read_csv", "tool_call_id": "read-1", "result": {
            "success": True, "source": "quality_copy.csv", "data": rows}}}]
    decision = FollowUpDecision(interaction_type="PROVENANCE_QUERY", requested_content=["raw_rows"])
    p = build_provenance(context)
    assert p.sql_raw_result is None
    assert p.file_raw_results[0]["rows"] == rows
    assert p.file_raw_results[0]["source"] == "quality_copy.csv"
    assert StateSufficiencyResolver().resolve(decision, context).action == "REUSE"


def test_actual_sql_version_overrides_stale_evidence_label():
    context = persisted_context()
    context["evidence"][0]["dataset_version"] = "train_v2"
    result = context["events"][0]["payload_json"]["result"]
    result["metadata"] = {"sql": "SELECT * FROM training_molecules WHERE dataset_version = 'train_v3'", "params": {}}
    provenance = build_provenance(context)
    assert provenance.dataset_version == "train_v3"
    assert any("不一致" in warning for warning in provenance.uncertainties)
    decision = FollowUpDecision(interaction_type="TASK_REFINEMENT", requested_content=["answer"],
        refinement_patch=TaskRefinementPatch(dataset_version="train_v2"))
    assert StateSufficiencyResolver().resolve(decision, context).requires_execution


def test_absent_params_are_not_fabricated_as_empty_binding():
    context = persisted_context()
    del context["events"][0]["payload_json"]["result"]["metadata"]["params"]
    decision = FollowUpDecision(interaction_type="PROVENANCE_QUERY", requested_content=["params"])
    result = StateSufficiencyResolver().resolve(decision, context)
    assert result.action == "INSUFFICIENT" and result.missing_content == ["params"]


@pytest.mark.asyncio
async def test_target_integrity_default_and_semantic_unavailability():
    contexts = [persisted_context(), persisted_context()]
    contexts[1]["task"]["id"] = "older-task"
    model = StructuredDecisionStub([FollowUpDecision(interaction_type="EVIDENCE_QUERY", target_task_id="older-task")])
    result = await ConversationContextResolver(model).resolve_async("show basis", contexts[0], contexts)
    assert result.target_task_id == "analysis-original"  # No explicit older reference, no stale reuse.
    model = StructuredDecisionStub([FollowUpDecision(interaction_type="EVIDENCE_QUERY", target_task_id="other-user-task", target_reference="EXPLICIT",
                                                    target_selector_type="ORDER", target_reference_text="the earlier one")])
    result = await ConversationContextResolver(model).resolve_async("the earlier one", contexts[0], contexts)
    assert result.interaction_type == "CLARIFY" and result.target_task_id is None
    resolver = ConversationContextResolver()
    resolver._configured_llm = lambda: None
    assert (await resolver.resolve_async("arbitrary language", contexts[0])).interaction_type == "CLARIFY"


@pytest.mark.asyncio
async def test_previous_message_uuid_cannot_be_hallucinated_as_current_user_reference():
    contexts = [persisted_context(), persisted_context()]
    contexts[1]["task"]["id"] = str(uuid.uuid4())
    contexts[0]["messages"][0]["content"] = f"rerun task {contexts[1]['task']['id']}"
    model = StructuredDecisionStub([FollowUpDecision(interaction_type="ERROR_QUESTION", requested_content=["error"],
        target_reference="EXPLICIT", target_selector_type="TASK_ID", target_task_id=contexts[1]["task"]["id"],
        target_reference_text=contexts[1]["task"]["id"])])
    result = await ConversationContextResolver(model).resolve_async("为什么刚才失败？", contexts[0], contexts)
    assert result.interaction_type == "ERROR_QUESTION" and result.target_task_id == contexts[0]["task"]["id"]


def test_bounded_context_contains_sufficiency_facts_not_raw_rows():
    summary = conversation_context_summary("question", [persisted_context()] * 100).model_dump()
    assert len(summary["recent_tasks"]) == 3
    descriptor = summary["recent_tasks"][0]
    assert descriptor["has_sql"] and descriptor["has_raw_rows"] and descriptor["has_evidence"]
    assert descriptor["dataset_version"] == "train_v3"
    assert "sql_raw_result" not in descriptor


def test_error_composition_preserves_recovery_without_unrelated_sql():
    context = persisted_context()
    context["events"] += [{"event_type": "ERROR", "payload_json": {"error": "unknown alias: v1"}},
                          {"event_type": "RECOVERY_DECISION", "payload_json": {"action": "fail_safely", "reason": "schema mismatch"}}]
    decision = FollowUpDecision(interaction_type="ERROR_QUESTION", requested_content=["error"])
    answer = compose_followup_response(decision, build_provenance(context), StateSufficiencyResolver().resolve(decision, context))
    assert "unknown alias: v1" in answer and "fail_safely" in answer
    assert "```sql" not in answer and "### Evidence" not in answer


@pytest.mark.skipif(not os.getenv("ADMIN_DATABASE_URL"), reason="requires product PostgreSQL")
@pytest.mark.parametrize("contents", [["sql", "params"], ["raw_rows"], ["evidence"], ["sql", "params", "raw_rows"], ["error"]])
def test_http_content_reuse_skips_entire_agent_and_does_not_change_analysis_target(monkeypatch, offline_followup_model, contents):
    from app.api import routes
    import app.main as main_module
    from app.main import app
    from app.services.conversation_history import ConversationRepository
    context = persisted_context()
    repository = ConversationRepository(os.environ["ADMIN_DATABASE_URL"])
    user = f"semantic-{uuid.uuid4()}"
    conversation = repository.create(user)["id"]
    original = repository.start_task(conversation, f"thread-{uuid.uuid4()}")
    repository.add_message(conversation, "user", context["messages"][0]["content"], original)
    repository.update_task(original, intent=context["task"]["intent_json"], status="completed")
    for item in context["events"]:
        repository.add_event(original, item["event_type"], item["payload_json"])
    repository.add_message(conversation, "assistant", "prior result", original)
    repository.add_evidence(original, {"evidence_id": "ev-1", "claim": "count", "value": 2,
        "source_type": "database", "source": "training_db", "tool_call_id": "call-real", "dataset_version": "train_v3"})
    concept = repository.start_task(conversation, f"concept-{uuid.uuid4()}")
    repository.add_message(conversation, "user", "general SQL definition", concept)
    repository.update_task(concept, intent={"task_type": "general", "goal": "general SQL definition"}, status="completed")
    assert repository.latest_analysis_context(conversation, user)["task"]["id"] == original
    offline_followup_model.decisions = [FollowUpDecision(interaction_type="PROVENANCE_QUERY", requested_content=contents, requires_execution=True)]
    class ForbiddenAgent:
        async def stream(self, *args, **kwargs):
            raise AssertionError("history explanation entered Agent workflow")
            yield
    monkeypatch.setattr(routes, "agent", ForbiddenAgent())
    # This HTTP integration fixture shares the configured development DB.
    # Its TestClient lifespan must not reconcile another process's live tasks
    # or reseed demo data while real browser acceptance is running.
    monkeypatch.setattr(main_module, "conversation_repository", lambda: None)
    monkeypatch.setattr(main_module, "ENABLE_DEMO_DATA", False)
    with TestClient(app) as client:
        response = client.post(f"/api/conversations/{conversation}/chat/stream", headers={"X-User-Id": user},
                               json={"query": "请解释上一轮的信息", "thread_id": f"follow-{uuid.uuid4()}"})
        assert response.status_code == 200 and '"new_tool_calls": 0' in response.text
        assert all(f"event: {name}" not in response.text for name in ["INTENT_RESOLVED", "PLAN_CREATED", "TOOL_STARTED", "TOOL_FINISHED"])
        assert repository.latest_analysis_context(conversation, user)["task"]["id"] == original
