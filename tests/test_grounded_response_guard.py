import pytest

from app.models.schemas import GroundedResponse
from app.services.grounded_response import GroundedResponseService, ResponseGroundingCheck
from app.models.schemas import FollowUpDecision, ProvenanceRecord, StateSufficiency


@pytest.mark.asyncio
async def test_public_export_answer_is_corrected_before_delivery(monkeypatch):
    outputs = [GroundedResponse(answer="CSV includes unsupported causal columns"),
               ResponseGroundingCheck(supported=False, issues=["actual CSV has only structure_type,count"]),
               GroundedResponse(answer="下载表仅含 structure_type,count；没有模型训练绑定，不能作因果解释。"),
               ResponseGroundingCheck(supported=True)]
    payloads = []
    async def stub(schema, system, payload):
        payloads.append(payload)
        return outputs.pop(0), {"fallback": False, "actual_model": "test-stub", "latency_ms": 1,
            "input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
    monkeypatch.setattr("app.services.grounded_response.structured_call", stub)
    answer, usage = await GroundedResponseService().generate("export", {
        "exported_tables": [{"columns": ["structure_type", "count"], "rows": [{"structure_type": "fused_ring", "count": 1}]}],
        "unverified_model_training_binding": True})
    assert "causal columns" not in answer.answer
    assert payloads[2]["response_validation"]["grounding_issues"]
    assert usage["response_attempts"] == 2 and usage["measured_llm_calls"] == 4
    assert usage["total_tokens"] == 20 and usage["fallback"] is False


@pytest.mark.asyncio
async def test_unsupported_export_answer_never_passes_as_success(monkeypatch):
    async def stub(schema, system, payload):
        output = ResponseGroundingCheck(supported=False, issues=["invented contents"]) if schema is ResponseGroundingCheck else GroundedResponse(answer="invented contents")
        return output, {"latency_ms": 1}
    monkeypatch.setattr("app.services.grounded_response.structured_call", stub)
    with pytest.raises(ValueError, match="persisted-fact validation"):
        await GroundedResponseService().generate("export", {"exported_tables": [{"columns": ["count"]}]})


@pytest.mark.asyncio
async def test_followup_keeps_claim_ids_distinct_from_evidence_ids(monkeypatch):
    captured = {}
    async def compose(self, question, facts):
        captured.update(facts)
        return GroundedResponse(answer="stub"), {}
    monkeypatch.setattr(GroundedResponseService, "generate", compose)
    provenance = ProvenanceRecord(claims=[{"id": "claim-uuid", "claim_text": "fused MAE", "evidence_ids_json": ["evidence-uuid"]}],
        evidence=[{"id": "evidence-uuid", "claim": "group MAE", "value_json": {"mae": 2.4}}])
    decision = FollowUpDecision(interaction_type="EVIDENCE_QUERY", requested_content=["claim", "evidence"])
    await GroundedResponseService().followup("basis", decision, provenance, StateSufficiency(action="REUSE"))
    assert captured["claims"][0]["claim_id"] == "claim-uuid"
    assert captured["claims"][0]["evidence_ids"] == ["evidence-uuid"]
    assert "id" not in captured["claims"][0]
    assert captured["evidence"][0]["evidence_id"] == "evidence-uuid"


@pytest.mark.asyncio
async def test_reused_demo_disclosure_is_not_appended_twice(monkeypatch):
    disclosure = "数据说明：本结果包含 synthetic/demo 数据，仅用于演示，不能外推为真实科研结论。"
    async def stub(*args, **kwargs):
        return GroundedResponse(answer=disclosure), {"latency_ms": 1}
    monkeypatch.setattr("app.services.grounded_response.structured_call", stub)
    answer, _ = await GroundedResponseService().generate("explain", {"data_origins": ["synthetic_demo"]})
    assert answer.answer.count(disclosure) == 1
