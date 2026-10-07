from __future__ import annotations

import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.agents import scientific_agent as agent_module
from app.agents.planning_graph import create_plan
from app.agents.scientific_agent import PlanDependencyError, ScientificAgent
from app.models.schemas import PlanStep, RequestIntent, ResourceSummary, SQLCandidate, SSEEvent, ScientificAgentState, ToolResult
from app.services.recovery import bounded_transient_retry, classify_failure
from app.tools.sql_guard import SQLGuardError


def test_failure_classification_does_not_retry_permanent_errors():
    assert classify_failure("permission denied", tool="sql").action == "fail_safely"
    assert classify_failure("column bogus does not exist", tool="sql").action == "replan"
    assert classify_failure("429 rate limit", tool="llm").action == "retry"
    assert classify_failure("0 rows", tool="sql").action == "fail_safely"
    assert classify_failure("invalid argument", tool="tool").action == "fail_safely"
    assert classify_failure("MCP server unavailable", tool="mcp:get_molecule_features").action == "alternative_tool"
    assert classify_failure(SQLGuardError("table not authorized: ['dual']"), tool="query_checker").action == "replan"


@pytest.mark.asyncio
async def test_configured_checkpoint_store_never_silently_uses_volatile_memory():
    from app.services.checkpointing import CheckpointService

    service = CheckpointService(database_url="")
    service.database_url = "configured-but-unavailable"
    with pytest.raises(RuntimeError, match="refusing volatile HITL"):
        await service.start_hitl({"query": "coverage", "thread_id": "test"})
    with pytest.raises(RuntimeError, match="unavailable"):
        service.checkpoint_exists("test")


def test_optional_deepagents_scaffold_uses_bounded_deterministic_model_by_default(monkeypatch):
    from app.agents.deep_runtime import DeepAgentRuntime, DeterministicRuntimeModel
    from app.services.checkpointing import CheckpointService

    monkeypatch.setenv("LLM_API_BASE", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "non-secret-test-placeholder")
    monkeypatch.setenv("LLM_MODEL", "qwen3.7-flash")
    monkeypatch.delenv("DEEP_RUNTIME_LLM", raising=False)
    runtime = DeepAgentRuntime(CheckpointService(database_url=""))
    assert isinstance(runtime._model(), DeterministicRuntimeModel)


@pytest.mark.asyncio
async def test_transient_retry_is_bounded_and_cancellation_propagates():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("timed out")
        return "ok"

    assert await bounded_transient_retry(operation, base_delay=0) == "ok"
    assert attempts == 2

    async def cancelled():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await bounded_transient_retry(cancelled, base_delay=0)


class FakeDatabase:
    def __init__(self, datasource_id, authorized):
        self.datasource_id = datasource_id
        self.checks = 0

    def schema(self):
        return ToolResult(success=True, source="training_db", data={"molecules": [
            {"name": "structure_type", "type": "text"}, {"name": "sample_count", "type": "integer"}]})

    def relationships(self):
        return ToolResult(success=True, source="training_db", data=[])

    def check_query(self, sql, params):
        self.checks += 1
        if "bogus" in sql:
            raise ValueError("column bogus does not exist")
        return ToolResult(success=True, source="training_db", data={"valid": True})

    def execute(self, sql, params):
        return ToolResult(success=True, source="training_db",
                          data=[{"structure_type": "fused_ring", "sample_count": 3}],
                          metadata={"sql": sql, "params": params})


class FakeGenerator:
    def __init__(self, duplicate=False):
        self.feedback = []
        self.duplicate = duplicate

    async def generate(self, **kwargs):
        self.feedback.append(kwargs.get("repair_feedback"))
        column = "bogus" if len(self.feedback) == 1 or self.duplicate else "structure_type"
        return SQLCandidate(sql=f"SELECT {column}, count(*) AS sample_count FROM molecules GROUP BY {column}",
                            params={}, reason="fake"), {"generator": "fake"}


def _state():
    return ScientificAgentState(user_id="tester", thread_id="repair", goal="统计结构覆盖",
        task_type="database_analysis", plan=[PlanStep(step_id="training-coverage", goal="统计覆盖")])


def test_plan_dependencies_control_step_execution_and_preserve_observations():
    state = ScientificAgentState(user_id="tester", thread_id="plan", goal="mixed", task_type="mixed_analysis",
        plan=[PlanStep(step_id="compare", goal="compare"),
              PlanStep(step_id="coverage", goal="coverage", depends_on=["compare"])])
    with pytest.raises(PlanDependencyError, match="blocked"):
        ScientificAgent._start_plan_step(state, "coverage")
    step = ScientificAgent._start_plan_step(state, "compare")
    step.observations.append({"tool": "calculate_metrics", "success": True})
    step.evidence_ids.append("ev-1")
    ScientificAgent._finish_plan_step(step, "done")
    assert ScientificAgent._start_plan_step(state, "coverage").status == "running"
    assert state.plan[0].observations[0]["success"] is True
    assert state.plan[0].evidence_ids == ["ev-1"]


