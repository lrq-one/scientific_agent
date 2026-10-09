"""One real DB rerun, then verify persisted provenance; no benchmark."""
import asyncio
import json
from time import perf_counter
import uuid
import httpx
from completion_product_smoke import sse_events, telemetry


async def main():
    conversation = "42b59f6f-608a-4366-a04a-3de774402565"
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", headers={"X-User-Id": "demo-researcher"}, trust_env=False, timeout=180) as client:
        before = (await client.get(f"/api/conversations/{conversation}")).json()
        previous = next(task for task in before["tasks"] if task["id"] == "409fc981-2693-4b33-83e6-e79827dbc4c0")
        start = perf_counter()
        response = await client.post(f"/api/conversations/{conversation}/chat/stream", json={"query": f"重新回答 task {previous['id']}", "thread_id": f"completion-db-recheck-{uuid.uuid4()}"})
        response.raise_for_status()
        events = sse_events(response.text)
        finals = [item["data"] for item in events if item["event"] == "FINAL_ANSWER"]
        assert finals, [item for item in events if item["event"] == "ERROR"]
        final = finals[-1]
        followup = next(item["data"] for item in events if item["event"] == "FOLLOW_UP_TYPE")
        assert followup["follow_up_type"] == "RERUN_PREVIOUS_TASK"
        assert followup["previous_task_id"] == previous["id"]
        assert "不同结构类型" in final["state"]["goal"]
        sql = next((item["data"]["result"] for item in events if item["event"] == "TOOL_FINISHED" and item["data"].get("tool") == "execute_readonly_sql"), None)
        assert sql is not None, {"task_id": final["task_id"], "answer": final["answer"]}
        assert sql["success"] and sql["metadata"]["read_only"] and sql["metadata"]["backend"] == "postgres"
        assert len(sql["data"]) >= 3, sql
        assert final["state"]["quality_status"] == "SUPPORTED_CONCLUSION", final["answer"]
        usage = telemetry(events)
        detail = (await client.get(f"/api/conversations/{conversation}")).json()
        persisted = [item for item in detail["evidence"] if item["task_id"] == final["task_id"]]
        assert len(persisted) == len(final["state"]["evidence"]) > 0
        print(json.dumps({"conversation_id": conversation, "task_id": final["task_id"], "previous_task_id": followup["previous_task_id"], "http_status": response.status_code,
            "latency_ms": round((perf_counter()-start)*1000, 2), "quality": final["state"]["quality_status"], "sql_rows": sql["data"], "sql": sql["metadata"]["sql"], "params": sql["metadata"]["params"],
            "evidence_count": len(final["state"]["evidence"]), "llm_telemetry": usage}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
