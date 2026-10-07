from __future__ import annotations

import json
import uuid

import httpx


BASE = "http://127.0.0.1:8000"
HEADERS = {"X-User-Id": "e2e-product-validation"}


def create(client: httpx.Client, title: str) -> dict:
    response = client.post("/api/conversations", headers=HEADERS, json={"title": title})
    response.raise_for_status()
    return response.json()


def run_chat(client: httpx.Client, conversation_id: str, query: str) -> list[str]:
    response = client.post(
        f"/api/conversations/{conversation_id}/chat/stream",
        headers=HEADERS,
        json={"query": query, "thread_id": str(uuid.uuid4()), "datasource_id": "training_db"},
        timeout=120,
    )
    response.raise_for_status()
    events = [line.removeprefix("event: ") for line in response.text.splitlines() if line.startswith("event: ")]
    if "FINAL_ANSWER" not in events:
        raise AssertionError(f"chat did not finish: {events[-5:]}")
    return events


def main() -> None:
    with httpx.Client(base_url=BASE, trust_env=False, timeout=30) as client:
        conversation_a = create(client, "validation A")
        simple_events = run_chat(client, conversation_a["id"], "model_v1.csv 有多少行？")
        conversation_b = create(client, "validation B")

        # Simulate a refresh by reading both server-side sources of truth again.
        listed = client.get("/api/conversations", headers=HEADERS).raise_for_status().json()["items"]
        ids = {item["id"] for item in listed}
        assert {conversation_a["id"], conversation_b["id"]} <= ids
        messages_a = client.get(f"/api/conversations/{conversation_a['id']}/messages", headers=HEADERS).raise_for_status().json()["items"]
        messages_b = client.get(f"/api/conversations/{conversation_b['id']}/messages", headers=HEADERS).raise_for_status().json()["items"]
        assert [item["role"] for item in messages_a] == ["user", "assistant"]
        assert messages_b == []

        mixed_query = "比较 model_v1.csv 和 model_v2.csv 的 RT 表现，分析 fused-ring 误差，并检查 training_db 训练覆盖。"
        mixed_events = run_chat(client, conversation_a["id"], mixed_query)
        detail = client.get(f"/api/conversations/{conversation_a['id']}", headers=HEADERS).raise_for_status().json()
        latest_task = detail["tasks"][-1]
        task_events = [item for item in detail["events"] if item["task_id"] == latest_task["id"]]
        event_types = {item["event_type"] for item in task_events}
        assert {"INTENT_RESOLVED", "PLAN_CREATED", "TOOL_CANDIDATES", "TOOL_STARTED", "EVIDENCE_ADDED", "ARTIFACT_CREATED", "FINAL_ANSWER"} <= event_types
        assert latest_task["selected_skills_json"]
        assert detail["evidence"]
        assert len(detail["artifacts"]) >= 2

        artifact = detail["artifacts"][0]
        download = client.get(f"/api/artifacts/{artifact['artifact_id']}/download", headers=HEADERS)
        download.raise_for_status()
        assert download.content.startswith(b"\x89PNG") or b"model,mae" in download.content

        print(json.dumps({
            "conversation_a": conversation_a["id"],
            "conversation_b": conversation_b["id"],
            "simple_event_count": len(simple_events),
            "mixed_event_count": len(mixed_events),
            "persisted_messages_a": len(client.get(f"/api/conversations/{conversation_a['id']}/messages", headers=HEADERS).json()["items"]),
            "persisted_messages_b": len(messages_b),
            "selected_skills": latest_task["selected_skills_json"],
            "trace_event_types": sorted(event_types),
            "evidence_count": len(detail["evidence"]),
            "artifact_files": [item["filename"] for item in detail["artifacts"]],
            "artifact_download_bytes": len(download.content),
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