@pytest.mark.asyncio
async def test_sql_schema_error_triggers_one_real_replan_and_grounded_final(monkeypatch):
    monkeypatch.setattr(agent_module, "DatabaseService", FakeDatabase)
    agent = ScientificAgent()
    generator = FakeGenerator()
    agent.text2sql = generator
    state = _state()
    events = [item async for item in agent._run_database_branch(
        state, ResourceSummary(authorized_datasources=["training_db"]),
        RequestIntent(goal=state.goal, task_type="database_analysis"), state.goal, "training_db", "train_v3")]
    assert [item.event for item in events].count("PLAN_REVISED") == 1
    assert len(generator.feedback) == 2
    assert "column bogus" in generator.feedback[1]
    assert state.replan_count == 1
    assert state.plan[-1].status == "completed"
    assert state.plan[0].evidence_ids
    assert agent._finalize(state).startswith("## 分析结论")
    assert state.quality_status == "SUPPORTED_CONCLUSION"


@pytest.mark.asyncio
async def test_hallucinated_dual_table_is_guarded_and_repaired_once(monkeypatch):
    class DualGuardDatabase(FakeDatabase):
        def check_query(self, sql, params):
            if "dual" in sql.lower():
                raise SQLGuardError("table not authorized: ['dual']")
            return super().check_query(sql, params)

    class DualThenValid:
        def __init__(self):
            self.calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            sql = "SELECT 1 FROM dual" if self.calls == 1 else (
                "SELECT structure_type, count(*) AS sample_count FROM molecules GROUP BY structure_type"
            )
            return SQLCandidate(sql=sql, params={}, reason="controlled"), {"generator": "controlled"}

    monkeypatch.setattr(agent_module, "DatabaseService", DualGuardDatabase)
    agent = ScientificAgent()
    generator = DualThenValid()
    agent.text2sql = generator
    state = _state()
    events = [item async for item in agent._run_database_branch(
        state, ResourceSummary(authorized_datasources=["training_db"]),
        RequestIntent(goal=state.goal, task_type="database_analysis"), state.goal, "training_db", "train_v3")]
    assert generator.calls == 2
    assert any(item.event == "PLAN_REVISED" for item in events)
    assert any(item.event == "EVIDENCE_ADDED" for item in events)
    assert agent._finalize(state).startswith("## 分析结论")


@pytest.mark.asyncio
async def test_mixed_database_step_sends_only_coverage_schema_to_sql_model(monkeypatch):
    class WideDatabase(FakeDatabase):
        def schema(self):
            return ToolResult(success=True, source="training_db", data={
                "training_molecules": [{"name": "dataset_version"}, {"name": "molecule_id"}],
                "molecules": [{"name": "molecule_id"}, {"name": "structure_type"}],
                "predictions": [{"name": "absolute_error"}],
            })

    class CaptureGenerator:
        async def generate(self, **kwargs):
            self.kwargs = kwargs
            return SQLCandidate(sql="SELECT structure_type, 3 AS sample_count FROM molecules",
                                params={}, reason="controlled"), {"generator": "controlled"}

    monkeypatch.setattr(agent_module, "DatabaseService", WideDatabase)
    agent = ScientificAgent()
    generator = CaptureGenerator()
    agent.text2sql = generator
    state = ScientificAgentState(user_id="u", thread_id="mixed-sql", goal="比较 model.csv 并检查 train_v3 训练覆盖",
                                 task_type="mixed_analysis", plan=[PlanStep(step_id="training-coverage", goal="coverage")])
    _ = [item async for item in agent._run_database_branch(
        state, ResourceSummary(authorized_datasources=["training_db"]),
        RequestIntent(goal=state.goal, task_type="mixed_analysis"), state.goal, "training_db", "train_v3")]
    assert set(generator.kwargs["full_schema"]) == {"training_molecules", "molecules"}
    assert "model.csv" not in generator.kwargs["goal"]
    assert generator.kwargs["dataset_version"] == "train_v3"


