import importlib
import json
import pytest
import httpx


def test_variant_requires_evaluation_server(monkeypatch):
    import app.services.evaluation_variant as v
    monkeypatch.setenv("AGENT_EVALUATION_VARIANT", "NO_REPLAN")
    monkeypatch.delenv("AGENT_EVALUATION_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="explicitly enabled"):
        importlib.reload(v)
    monkeypatch.setenv("AGENT_EVALUATION_VARIANT", "FULL")
    importlib.reload(v)


@pytest.mark.asyncio
async def test_all_http_attempts_include_failed_json_tokens(tmp_path, monkeypatch):
    from evaluation.frozen.telemetry import install, request_identity
    original=httpx.AsyncClient.send
    monkeypatch.setattr(httpx.AsyncClient,"_evaluation_accounted",False,raising=False)
    target=tmp_path/"provider.jsonl"
    restore=install(target,"FULL")
    try:
        token=request_identity.set({"conversation_id":"fixture-conversation"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={
            "model":"qwen3.7-flash","usage":{"prompt_tokens":31,"completion_tokens":9,"total_tokens":40},
            "choices":[{"finish_reason":"length","message":{"reasoning_content":"MUST NOT BE STORED","content":"bad json"}}]}))) as c:
            for _ in range(2):await c.post("https://example.test/chat/completions",json={"model":"qwen3.7-flash"},headers={"Authorization":"secret-key"})
        request_identity.reset(token)
        rows=[json.loads(line) for line in target.read_text().splitlines()]
        finished=[r for r in rows if r["phase"]=="provider_finished"]
        assert len(finished)==2
        assert sum(r["usage"]["total_tokens"] for r in finished)==80
        assert all(r["finish_reason"]=="length" and r["http_status"]==200 for r in finished)
        assert "MUST NOT BE STORED" not in target.read_text()
        assert "secret-key" not in target.read_text()
    finally:
        restore()


def test_frozen_coverage_and_gold_hidden():
    from evaluation.frozen.prepare import REPORT
    cases=[json.loads(x) for x in (REPORT/"benchmark_manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    gold=[json.loads(x) for x in (REPORT/"gold/cases.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(cases)==len(gold)==60
    assert len({c["case_id"] for c in cases})==60
    assert all("expected_numeric_facts" not in c for c in cases)
    for subset in ["skill","replan","followup","evidence"]:
        assert sum(subset in c["subsets"] for c in cases)==15
    assert all(not any("gold" in name for name in c["files"]) for c in cases)
