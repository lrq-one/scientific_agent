import os

import psycopg
import pytest

from app.agents.scientific_agent import ScientificAgent
from app.models.schemas import ToolResult


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL", "postgresql://agent_reader:reader_demo@127.0.0.1:55432/scientific_agent")


def postgres_available() -> bool:
    try:
        with psycopg.connect(POSTGRES_URL, connect_timeout=2) as connection:
            connection.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


@pytest.mark.asyncio
@pytest.mark.skipif(not postgres_available(), reason="PostgreSQL integration service is unavailable")
async def test_mixed_trace_uses_deepagents_mcp_text2sql_and_postgres(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    query = "比较 model_v1.csv 和 model_v2.csv，分析为什么新模型在 fused-ring 分子上误差更高，并检查是不是 training_db 训练数据覆盖不足。"
    events = [event async for event in ScientificAgent().stream(query, "e2e", "mixed-postgres", "training_db")]
    intent = next(event for event in events if event.event == "INTENT_RESOLVED")
    assert intent.data["intent"]["task_type"] == "mixed_analysis"
    assert intent.data["intent"]["complexity"] == "complex"
    assert intent.data["selected_skills"]
    assert any(event.event == "TOOL_FINISHED" and event.data.get("tool") == "deepagents_runtime" for event in events)
    assert any(event.event == "TOOL_FINISHED" and event.data.get("tool") == "get_molecule_features" for event in events)
    sql_event = next(event for event in events if event.event == "TOOL_FINISHED" and event.data.get("tool") == "text_to_sql")
    assert sql_event.data["result"]["metadata"]["generator"] in {"deterministic_fixture_fallback", "llm_structured_output"}
    db_event = next(event for event in events if event.event == "TOOL_FINISHED" and event.data.get("tool") == "execute_readonly_sql")
    assert db_event.data["result"]["metadata"]["backend"] == "postgres"
    assert events[-1].event == "FINAL_ANSWER"


@pytest.mark.asyncio
@pytest.mark.skipif(not postgres_available(), reason="PostgreSQL integration service is unavailable")
async def test_mcp_unavailable_uses_file_subgroup_as_limited_alternative(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    class UnavailableMCP:
        async def call(self, name, arguments):
            return ToolResult(success=False, source="mcp", error="MCP server unavailable")

    agent = ScientificAgent()
    agent.mcp = UnavailableMCP()

    async def scaffold(*args, **kwargs):
        return {"runtime": "controlled-test-scaffold", "model_mode": "deterministic_scaffold"}

    monkeypatch.setattr(agent.deep_runtime, "run_scaffold", scaffold)
    query = "比较 model_v1.csv 和 model_v2.csv，分析 fused-ring 结构误差，并检查 training_db 训练覆盖。"
    events = [item async for item in agent.stream(query, "mcp-failure", "mcp-failure-thread", "training_db")]
    decision = next(item for item in events if item.event == "RECOVERY_DECISION" and item.data.get("failure_kind") == "tool_unavailable")
    assert decision.data["action"] == "alternative_tool"
    assert decision.data["alternative_tool"] == "group_metrics"
    final = next(item for item in events if item.event == "FINAL_ANSWER")
    assert final.data["state"]["quality_status"] == "INSUFFICIENT_EVIDENCE"
    assert not any(item.data.get("evidence", {}).get("source_type") == "mcp" for item in events if item.event == "EVIDENCE_ADDED")


@pytest.mark.asyncio
@pytest.mark.skipif(not postgres_available(), reason="PostgreSQL integration service is unavailable")
async def test_explicit_version_mixed_task_checks_file_db_molecule_join(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    query = "比较 model_v1.csv 和 model_v2.csv 的 fused-ring 误差，并检查 training_db train_v3 训练覆盖。"
    events = [item async for item in ScientificAgent().stream(query, "e2e", "mixed-explicit-version", "training_db")]
    join = next(item for item in events if item.event == "TOOL_FINISHED" and item.data.get("tool") == "cross_resource_join")
    assert join.data["result"]["metadata"]["read_only"] is True
    assert join.data["result"]["metadata"]["params"]["dataset_version"] == "train_v3"
    final = events[-1]
    assert final.event == "FINAL_ANSWER"
    # The demo model file and demo training membership use different molecule ID
    # namespaces. An empty join is reported as insufficient, never fabricated.
    assert final.data["state"]["quality_status"] == "INSUFFICIENT_EVIDENCE"
    assert "未找到文件分子" in final.data["answer"]