@pytest.mark.asyncio
async def test_complex_db_plan_dispatches_dependent_steps_and_rewires_after_replan(monkeypatch):
    monkeypatch.setattr(agent_module, "DatabaseService", FakeDatabase)
    agent = ScientificAgent()
    agent.text2sql = FakeGenerator()
    intent = RequestIntent(goal="比较 train_v3 各结构类型覆盖并检查差异", task_type="database_analysis",
                           complexity="complex", need_planning=True)
    graph_state = create_plan({"intent": intent.model_dump(mode="json"), "plan": [], "plan_version": 0})
    state = ScientificAgentState(user_id="tester", thread_id="complex-repair", goal=intent.goal,
        task_type="database_analysis", complexity="complex",
        plan=[PlanStep.model_validate(step) for step in graph_state["plan"]])
    events = [item async for item in agent._run_database_branch(
        state, ResourceSummary(authorized_datasources=["training_db"]), intent,
        intent.goal, "training_db", "train_v3")]
    steps = {step.step_id: step for step in state.plan}
    assert steps["schema-retrieval"].status == "completed"
    assert steps["sql-generation"].status == "failed"
    assert steps["sql-repair-1"].status == "completed"
    assert steps["sql-repair-1"].depends_on == ["schema-retrieval"]
    assert steps["sql-execution"].status == "completed"
    assert steps["sql-execution"].depends_on == ["sql-repair-1"]
    assert steps["sql-execution"].evidence_ids
    assert [item.event for item in events].count("PLAN_REVISED") == 1


@pytest.mark.skipif(not os.getenv("ADMIN_DATABASE_URL"), reason="requires product PostgreSQL schema")
def test_hitl_resume_claim_is_atomic_and_prevents_duplicate_or_expired_execution():
    from app.services.conversation_history import ConversationRepository

    repository = ConversationRepository(os.environ["ADMIN_DATABASE_URL"])
    conversation_id = repository.create(f"resume-claim-{uuid.uuid4()}")["id"]
    thread_id = f"thread-{uuid.uuid4()}"
    task_id = repository.start_task(conversation_id, thread_id)
    repository.update_task(task_id, status="waiting_for_user")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repository.claim_resume(task_id, conversation_id, thread_id), range(2)))
    assert sorted(results) == [False, True]
    assert not repository.claim_resume(task_id, conversation_id, thread_id)
    assert not repository.claim_resume(task_id, conversation_id, "wrong-thread")

    # A conversation now permits only one active task at a time.
    repository.update_task(task_id, status="completed")

    expired_thread = f"expired-{uuid.uuid4()}"
    expired_task = repository.start_task(conversation_id, expired_thread)
    repository.update_task(expired_task, status="waiting_for_user")
    with repository.connect() as connection:
        connection.execute("UPDATE tasks SET started_at = now() - interval '8 days' WHERE id = %s", (expired_task,))
    assert not repository.claim_resume(expired_task, conversation_id, expired_thread)


@pytest.mark.skipif(not os.getenv("ADMIN_DATABASE_URL"), reason="requires product PostgreSQL schema")
def test_cancelled_hitl_task_cannot_resume_via_scoped_or_legacy_api():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.conversation_history import ConversationRepository

    repository = ConversationRepository(os.environ["ADMIN_DATABASE_URL"])
    user_id = f"cancel-{uuid.uuid4()}"
    conversation_id = repository.create(user_id)["id"]
    thread_id = f"thread-{uuid.uuid4()}"
    task_id = repository.start_task(conversation_id, thread_id)
    repository.update_task(task_id, status="waiting_for_user")
    headers = {"X-User-Id": user_id}
    with TestClient(app) as client:
        cancel = client.post(f"/api/conversations/{conversation_id}/tasks/{task_id}/cancel", headers=headers)
        assert cancel.status_code == 200
        assert cancel.json()["cancelled"] is True
        assert client.post(f"/api/conversations/{conversation_id}/tasks/{task_id}/cancel", headers=headers).status_code == 409
        payload = {"thread_id": thread_id, "answer": "train_v3", "conversation_id": conversation_id, "task_id": task_id}
        assert client.post("/api/agent/resume", headers=headers, json=payload).status_code == 409
        assert client.post("/api/agent/resume", headers=headers,
                           json={"thread_id": thread_id, "answer": "train_v3"}).status_code == 400
    detail = repository.get(conversation_id, user_id)
    assert any(event["event_type"] == "CANCELLED" for event in detail["events"])


