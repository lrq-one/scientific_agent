"""Targeted real-LLM four-turn conversation smoke test; not the Phase 4 benchmark."""

from __future__ import annotations

import json
import os
import uuid

from fastapi.testclient import TestClient

from app.main import app


def parse_events(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event_name = None
    for line in body.splitlines():
        if line.startswith("event: "):
            event_name = line[7:]
        elif line.startswith("data: ") and event_name:
            events.append((event_name, json.loads(line[6:])))
            event_name = None
    return events


def post_turn(client: TestClient, conversation_id: str, headers: dict[str, str], query: str):
    thread_id = f"followup-real-{uuid.uuid4()}"
    response = client.post(
        f"/api/conversations/{conversation_id}/chat/stream",
        headers=headers,
        json={"query": query, "thread_id": thread_id, "datasource_id": "training_db"},
    )
    response.raise_for_status()
    events = parse_events(response.text)
    task_id = next(data["task_id"] for _, data in events if data.get("task_id"))
    if any(name == "WAITING_FOR_USER" for name, _ in events):
        resumed = client.post(
            "/api/agent/resume",
            headers=headers,
            json={
                "thread_id": thread_id,
                "answer": "train_v2" if "train_v2" in query else "train_v3",
                "conversation_id": conversation_id,
                "task_id": task_id,
            },
        )
        resumed.raise_for_status()
        events += parse_events(resumed.text)
    return task_id, thread_id, events


def main() -> None:
    assert os.getenv("LLM_MODEL") == "qwen3.7-flash"
    assert os.getenv("LLM_API_BASE") and os.getenv("LLM_API_KEY")
    assert os.getenv("ADMIN_DATABASE_URL") and os.getenv("DATABASE_URL")
    headers = {"X-User-Id": f"followup-real-{uuid.uuid4()}"}
    with TestClient(app) as client:
        conversation_id = client.post(
            "/api/conversations", headers=headers, json={"title": "Follow-up real-LLM smoke"}
        ).json()["id"]
        first_task, _, first = post_turn(
            client, conversation_id, headers, "统计 training_db 中 train_v3 不同结构类型覆盖。"
        )
        first_tools = [data for name, data in first if name == "TOOL_FINISHED"]
        first_evidence = [data for name, data in first if name == "EVIDENCE_ADDED"]
        sql_generators = [
            (data.get("result") or {}).get("metadata", {}).get("generator")
            for data in first_tools if data.get("tool") == "text_to_sql"
        ]
        assert first_evidence, "first task did not persist evidence"
        assert sql_generators == ["llm_structured_output"], sql_generators
        assert any(name == "FINAL_ANSWER" for name, _ in first)

        followups = []
        for query in ("这些结果怎么得到的？", "把 fused-ring 的原始证据告诉我。"):
            task_id, _, events = post_turn(client, conversation_id, headers, query)
            trace = next(data for name, data in events if name == "FOLLOW_UP_TYPE")
            assert trace["follow_up_type"] == "EVIDENCE_EXPLANATION"
            assert trace["previous_task_id"] == first_task
            assert trace["new_tool_calls"] == 0
            assert trace["loaded_evidence_count"] >= 1
            assert not any(name in {"TOOL_STARTED", "TOOL_FINISHED"} for name, _ in events)
            assert any(name == "FINAL_ANSWER" for name, _ in events)
            followups.append({"task_id": task_id, "evidence_count": trace["loaded_evidence_count"]})

        refine_task, _, refine = post_turn(
            client, conversation_id, headers, "换成 train_v2 再分析一次。"
        )
        refine_trace = next(data for name, data in refine if name == "FOLLOW_UP_TYPE")
        assert refine_trace["follow_up_type"] == "REFINE_PREVIOUS_TASK"
        assert any(name == "INTENT_RESOLVED" for name, _ in refine)
        refine_evidence = [data for name, data in refine if name == "EVIDENCE_ADDED"]
        assert refine_evidence, "train_v2 refinement produced no evidence"
        assert all(
            data["evidence"].get("dataset_version") == "train_v2" for data in refine_evidence
        )

        detail = client.get(f"/api/conversations/{conversation_id}", headers=headers).json()
        assert len(detail["tasks"]) == 4
        assert all(task["conversation_id"] == conversation_id for task in detail["tasks"])
        print(json.dumps({
            "model": os.getenv("LLM_MODEL"),
            "conversation_id": conversation_id,
            "first_task_id": first_task,
            "first_sql_generator": sql_generators[0],
            "first_evidence_count": len(first_evidence),
            "evidence_followups": followups,
            "refine_task_id": refine_task,
            "refine_entered_workflow": True,
            "refine_evidence_count": len(refine_evidence),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
