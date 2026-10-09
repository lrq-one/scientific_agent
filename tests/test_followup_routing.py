from __future__ import annotations

import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.agents.scientific_agent import ScientificAgent
from app.models.schemas import ScientificAgentState, SSEEvent, ToolResult, FollowUpDecision, TaskRefinementPatch
from conftest import StructuredDecisionStub
from app.services.followup import (
    ConversationContextResolver,
    build_provenance,
    conversation_context_summary,
    parse_refinement_patch,
    provenance_answer,
    render_persisted_table,
    workflow_query,
)


def _context() -> dict:
    return {
        "task": {
            "id": "task-original",
            "conversation_id": "conversation-1",
            "thread_id": "thread-1",
            "intent_json": {"task_type": "database_analysis"},
            "selected_skills_json": ["training_coverage_analysis"],
        },
        "events": [],
        "evidence": [],
        "artifacts": [],
        "assistant_message": {"content": "上一轮结果"},
    }


def test_repeated_rerun_keeps_persisted_scientific_goal():
    goal = "统计 training_db 中 train_v3 不同结构类型覆盖"
    context = _context()
    context["task"]["intent_json"]["goal"] = goal
    context["messages"] = [{"role": "user", "content": "重新回答"}]
    assert workflow_query("再重跑一次", "RERUN_PREVIOUS_TASK", context) == (goal, "train_v3")
    refined, version = workflow_query("换成 train_v2", "REFINE_PREVIOUS_TASK", context)
    assert "不同结构类型" in refined and "train_v2" in refined
    assert version == "train_v2"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("这是一个全新的科研问题", "NEW_TASK"),
        ("证据呢？", "EVIDENCE_EXPLANATION"),
        ("刚才为什么这么判断？", "RESULT_EXPLANATION"),
        ("继续分析下一步", "CONTINUE_ANALYSIS"),
        ("换成 train_v2 再分析一次", "REFINE_PREVIOUS_TASK"),
        ("重新运行一次", "RERUN_PREVIOUS_TASK"),
        ("刚才的查询为什么报错？", "ERROR_QUESTION"),
    ],
)
@pytest.mark.asyncio
async def test_structured_followup_contract(query: str, expected: str):
    semantic_type = {"EVIDENCE_EXPLANATION": "EVIDENCE_QUERY", "REFINE_PREVIOUS_TASK": "TASK_REFINEMENT",
                     "RERUN_PREVIOUS_TASK": "RERUN"}.get(expected, expected)
    model = StructuredDecisionStub([FollowUpDecision(interaction_type=semantic_type, requested_content=["evidence"])])
    result = await ConversationContextResolver(model).resolve_async(query, _context())
    assert result is not None
    assert result.follow_up_type == expected
    assert result.source == "llm_structured"
    assert query in model.prompts[0]


def test_provenance_reconstructs_persisted_sql_and_evidence():
    context = _context()
    context["events"] = [
        {
            "event_type": "TOOL_FINISHED",
            "payload_json": {
                "tool": "text_to_sql",
                "result": {
                    "success": True,
                    "source": "training_db",
                    "data": {"sql": "SELECT structure_type, count(*) FROM molecules GROUP BY 1", "params": {}},
                },
            },
        },
        {
            "event_type": "TOOL_FINISHED",
            "payload_json": {
                "tool": "execute_readonly_sql",
                "result": {
                    "success": True,
                    "source": "training_db",
                    "data": [{"structure_type": "fused_ring", "sample_count": 2}],
                    "metadata": {"params": {"dataset_version": "train_v3"}},
                },
            },
        },
    ]
    context["evidence"] = [
        {
            "id": "evidence-1",
            "claim": "训练覆盖与预测误差统计",
            "value_json": [{"structure_type": "fused_ring", "sample_count": 2}],
            "source_type": "database",
            "source": "training_db",
            "tool_call_id": "tool-5",
            "dataset_version": "train_v3",
        }
    ]
    provenance = build_provenance(context)
    answer = provenance_answer(provenance)
    assert provenance.datasource == "training_db"
    assert provenance.dataset_version == "train_v3"
    assert provenance.sql_raw_result[0]["structure_type"] == "fused_ring"
    assert "evidence-1" in answer
    assert "SELECT structure_type" in answer


