import uuid

import pytest

from app.agents.scientific_agent import ScientificAgent
from app.models.schemas import ResourceSummary, ScientificAgentState, ToolResult


@pytest.mark.asyncio
async def test_dataset_version_interrupt_and_command_resume(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    thread_id = f"hitl-{uuid.uuid4()}"
    agent = ScientificAgent()
    agent.resources.discover = lambda user_id, task_thread_id: ResourceSummary(
        authorized_datasources=["training_db"],
        available_mcp_tools=["get_molecule_features"],
    )
    first = [event async for event in agent.stream("分析 fused-ring 在训练数据中的覆盖情况。", "tester", thread_id)]
    assert first[-1].event == "WAITING_FOR_USER"
    assert first[-1].data["field"] == "dataset_version"

    resumed = [event async for event in agent.resume(thread_id, "train_v3", "tester")]
    assert resumed[0].event == "PLAN_STEP_STARTED"
    assert resumed[0].data["dataset_version"] == "train_v3"
    assert resumed[-1].event == "FINAL_ANSWER"
    assert resumed[-1].data["state"]["thread_id"] == thread_id
    assert any(event.event == "EVIDENCE_ADDED" for event in resumed)


def test_multicolumn_sql_rows_are_preserved_as_evidence_and_finalized():
    agent = ScientificAgent()
    state = ScientificAgentState(
        user_id="tester",
        thread_id="multi-column",
        goal="分析含环分子的预测误差",
        task_type="database_analysis",
    )
    rows = [
        {"is_fused_ring": False, "train_molecule_count": 4, "avg_absolute_error": 0.1967},
        {"is_fused_ring": True, "molecule_count": 2, "avg_absolute_error": 0.4200},
    ]

    evidence = agent._database_evidence(
        state,
        ToolResult(success=True, data=rows, source="training_db"),
        "training_db",
        "tool-1",
        "train_v3",
    )

    assert evidence[0].value == rows
    assert evidence[1].value == 2
    answer = agent._finalize(state)
    assert "avg_absolute_error" in answer
    assert answer != "structure_type"

