from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import pytest

from app.models.schemas import SSEEvent


@pytest.mark.skipif(not os.getenv("ADMIN_DATABASE_URL"), reason="requires product PostgreSQL schema")
@pytest.mark.asyncio
async def test_http_parallel_conversations_cancel_one_and_replay(monkeypatch):
    from app.api import routes
    from app.main import app
    from app.services.conversation_history import ConversationRepository

    releases = {"A": asyncio.Event(), "B": asyncio.Event()}

    class SlowAgent:
        async def stream(self, query, user_id, thread_id, datasource_id, **kwargs):
            label = "A" if "任务 A" in query else "B"
            yield SSEEvent(event="TOOL_STARTED", message="tool started", data={"tool": f"tool-{label}"})
            await releases[label].wait()
            yield SSEEvent(event="TOOL_FINISHED", message="tool finished", data={"tool": f"tool-{label}"})
            yield SSEEvent(event="FINAL_ANSWER", message="finished", data={"answer": f"answer-{label}"})

    monkeypatch.setattr(routes, "agent", SlowAgent())
    repository = ConversationRepository(os.environ["ADMIN_DATABASE_URL"])
    user_id = f"runtime-http-{uuid.uuid4()}"
    conversation_a = repository.create(user_id)["id"]
    conversation_b = repository.create(user_id)["id"]
    headers = {"X-User-Id": user_id}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=20) as client:
        def submit(conversation_id: str, label: str):
            return client.post(
                f"/api/conversations/{conversation_id}/chat/stream", headers=headers,
                json={"query": f"统计任务 {label} 的数据", "thread_id": f"thread-{uuid.uuid4()}", "datasource_id": "training_db"},
            )

        request_a = asyncio.create_task(submit(conversation_a, "A"))
        request_b = asyncio.create_task(submit(conversation_b, "B"))
        for _ in range(50):
            detail_a, detail_b = repository.get(conversation_a, user_id), repository.get(conversation_b, user_id)
            if detail_a["tasks"] and detail_b["tasks"] and all(
                any(event["event_type"] == "TOOL_STARTED" for event in detail["events"])
                for detail in (detail_a, detail_b)
            ):
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("A and B did not reach concurrent running state")
        task_a = detail_a["tasks"][-1]["id"]
        task_b = detail_b["tasks"][-1]["id"]
        assert task_a != task_b
        duplicate = await submit(conversation_a, "A")
        assert duplicate.status_code == 409
        assert len(repository.get(conversation_a, user_id)["tasks"]) == 1
        cancelled = await client.post(f"/api/conversations/{conversation_a}/tasks/{task_a}/cancel", headers=headers)
        assert cancelled.status_code == 200
        assert "event: CANCELLED" in (await request_a).text
        assert repository.task_status(task_a, conversation_a) == "cancelled"
        assert repository.task_status(task_b, conversation_b) == "running"
        releases["B"].set()
        assert "event: FINAL_ANSWER" in (await request_b).text
        assert repository.task_status(task_b, conversation_b) == "completed"
        replay = await client.get(f"/api/conversations/{conversation_b}/tasks/{task_b}/events", headers=headers)
        assert replay.status_code == 200
        assert "event: TOOL_STARTED" in replay.text
        assert "event: FINAL_ANSWER" in replay.text
        assert not any(event["event_type"] == "TOOL_FINISHED" for event in repository.get(conversation_a, user_id)["events"])