def test_refine_restores_previous_goal_and_replaces_dataset_version():
    context = _context()
    context["messages"] = [
        {"role": "user", "content": "统计 training_db 中 train_v3 不同结构类型覆盖。"}
    ]
    query, version = workflow_query("换成 train_v2 再分析一次。", "REFINE_PREVIOUS_TASK", context)
    assert "不同结构类型覆盖" in query
    assert "train_v2" in query
    assert "train_v3" not in query
    assert version == "train_v2"


def test_empty_sql_result_is_insufficient_evidence():
    agent = ScientificAgent()
    state = ScientificAgentState(
        user_id="tester",
        thread_id="empty-result",
        goal="统计结构覆盖",
        task_type="database_analysis",
    )
    evidence = agent._database_evidence(
        state,
        ToolResult(success=True, data=[], source="training_db"),
        "training_db",
        "tool-1",
        "train_v3",
    )
    answer = agent._finalize(state)
    assert evidence == []
    assert answer.startswith("NO_DATA")
    assert state.quality_status == "NO_DATA"
    assert "不等价于科学上不存在" in answer


def test_nonempty_sql_rows_missing_error_field_cannot_answer_error_question():
    agent = ScientificAgent()
    state = ScientificAgentState(
        user_id="tester",
        thread_id="missing-error-field",
        goal="分析不同结构类型的预测误差和训练覆盖",
        task_type="database_analysis",
    )
    rows = [{"structure_type": "fused_ring", "sample_count": 2}]
    agent._database_evidence(
        state, ToolResult(success=True, data=rows, source="training_db"),
        "training_db", "tool-1", "train_v3",
    )
    answer = agent._finalize(state)
    assert answer.startswith("INSUFFICIENT_EVIDENCE")
    assert "缺少误差指标字段" in answer


def test_missing_fused_ring_row_does_not_become_zero_coverage():
    agent = ScientificAgent()
    state = ScientificAgentState(
        user_id="tester",
        thread_id="missing-fused-row",
        goal="检查 fused-ring 的训练覆盖",
        task_type="database_analysis",
    )
    evidence = agent._database_evidence(
        state,
        ToolResult(success=True, data=[{"structure_type": "linear", "sample_count": 8}], source="training_db"),
        "training_db", "tool-1", "train_v3",
    )
    answer = agent._finalize(state)
    assert len(evidence) == 1
    assert "覆盖数" not in evidence[0].claim
    assert answer.startswith("INSUFFICIENT_EVIDENCE")
    assert "不能据此把 fused_ring 覆盖数推断为 0" in answer


def test_error_question_reuses_persisted_error_trace_without_inventing_evidence():
    context = _context()
    context["events"] = [
        {
            "event_type": "ERROR",
            "payload_json": {"message": "任务执行失败", "error": "SQLGlot rejected a write statement"},
        }
    ]
    provenance = build_provenance(context)
    answer = provenance_answer(provenance)
    assert answer.startswith("INSUFFICIENT_EVIDENCE")
    assert "SQLGlot rejected a write statement" in answer
    assert "重新执行 SQL" in answer


def test_cancelled_task_partial_evidence_cannot_be_explained_as_final_conclusion():
    context = _context()
    context["task"]["status"] = "failed"
    context["events"] = [{"event_type": "CANCELLED", "payload_json": {"message": "用户已取消"}}]
    context["evidence"] = [{"id": "partial-evidence", "claim": "partial count", "value_json": 2,
                            "source_type": "database", "source": "training_db", "tool_call_id": "tool-1"}]
    answer = provenance_answer(build_provenance(context))
    assert answer.startswith("INSUFFICIENT_EVIDENCE")
    assert "已由用户取消" in answer


@pytest.mark.asyncio
async def test_contextual_routing_keeps_semantic_model_decision():
    resolver = ConversationContextResolver(StructuredDecisionStub([
        FollowUpDecision(interaction_type="CONTINUE_ANALYSIS"), FollowUpDecision(interaction_type="NEW_TASK"),
        FollowUpDecision(interaction_type="NEW_TASK")]))
    assert (await resolver.resolve_async("继续分析异常结构的预测误差", _context())).follow_up_type == "CONTINUE_ANALYSIS"
    assert (await resolver.resolve_async("分析异常结构的预测误差", _context())).follow_up_type == "NEW_TASK"
    assert (await resolver.resolve_async("统计 train_v2 中的含环分子", _context())).follow_up_type == "NEW_TASK"


