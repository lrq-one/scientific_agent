"""Regression for exact-request retry and grounded historical error reuse.

All tests are offline. No Qwen, PostgreSQL or tool execution is required.
"""
from __future__ import annotations

import pytest

from app.models.schemas import FollowUpDecision, GroundedResponse, ProvenanceRecord, StateSufficiency
from app.services.followup import (
    ConversationContextResolver,
    StateSufficiencyResolver,
    build_provenance,
    compose_followup_response,
    workflow_query,
)
from app.services.grounded_response import GroundedResponseService


def _context(request: str, task_id: str):
    return {
        "task": {"id": task_id, "status": "failed", "conversation_id": "current-conversation",
                 "intent_json": {"goal": request, "datasource_id": "training_db"}},
        "messages": [{"role": "user", "content": request, "task_id": task_id}],
        "events": [{"event_type": "ERROR", "payload_json": {
            "error": "previous query_checker could not execute"}}],
        "evidence": [], "artifacts": [], "claims": [],
        "assistant_message": {"content": "上一轮数据分析失败"},
    }


@pytest.mark.asyncio
async def test_exact_repeat_reruns_latest_failed_analysis_without_classification_model():
    request = ("用 training_db 的真实 predictions 对比 baseline-run、candidate-run "
               "按结构分组的 MAE，并说明合成数据局限；将实际结果导出 CSV。")
    context = _context(request, "task-latest")
    resolver = ConversationContextResolver()
    resolver._configured_llm = lambda: (_ for _ in ()).throw(
        AssertionError("Exact repeat must not call paid classification model")
    )
    decision = await resolver.resolve_async(request, context, [context])
    assert decision.interaction_type == "RERUN"
    assert decision.follow_up_type == "RERUN_PREVIOUS_TASK"
    assert decision.target_task_id == "task-latest"
    assert decision.source == "exact_repeat"
    assert decision.llm_telemetry["llm_called"] is False
    sufficiency = StateSufficiencyResolver().resolve(decision, context)
    assert sufficiency.action == "EXECUTE" and sufficiency.requires_execution
    assert workflow_query(request, decision.follow_up_type, context)[0] == request


@pytest.mark.asyncio
async def test_whitespace_normalized_exact_repeat_targets_newest_not_older_task():
    latest = _context("统计 training_db 中 train_v3 的结构覆盖", "latest")
    older = _context("统计更早版本", "older")
    resolver = ConversationContextResolver()
    resolver._configured_llm = lambda: (_ for _ in ()).throw(
        AssertionError("No model needed for a direct repeat")
    )
    decision = await resolver.resolve_async(
        "  统计 training_db 中 train_v3 的结构覆盖  ", latest, [latest, older]
    )
    assert decision.target_task_id == "latest"


@pytest.mark.asyncio
async def test_different_request_is_not_forced_into_rerun():
    latest = _context("分析 train_v3 的结构覆盖", "latest")
    class FakeModel:
        def with_structured_output(self, schema): return self
        async def ainvoke(self, *args, **kwargs):
            return FollowUpDecision(interaction_type="NEW_TASK").model_dump(mode="json")
    result = await ConversationContextResolver(FakeModel()).resolve_async(
        "分析 train_v4 的结构覆盖", latest, [latest]
    )
    assert result.interaction_type == "NEW_TASK" and result.source == "llm_structured"


def test_process_error_reply_uses_actual_recorded_error_not_fake_scientific_evidence():
    context = _context("统计 train_v3", "latest")
    decision = FollowUpDecision(interaction_type="ERROR_QUESTION", requested_content=["error"])
    sufficiency = StateSufficiencyResolver().resolve(decision, context)
    assert not sufficiency.requires_execution
    reply = compose_followup_response(decision, build_provenance(context), sufficiency)
    assert "previous query_checker could not execute" in reply
    assert "Evidence IDs" not in reply
    assert "没" not in "Fake Evidence IDs"


@pytest.mark.asyncio
async def test_claim_only_followup_passes_real_persisted_evidence_ids_to_composer(monkeypatch):
    captured = {}
    async def fake(self, question, facts):
        captured.update(facts)
        return GroundedResponse(answer="historical claim summary"), {}
    monkeypatch.setattr(GroundedResponseService, "generate", fake)
    provenance = ProvenanceRecord(
        claims=[{"id": "claim-uuid", "claim_text": "descriptive MAE", "evidence_ids_json": ["evidence-uuid"]}],
        evidence=[{"id": "evidence-uuid", "claim": "observed result", "value_json": {"mae": 3.5}}],
    )
    decision = FollowUpDecision(interaction_type="RESULT_EXPLANATION", requested_content=["claim"])
    await GroundedResponseService().followup("解释结论", decision, provenance, StateSufficiency(action="REUSE"))
    assert captured["claims"][0]["claim_id"] == "claim-uuid"
    assert captured["claims"][0]["evidence_ids"] == ["evidence-uuid"]
    assert captured["evidence"][0]["evidence_id"] == "evidence-uuid"