@pytest.mark.skipif(not os.getenv("ADMIN_DATABASE_URL"), reason="requires product PostgreSQL schema")
def test_active_cancellation_wins_over_terminal_answer_persistence():
    from app.services.conversation_history import ConversationRepository

    repository = ConversationRepository(os.environ["ADMIN_DATABASE_URL"])
    user_id = f"active-cancel-{uuid.uuid4()}"
    conversation_id = repository.create(user_id)["id"]
    task_id = repository.start_task(conversation_id, f"thread-{uuid.uuid4()}")
    assert repository.cancel_waiting_task(task_id, conversation_id)
    assert repository.is_cancelled(task_id)
    assert not repository.finish_task_with_answer(task_id, conversation_id, "late answer", {"answer": "late answer"})
    detail = repository.get(conversation_id, user_id)
    assert not any(message["role"] == "assistant" for message in detail["messages"])
    assert not any(event["event_type"] == "FINAL_ANSWER" for event in detail["events"])


@pytest.mark.skipif(not os.getenv("ADMIN_DATABASE_URL"), reason="requires product PostgreSQL schema")
def test_resume_tool_failure_is_persisted_and_cannot_execute_twice(monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import routes
    from app.main import app
    from app.services.conversation_history import ConversationRepository

    class FailedResume:
        def __init__(self):
            self.calls = 0

        async def resume(self, thread_id, answer, user_id):
            self.calls += 1
            yield SSEEvent(event="ERROR", message="恢复后工具失败", data={"error": "schema unavailable"})

    fake = FailedResume()
    monkeypatch.setattr(routes, "agent", fake)
    repository = ConversationRepository(os.environ["ADMIN_DATABASE_URL"])
    user_id = f"resume-fail-{uuid.uuid4()}"
    conversation_id = repository.create(user_id)["id"]
    thread_id = f"thread-{uuid.uuid4()}"
    task_id = repository.start_task(conversation_id, thread_id)
    repository.update_task(task_id, status="waiting_for_user")
    headers = {"X-User-Id": user_id}
    payload = {"thread_id": thread_id, "answer": "train_v3", "conversation_id": conversation_id, "task_id": task_id}
    with TestClient(app) as client:
        first = client.post("/api/agent/resume", headers=headers, json=payload)
        assert first.status_code == 200
        assert "event: ERROR" in first.text
        assert client.post("/api/agent/resume", headers=headers, json=payload).status_code == 409
    detail = repository.get(conversation_id, user_id)
    assert next(item for item in detail["tasks"] if item["id"] == task_id)["status"] == "failed"
    assert any(event["event_type"] == "ERROR" for event in detail["events"])
    assert fake.calls == 1


@pytest.mark.skipif(not os.getenv("ADMIN_DATABASE_URL"), reason="requires product PostgreSQL schema")
def test_resume_may_reinterrupt_and_remain_waiting(monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import routes
    from app.main import app
    from app.services.conversation_history import ConversationRepository

    class Reinterrupt:
        async def resume(self, thread_id, answer, user_id):
            yield SSEEvent(event="WAITING_FOR_USER", message="还需模型版本", data={"question": "model_version?"})

    monkeypatch.setattr(routes, "agent", Reinterrupt())
    repository = ConversationRepository(os.environ["ADMIN_DATABASE_URL"])
    user_id = f"resume-again-{uuid.uuid4()}"
    conversation_id = repository.create(user_id)["id"]
    thread_id = f"thread-{uuid.uuid4()}"
    task_id = repository.start_task(conversation_id, thread_id)
    repository.update_task(task_id, status="waiting_for_user")
    with TestClient(app) as client:
        result = client.post("/api/agent/resume", headers={"X-User-Id": user_id}, json={
            "thread_id": thread_id, "answer": "train_v3", "conversation_id": conversation_id, "task_id": task_id,
        })
    assert result.status_code == 200
    assert "event: WAITING_FOR_USER" in result.text
    detail = repository.get(conversation_id, user_id)
    assert next(item for item in detail["tasks"] if item["id"] == task_id)["status"] == "waiting_for_user"


@pytest.mark.skipif(not os.getenv("ADMIN_DATABASE_URL"), reason="requires product PostgreSQL schema")
def test_execution_failed_final_answer_persists_failed_task_status(monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import routes
    from app.main import app

    class FailedAgent:
        async def stream(self, query, user_id, thread_id, datasource_id=None, **kwargs):
            yield SSEEvent(event="FINAL_ANSWER", message="analysis failed", data={
                "answer": "EXECUTION_FAILED\n- schema unavailable",
                "state": {"quality_status": "EXECUTION_FAILED", "claims": []},
            })

    monkeypatch.setattr(routes, "agent", FailedAgent())
    headers = {"X-User-Id": f"failed-final-{uuid.uuid4()}"}
    with TestClient(app) as client:
        conversation_id = client.post("/api/conversations", headers=headers, json={"title": "failure"}).json()["id"]
        response = client.post(f"/api/conversations/{conversation_id}/chat/stream", headers=headers, json={
            "query": "分析 train_v3 覆盖", "thread_id": f"thread-{uuid.uuid4()}", "datasource_id": "training_db"})
        assert response.status_code == 200
        assert "EXECUTION_FAILED" in response.text
        detail = client.get(f"/api/conversations/{conversation_id}", headers=headers).json()
    assert detail["tasks"][-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_duplicate_repair_candidate_fails_safely_without_loop(monkeypatch):
    monkeypatch.setattr(agent_module, "DatabaseService", FakeDatabase)
    agent = ScientificAgent()
    generator = FakeGenerator(duplicate=True)
    agent.text2sql = generator
    state = _state()
    events = [item async for item in agent._run_database_branch(
        state, ResourceSummary(authorized_datasources=["training_db"]),
        RequestIntent(goal=state.goal, task_type="database_analysis"), state.goal, "training_db", "train_v3")]
    assert events[-1].event == "FINAL_ANSWER"
    assert events[-1].data["state"]["quality_status"] == "EXECUTION_FAILED"
    assert len(generator.feedback) == 2
    assert state.replan_count == 1


@pytest.mark.asyncio
async def test_empty_sql_result_fails_safely_without_broadening_query(monkeypatch):
    class EmptyDatabase(FakeDatabase):
        def execute(self, sql, params):
            return ToolResult(success=True, source="training_db", data=[], metadata={"sql": sql, "params": params})

    class ValidGenerator:
        def __init__(self):
            self.calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            return SQLCandidate(sql="SELECT structure_type, count(*) AS sample_count FROM molecules GROUP BY structure_type",
                                params={}, reason="fake"), {"generator": "fake"}

    monkeypatch.setattr(agent_module, "DatabaseService", EmptyDatabase)
    agent = ScientificAgent()
    generator = ValidGenerator()
    agent.text2sql = generator
    state = _state()
    events = [item async for item in agent._run_database_branch(
        state, ResourceSummary(authorized_datasources=["training_db"]),
        RequestIntent(goal=state.goal, task_type="database_analysis"), state.goal, "training_db", "train_v3")]
    decision = next(item for item in events if item.event == "RECOVERY_DECISION")
    assert decision.data["action"] == "fail_safely"
    assert generator.calls == 1
    assert state.replan_count == 0
    assert agent._finalize(state).startswith("NO_DATA")


@pytest.mark.asyncio
async def test_schema_unavailable_is_classified_and_terminates_without_sql(monkeypatch):
    class MissingSchema(FakeDatabase):
        def schema(self):
            raise ConnectionError("schema service unavailable")

    monkeypatch.setattr(agent_module, "DatabaseService", MissingSchema)
    agent = ScientificAgent()
    state = _state()
    events = [item async for item in agent._run_database_branch(
        state, ResourceSummary(authorized_datasources=["training_db"]),
        RequestIntent(goal=state.goal, task_type="database_analysis"), state.goal, "training_db", "train_v3")]
    assert [item.event for item in events].count("FINAL_ANSWER") == 1
    assert not any(item.event == "EVIDENCE_ADDED" for item in events)
    decision = next(item for item in events if item.event == "RECOVERY_DECISION")
    assert decision.data["failure_kind"] == "tool_unavailable"
    assert state.quality_status == "EXECUTION_FAILED"


@pytest.mark.asyncio
async def test_readonly_execution_permission_failure_is_not_retried(monkeypatch):
    class DeniedDatabase(FakeDatabase):
        def execute(self, sql, params):
            raise PermissionError("permission denied for training_molecules")

    class ValidGenerator:
        def __init__(self):
            self.calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            return SQLCandidate(sql="SELECT structure_type, count(*) AS sample_count FROM molecules GROUP BY structure_type",
                                params={}, reason="fake"), {"generator": "fake"}

    monkeypatch.setattr(agent_module, "DatabaseService", DeniedDatabase)
    agent = ScientificAgent()
    generator = ValidGenerator()
    agent.text2sql = generator
    state = _state()
    events = [item async for item in agent._run_database_branch(
        state, ResourceSummary(authorized_datasources=["training_db"]),
        RequestIntent(goal=state.goal, task_type="database_analysis"), state.goal, "training_db", "train_v3")]
    assert generator.calls == 1
    assert state.replan_count == 0
    assert state.quality_status == "EXECUTION_FAILED"
    decision = next(item for item in events if item.event == "RECOVERY_DECISION")
    assert decision.data["failure_kind"] == "permission_denied"
