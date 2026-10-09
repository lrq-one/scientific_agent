"""Targeted real-Qwen paraphrase/product smoke. No analysis rerun or Phase 4 benchmark."""
import asyncio
import json
import os
from time import perf_counter
import uuid
import httpx

from app.services.conversation_history import ConversationRepository
from app.services.followup import ConversationContextResolver, StateSufficiencyResolver
from completion_product_smoke import sse_events

CONVERSATION = "42b59f6f-608a-4366-a04a-3de774402565"
CASES = [
    ("SQL是什么？", {"sql", "params"}),
    ("刚才到底查了什么？", {"sql", "params"}),
    ("把查询语句给我看下", {"sql", "params"}),
    ("展示原始rows", {"raw_rows"}),
    ("数据库实际返回了啥？", {"raw_rows"}),
    ("别总结，把原始结果给我", {"raw_rows"}),
    ("给我证据", {"evidence"}),
    ("依据是什么？", {"claim", "evidence", "uncertainty"}),
    ("这个数字哪里来的？", {"evidence"}),
    ("为什么这么说？", {"claim", "evidence", "uncertainty"}),
    ("SQL是什么，原始结果也给我", {"sql", "params", "raw_rows"}),
]


async def main():
    outputs = []
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", headers={"X-User-Id": "demo-researcher"},
                                 trust_env=False, timeout=120) as client:
        before = (await client.get(f"/api/conversations/{CONVERSATION}")).json()
        original = next(task for task in reversed(before["tasks"]) if task["id"] == "7742e23f-0811-46a3-ac8f-6a09187af913")
        for query, required in CASES:
            started = perf_counter()
            response = await client.post(f"/api/conversations/{CONVERSATION}/chat/stream",
                json={"query": query, "thread_id": f"semantic-smoke-{uuid.uuid4()}"})
            response.raise_for_status()
            events = sse_events(response.text)
            trace = next(e["data"] for e in events if e["event"] == "FOLLOW_UP_TYPE")
            final = next(e["data"] for e in events if e["event"] == "FINAL_ANSWER")
            observed = {"query": query, "interaction_type": trace["interaction_type"],
                        "requested_content": trace["requested_content"], "task_id": final["task_id"],
                        "previous_task_id": trace["previous_task_id"], "new_tool_calls": trace["new_tool_calls"],
                        "state_sufficiency": trace["state_sufficiency"], "http_status": response.status_code,
                        "latency_ms": round((perf_counter()-started)*1000, 2), "llm": trace["llm_telemetry"]}
            print(json.dumps(observed, ensure_ascii=False), flush=True)
            assert required.issubset(trace["requested_content"]), observed
            assert trace["interaction_type"] in {"PROVENANCE_QUERY", "EVIDENCE_QUERY", "RESULT_EXPLANATION"}, observed
            assert trace["previous_task_id"] == original["id"], observed
            assert not trace["requires_execution"] and trace["new_tool_calls"] == 0, observed
            assert not any(e["event"] in {"INTENT_RESOLVED", "PLAN_CREATED", "TOOL_STARTED", "TOOL_FINISHED"} for e in events), observed
            assert trace["llm_telemetry"]["actual_model"] == "qwen3.7-flash" and trace["llm_telemetry"]["fallback"] is False
            if "sql" in required:
                assert "SELECT m.structure_type" in final["answer"] and "train_v3" in final["answer"]
            if "raw_rows" in required:
                assert "| aromatic | 1 |" in final["answer"] and "| linear | 3 |" in final["answer"]
            outputs.append(observed)
        # Real structured classifications + actual persisted sufficiency, without spending SQL/Agent calls.
        repository = ConversationRepository(os.environ["ADMIN_DATABASE_URL"])
        contexts = repository.recent_analysis_contexts(CONVERSATION, "demo-researcher")
        resolver = ConversationContextResolver()
        for query, expected, execution in [("那train_v2呢？", "TASK_REFINEMENT", True),
                                            ("重新分析", "RERUN", True),
                                            ("为什么刚才失败？", "ERROR_QUESTION", False)]:
            decision = await resolver.resolve_async(query, contexts[0], contexts)
            target = next((c for c in contexts if c["task"]["id"] == decision.target_task_id), None)
            sufficiency = StateSufficiencyResolver().resolve(decision, target)
            observed = {"query": query, "decision": decision.model_dump(), "sufficiency": sufficiency.model_dump(), "agent_executed": False}
            print(json.dumps(observed, ensure_ascii=False), flush=True)
            assert decision.interaction_type == expected and sufficiency.requires_execution == execution, observed
            assert decision.target_task_id == original["id"], observed
            assert decision.llm_telemetry["actual_model"] == "qwen3.7-flash" and decision.llm_telemetry["fallback"] is False
            outputs.append(observed)
        print(json.dumps({"passed": len(outputs), "real_product_followups": len(CASES), "new_analysis_executions": 0}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
