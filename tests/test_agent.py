import pytest

from app.agents.scientific_agent import ScientificAgent, ToolLimitExceeded
from app.models.schemas import ResourceSummary, ScientificAgentState, ToolResult


@pytest.mark.asyncio
async def test_simple_file_route_runs():
    events = [event async for event in ScientificAgent().stream("model_v1.csv 有多少行？", "tester", "simple-file")]
    assert events[-1].event == "FINAL_ANSWER"
    assert any(event.event == "EVIDENCE_ADDED" and event.data["evidence"]["value"] == 8 for event in events)


@pytest.mark.asyncio
async def test_mixed_end_to_end():
    query = "比较 model_v1.csv 和 model_v2.csv，分析为什么新模型在 fused-ring 分子上误差更高，并检查是不是 training_db 训练数据覆盖不足。"
    agent = ScientificAgent()
    agent.resources.discover = lambda user_id, thread_id: ResourceSummary(
        available_files=["model_v1.csv", "model_v2.csv"],
        authorized_datasources=["training_db"],
        available_mcp_tools=["get_molecule_features"],
    )
    events = [event async for event in agent.stream(query, "tester", "mixed")]
    names = [event.event for event in events]
    assert "PLAN_CREATED" in names
    assert names.count("TOOL_STARTED") >= 5
    assert any(event.event == "EVIDENCE_ADDED" and "覆盖数" in event.data["evidence"]["claim"] for event in events)
    final = events[-1]
    assert final.event == "FINAL_ANSWER"
    assert "## 分析结论" in final.data["answer"]
    assert "**证据**" in final.data["answer"]
    assert "synthetic" in final.data["answer"]


def test_tool_call_limit(monkeypatch):
    monkeypatch.setattr("app.agents.scientific_agent.MAX_TOOL_CALLS", 1)
    agent = ScientificAgent()
    state = ScientificAgentState(user_id="u", thread_id="t", goal="g")
    result = ToolResult(success=True, source="test")
    agent._record_tool(state, "one", result)
    with pytest.raises(ToolLimitExceeded):
        agent._record_tool(state, "two", result)


def test_evidence_construction():
    agent = ScientificAgent()
    state = ScientificAgentState(user_id="u", thread_id="t", goal="g")
    evidence = agent._add_evidence(state, "MAE", 1.25, "file", "x.csv", "tool-1", "v1")
    assert evidence.value == 1.25
    assert evidence.tool_call_id == "tool-1"
    assert state.evidence == [evidence]


@pytest.mark.asyncio
async def test_hitl_interrupt_and_resume():
    agent = ScientificAgent()
    first = [event async for event in agent.stream("分析 missing.csv", "tester", "hitl")]
    assert first[-1].event == "WAITING_FOR_USER"
    assert "hitl" in agent.pending
    resumed = [event async for event in agent.resume("hitl", "稍后上传")]
    assert resumed[-1].event == "WAITING_FOR_USER"


@pytest.mark.asyncio
async def test_explicit_dataset_version_does_not_trigger_hitl(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    agent = ScientificAgent()
    agent.resources.discover = lambda user_id, thread_id: ResourceSummary(
        authorized_datasources=["training_db"],
        available_mcp_tools=["get_molecule_features"],
    )
    events = [item async for item in agent.stream(
        "统计 training_db 中 train_v3 的结构类型训练覆盖", "tester", "explicit-version-no-hitl"
    )]
    assert not any(item.event == "WAITING_FOR_USER" for item in events)
    sql = next(item for item in events if item.event == "TOOL_FINISHED" and item.data.get("tool") == "execute_readonly_sql")
    assert sql.data["result"]["metadata"]["params"]["dataset_version"] == "train_v3"