@pytest.mark.asyncio
async def test_multiple_previous_tasks_require_explicit_target_or_clarification():
    first, second = _context(), _context()
    first["task"]["id"] = str(uuid.uuid4())
    second["task"]["id"] = str(uuid.uuid4())
    resolver = ConversationContextResolver(StructuredDecisionStub([
        FollowUpDecision(interaction_type="CLARIFY", clarification_question="请说明要引用哪一项"),
        FollowUpDecision(interaction_type="EVIDENCE_QUERY", requested_content=["evidence"]),
        FollowUpDecision(interaction_type="EVIDENCE_QUERY", target_task_id=second["task"]["id"], target_reference="EXPLICIT",
                         target_selector_type="ORDER", target_reference_text="上上轮", requested_content=["evidence"]),
    ]))
    ambiguous = await resolver.resolve_async("之前那个任务的证据呢？", first, [first, second])
    assert ambiguous.interaction_type == "CLARIFY"
    assert ambiguous.clarification_question
    assert ambiguous.target_task_id is None
    explicit = await resolver.resolve_async(f"请展示 task_id {second['task']['id']} 的证据", first, [first, second])
    assert explicit.target_task_id == second["task"]["id"]
    older = await resolver.resolve_async("上上轮的证据呢？", first, [first, second])
    assert older.target_task_id == second["task"]["id"]


def test_context_prompt_is_bounded_and_does_not_embed_full_history():
    context = _context()
    context["messages"] = [{"role": "user", "content": "x" * 5000}]
    context["assistant_message"] = {"content": "y" * 5000}
    context["evidence"] = [{"id": str(i), "claim": "z" * 500} for i in range(30)]
    summary = conversation_context_summary("q" * 5000, [context] * 100)
    assert len(summary.current_user_query) == 1000
    assert len(summary.previous_user_query) == 1000
    assert len(summary.previous_assistant_answer) == 1200
    assert len(summary.available_evidence_summary) == 5
    assert len(summary.recent_tasks) == 3


def test_refinement_patch_changes_only_explicit_fields_and_reuses_format_only_data():
    patch = parse_refinement_patch("换成 train_v2，只看 fused-ring，模型改为 model_v3")
    assert set(patch.changed_fields) == {"dataset_version", "model_version", "subgroup"}
    assert patch.dataset_version == "train_v2"
    assert patch.model_version == "model_v3"
    assert patch.subgroup == "fused_ring"
    assert parse_refinement_patch("不要画图，只输出表格").changed_fields == ["output_format"]
    context = _context()
    context["messages"] = [{"role": "user", "content": "比较 model_v1 的 train_v3 结构覆盖"}]
    query, version = workflow_query("换成 train_v2，模型改为 model_v3", "REFINE_PREVIOUS_TASK", context)
    assert "train_v3" not in query
    assert "model_v1" not in query
    assert "model_v3" in query
    assert version == "train_v2"
    context["evidence"] = [{"id": "e1", "claim": "count", "value_json": 2, "source": "training_db"}]
    context["events"] = [{"event_type": "TOOL_FINISHED", "payload_json": {
        "tool": "execute_readonly_sql", "result": {"success": True, "source": "training_db",
        "data": [{"structure_type": "fused_ring", "sample_count": 2}]}}}]
    table = render_persisted_table(build_provenance(context))
    assert "| fused_ring | 2 |" in table
    assert "未重新执行 SQL" in table


def test_evidence_quality_v2_detects_conflict_null_version_and_partial_result():
    from app.models.schemas import Evidence

    agent = ScientificAgent()
    state = ScientificAgentState(user_id="tester", thread_id="quality", goal="统计 train_v3 结构覆盖", task_type="database_analysis")
    for value in (2, 3):
        state.evidence.append(Evidence(evidence_id=f"ev-{value}", claim="覆盖", value=value,
            source_type="database", source="training_db", tool_call_id="tool-1", dataset_version="train_v3"))
    assert agent._finalize(state).startswith("CONFLICTING_EVIDENCE")
    assert state.claims == []
    state.evidence = [Evidence(evidence_id="ev-null", claim="覆盖", value=[{"structure_type": "fused_ring", "sample_count": None}],
        source_type="database", source="training_db", tool_call_id="tool-2", dataset_version="train_v2")]
    state.observations = [ToolResult(success=True, data=[{"sample_count": None}], source="training_db", metadata={"max_rows": 1})]
    answer = agent._finalize(state)
    assert answer.startswith("INSUFFICIENT_EVIDENCE")
    assert "NULL" in answer
    assert "版本" in answer
    assert "部分结果" in answer