@pytest.mark.skipif(not os.getenv("ADMIN_DATABASE_URL"), reason="requires product PostgreSQL schema")
@pytest.mark.asyncio
async def test_sse_client_disconnect_does_not_cancel_backend_task(monkeypatch):
    from app.api import routes
    from app.main import app
    from app.services.conversation_history import ConversationRepository

    release = asyncio.Event()

    class SlowAgent:
        async def stream(self, query, user_id, thread_id, datasource_id, **kwargs):
            yield SSEEvent(event="TOOL_STARTED", message="started", data={"tool": "slow"})
            await release.wait()
            yield SSEEvent(event="FINAL_ANSWER", message="finished", data={"answer": "persisted final"})

    monkeypatch.setattr(routes, "agent", SlowAgent())
    repository = ConversationRepository(os.environ["ADMIN_DATABASE_URL"])
    user_id = f"disconnect-http-{uuid.uuid4()}"
    conversation_id = repository.create(user_id)["id"]
    headers = {"X-User-Id": user_id}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=20) as client:
        request = asyncio.create_task(client.post(
            f"/api/conversations/{conversation_id}/chat/stream", headers=headers,
            json={"query": "统计任务 A 的数据", "thread_id": f"thread-{uuid.uuid4()}"},
        ))
        for _ in range(50):
            detail = repository.get(conversation_id, user_id)
            if detail["tasks"] and any(event["event_type"] == "TOOL_STARTED" for event in detail["events"]):
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("task did not start")
        task_id = detail["tasks"][-1]["id"]
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert repository.task_status(task_id, conversation_id) == "running"
        release.set()
        for _ in range(50):
            if repository.task_status(task_id, conversation_id) == "completed":
                break
            await asyncio.sleep(0.05)
        assert repository.task_status(task_id, conversation_id) == "completed"
        replay = await client.get(f"/api/conversations/{conversation_id}/tasks/{task_id}/events", headers=headers)
        assert "persisted final" in replay.text


@pytest.mark.skipif(not os.getenv("ADMIN_DATABASE_URL"), reason="requires product PostgreSQL schema")
@pytest.mark.asyncio
async def test_cancel_during_llm_prevents_later_tool_call(monkeypatch):
    from app.api import routes
    from app.main import app
    from app.services.conversation_history import ConversationRepository

    entered_llm = asyncio.Event()
    release_llm = asyncio.Event()

    class SlowAgent:
        async def stream(self, query, user_id, thread_id, datasource_id, **kwargs):
            entered_llm.set()
            await release_llm.wait()
            yield SSEEvent(event="TOOL_STARTED", message="should not run", data={"tool": "sql"})
            yield SSEEvent(event="FINAL_ANSWER", message="finished", data={"answer": "should not persist"})

    monkeypatch.setattr(routes, "agent", SlowAgent())
    repository = ConversationRepository(os.environ["ADMIN_DATABASE_URL"])
    user_id = f"cancel-llm-{uuid.uuid4()}"
    conversation_id = repository.create(user_id)["id"]
    headers = {"X-User-Id": user_id}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=20) as client:
        request = asyncio.create_task(client.post(
            f"/api/conversations/{conversation_id}/chat/stream", headers=headers,
            json={"query": "统计新的数据库任务", "thread_id": f"thread-{uuid.uuid4()}"},
        ))
        await asyncio.wait_for(entered_llm.wait(), 10)
        task_id = repository.get(conversation_id, user_id)["tasks"][-1]["id"]
        cancelled = await client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
        assert cancelled.status_code == 200
        release_llm.set()
        assert "event: CANCELLED" in (await request).text
        assert repository.task_status(task_id, conversation_id) == "cancelled"
        detail = repository.get(conversation_id, user_id)
        assert not any(event["event_type"] == "TOOL_STARTED" for event in detail["events"])
        assert not any(message["role"] == "assistant" for message in detail["messages"])


@pytest.mark.skipif(not os.getenv("ADMIN_DATABASE_URL"), reason="requires product PostgreSQL schema")
def test_thread_id_cannot_be_reused_across_conversations():
    from app.services.conversation_history import ActiveTaskConflict, ConversationRepository

    repository = ConversationRepository(os.environ["ADMIN_DATABASE_URL"])
    user_id = f"thread-isolation-{uuid.uuid4()}"
    first = repository.create(user_id)["id"]
    second = repository.create(user_id)["id"]
    thread_id = f"thread-{uuid.uuid4()}"
    task_id = repository.start_task(first, thread_id)
    repository.update_task(task_id, status="completed")
    with pytest.raises(ActiveTaskConflict):
        repository.start_task(second, thread_id)