def test_evidence_quality_v2_distinguishes_execution_failure_from_no_data():
    agent = ScientificAgent()
    state = ScientificAgentState(user_id="tester", thread_id="failed", goal="查询结构类型", task_type="database_analysis")
    state.observations = [ToolResult(success=False, source="training_db", error="permission denied")]
    answer = agent._finalize(state)
    assert state.quality_status == "EXECUTION_FAILED"
    assert answer.startswith("EXECUTION_FAILED")
    assert "permission denied" in answer


POSTGRES_URL = os.getenv("ADMIN_DATABASE_URL")


def _sse_events(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    current = None
    for line in text.splitlines():
        if line.startswith("event: "):
            current = line[7:]
        elif line.startswith("data: ") and current:
            events.append((current, json.loads(line[6:])))
            current = None
    return events


class FakeAnalysisAgent:
    def __init__(self):
        self.calls: list[str] = []
        self.dataset_versions: list[str | None] = []

    async def stream(self, query, user_id, thread_id, datasource_id=None, dataset_version=None):
        self.calls.append(query)
        self.dataset_versions.append(dataset_version)
        yield SSEEvent(
            event="INTENT_RESOLVED",
            message="intent",
            data={
                "intent": {"task_type": "database_analysis", "goal": query},
                "selected_skills": ["training_coverage_analysis"],
            },
        )
        candidate = {
            "sql": "SELECT structure_type, count(*) AS sample_count FROM molecules GROUP BY structure_type",
            "params": {"dataset_version": "train_v3" if "train_v2" not in query else "train_v2"},
        }
        yield SSEEvent(
            event="TOOL_FINISHED",
            message="sql generated",
            data={"tool": "text_to_sql", "result": {"success": True, "data": candidate, "source": "training_db"}},
        )
        rows = [
            {"structure_type": "fused_ring", "sample_count": 2},
            {"structure_type": "linear", "sample_count": 8},
        ]
        yield SSEEvent(
            event="TOOL_FINISHED",
            message="sql executed",
            data={
                "tool": "execute_readonly_sql",
                "result": {
                    "success": True,
                    "data": rows,
                    "source": "training_db",
                    "metadata": {"sql": candidate["sql"], "params": candidate["params"]},
                },
            },
        )
        yield SSEEvent(
            event="EVIDENCE_ADDED",
            message="evidence",
            data={
                "evidence": {
                    "evidence_id": "ev-1",
                    "claim": "训练覆盖与预测误差统计",
                    "value": rows,
                    "source_type": "database",
                    "source": "training_db",
                    "tool_call_id": "tool-5",
                    "dataset_version": candidate["params"]["dataset_version"],
                    "model_version": None,
                }
            },
        )
        yield SSEEvent(event="FINAL_ANSWER", message="done", data={"answer": "fused_ring 有 2 条训练记录。"})


@pytest.mark.skipif(not POSTGRES_URL, reason="requires product PostgreSQL schema")
def test_multiturn_followup_reuses_persisted_evidence_without_tools(monkeypatch, offline_followup_model):
    from app.api import routes
    from app.main import app

    fake = FakeAnalysisAgent()
    monkeypatch.setattr(routes, "agent", fake)
    offline_followup_model.decisions = [FollowUpDecision(interaction_type="NEW_TASK"),
        *[FollowUpDecision(interaction_type="EVIDENCE_QUERY", requested_content=["evidence"]) for _ in range(3)],
        FollowUpDecision(interaction_type="TASK_REFINEMENT", requested_content=["answer"],
                         refinement_patch=TaskRefinementPatch(dataset_version="train_v2", changed_fields=["dataset_version"])),
        FollowUpDecision(interaction_type="TASK_REFINEMENT", requested_content=["raw_rows"],
                         refinement_patch=TaskRefinementPatch(output_format="table", changed_fields=["output_format"])),
        FollowUpDecision(interaction_type="CLARIFY", clarification_question="请说明要引用哪一项任务"),
    ]
    headers = {"X-User-Id": f"followup-{uuid.uuid4()}"}
    thread_ids = [f"followup-thread-{uuid.uuid4()}" for _ in range(5)]
    with TestClient(app) as client:
        conversation_id = client.post(
            "/api/conversations", headers=headers, json={"title": "follow-up regression"}
        ).json()["id"]

        turn1 = client.post(
            f"/api/conversations/{conversation_id}/chat/stream",
            headers=headers,
            json={
                "query": "统计 training_db 中 train_v3 不同结构类型覆盖。",
                "thread_id": thread_ids[0],
                "datasource_id": "training_db",
            },
        )
        turn1_events = _sse_events(turn1.text)
        original_task_id = next(data["task_id"] for name, data in turn1_events if name == "FINAL_ANSWER")
        assert len(fake.calls) == 1

        for turn_index, query in enumerate(
            ("这些结果怎么得到的？", "把 fused-ring 的原始证据告诉我。", "给我你得到的这些证据"), start=1
        ):
            response = client.post(
                f"/api/conversations/{conversation_id}/chat/stream",
                headers=headers,
                json={"query": query, "thread_id": thread_ids[turn_index], "datasource_id": "training_db"},
            )
            events = _sse_events(response.text)
            names = [name for name, _ in events]
            trace = next(data for name, data in events if name == "FOLLOW_UP_TYPE")
            final = next(data for name, data in events if name == "FINAL_ANSWER")
            assert trace["follow_up_type"] == "EVIDENCE_EXPLANATION"
            assert trace["previous_task_id"] == original_task_id
            assert trace["loaded_evidence_count"] == 1
            assert trace["provenance_source"] == "postgres"
            assert trace["new_tool_calls"] == 0
            assert "TOOL_FINISHED" not in names
            assert "TOOL_STARTED" not in names
            assert "fused_ring" in final["answer"]
            assert "train_v3" in final["answer"]
            assert len(fake.calls) == 1

        rerun = client.post(
            f"/api/conversations/{conversation_id}/chat/stream",
            headers=headers,
            json={
                "query": "换成 train_v2 再分析一次。",
                "thread_id": thread_ids[4],
                "datasource_id": "training_db",
            },
        )
        rerun_events = _sse_events(rerun.text)
        assert next(data for name, data in rerun_events if name == "FOLLOW_UP_TYPE")["follow_up_type"] == "REFINE_PREVIOUS_TASK"
        assert any(name == "TOOL_FINISHED" for name, _ in rerun_events)
        assert len(fake.calls) == 2
        assert "不同结构类型覆盖" in fake.calls[-1]
        assert "train_v3" not in fake.calls[-1]
        assert fake.dataset_versions[-1] == "train_v2"

        format_turn = client.post(
            f"/api/conversations/{conversation_id}/chat/stream",
            headers=headers,
            json={"query": "不要画图，只输出表格", "thread_id": f"format-{uuid.uuid4()}", "datasource_id": "training_db"},
        )
        format_events = _sse_events(format_turn.text)
        assert next(data for name, data in format_events if name == "FOLLOW_UP_TYPE")["follow_up_type"] == "REFINE_PREVIOUS_TASK"
        assert "| fused_ring | 2 |" in next(data for name, data in format_events if name == "FINAL_ANSWER")["answer"]
        assert len(fake.calls) == 2

        ambiguous = client.post(
            f"/api/conversations/{conversation_id}/chat/stream",
            headers=headers,
            json={"query": "之前那个任务的证据呢？", "thread_id": f"ambiguous-{uuid.uuid4()}", "datasource_id": "training_db"},
        )
        ambiguous_events = _sse_events(ambiguous.text)
        assert "请说明" in next(data for name, data in ambiguous_events if name == "FINAL_ANSWER")["answer"]
        assert not any(name == "TOOL_FINISHED" for name, _ in ambiguous_events)
        assert len(fake.calls) == 2

        detail = client.get(f"/api/conversations/{conversation_id}", headers=headers).json()
        explanation_tasks = [
            task for task in detail["tasks"]
            if task["intent_json"].get("follow_up_type") == "EVIDENCE_EXPLANATION"
            and not task["intent_json"].get("clarification_question")
        ]
        assert len(explanation_tasks) == 3
        for index, task in enumerate(explanation_tasks, start=1):
            task_events = [event for event in detail["events"] if event["task_id"] == task["id"]]
            assert [event["event_type"] for event in task_events] == [
                "SECURITY_DECISION",
                "INTERACTION_RESOLVED",
                "FOLLOW_UP_TYPE",
                "PROVENANCE_LOADED",
                "FINAL_ANSWER",
            ]
            assert task["thread_id"] == thread_ids[index]
            assert task["conversation_id"] == conversation_id


@pytest.mark.skipif(not POSTGRES_URL, reason="requires product PostgreSQL schema")
def test_persisted_claim_links_resolve_to_real_evidence_ids():
    from app.services.conversation_history import ConversationRepository

    repository = ConversationRepository(POSTGRES_URL)
    user_id = f"claim-regression-{uuid.uuid4()}"
    conversation_id = repository.create(user_id)["id"]
    task_id = repository.start_task(conversation_id, f"thread-{uuid.uuid4()}")
    evidence_id = repository.add_evidence(task_id, {
        "evidence_id": "ev-1", "claim": "fused count", "value": 2,
        "source_type": "database", "source": "training_db", "tool_call_id": "tool-5",
        "dataset_version": "train_v3",
    })
    repository.add_claims(task_id, [
        {"text": "fused count = 2", "evidence_ids": ["ev-1"], "status": "supported", "category": "observation"},
        {"text": "unlinked claim", "evidence_ids": ["ev-missing"], "status": "supported", "category": "interpretation"},
    ])
    repository.update_task(task_id, status="completed")
    detail = repository.get(conversation_id, user_id)
    claims = {item["claim_text"]: item for item in detail["claims"]}
    assert claims["fused count = 2"]["evidence_ids_json"] == [evidence_id]
    assert claims["fused count = 2"]["status"] == "supported"
    assert claims["unlinked claim"]["status"] == "unsupported"
    context = repository.latest_analysis_context(conversation_id, user_id)
    assert any(item["evidence_ids_json"] == [evidence_id] for item in context["claims"])
    assert "Claim" in provenance_answer(build_provenance(context))


@pytest.mark.skipif(not POSTGRES_URL, reason="requires product PostgreSQL schema")
def test_latest_analysis_without_evidence_never_reuses_older_evidence(monkeypatch, offline_followup_model):
    from app.api import routes
    from app.main import app
    from app.services.conversation_history import ConversationRepository
    offline_followup_model.decisions = [FollowUpDecision(interaction_type="EVIDENCE_QUERY", requested_content=["evidence"])]

    repository = ConversationRepository(POSTGRES_URL)
    user_id = f"stale-evidence-{uuid.uuid4()}"
    conversation_id = repository.create(user_id)["id"]
    first_task = repository.start_task(conversation_id, f"first-{uuid.uuid4()}")
    repository.add_message(conversation_id, "user", "统计 train_v3 覆盖", first_task)
    repository.add_evidence(first_task, {"evidence_id": "ev-1", "claim": "count", "value": 2,
        "source_type": "database", "source": "training_db", "tool_call_id": "tool-1", "dataset_version": "train_v3"})
    repository.update_task(first_task, status="completed")
    second_task = repository.start_task(conversation_id, f"second-{uuid.uuid4()}")
    repository.add_message(conversation_id, "user", "分析 train_v4 覆盖", second_task)
    repository.add_message(conversation_id, "assistant", "NO_DATA", second_task)
    repository.update_task(second_task, status="completed")
    assert repository.latest_analysis_context(conversation_id, user_id)["task"]["id"] == second_task
    with TestClient(app) as client:
        response = client.post(
            f"/api/conversations/{conversation_id}/chat/stream",
            headers={"X-User-Id": user_id},
            json={"query": "这个结果的证据呢？", "thread_id": f"question-{uuid.uuid4()}"},
        )
    events = _sse_events(response.text)
    trace = next(data for name, data in events if name == "FOLLOW_UP_TYPE")
    answer = next(data for name, data in events if name == "FINAL_ANSWER")["answer"]
    assert trace["previous_task_id"] == second_task
    assert trace["loaded_evidence_count"] == 0
    assert answer.startswith("INSUFFICIENT_EVIDENCE")
    assert "count = 2" not in answer
