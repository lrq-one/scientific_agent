import sqlite3

import pytest

from app.agents.scientific_agent import ScientificAgent
from app.models.schemas import ResourceSummary
from app.services.cross_dataset import compare_rows
from app.services.skills import SkillService
from app.tools.database_tools import DatabaseService


def test_skill_routing_negative_and_composition():
    service = SkillService()
    selected = service.select("比较 training_db 中 train_v2 和 train_v3 的结构类型与训练覆盖", "database_analysis")
    assert "cross_dataset_comparison" in selected
    assert "training_coverage_analysis" in selected
    assert "cross_dataset_comparison" not in service.select("统计 training_db 中 train_v3 的训练覆盖", "database_analysis")
    assert "cross_dataset_comparison" not in service.select("比较 model_v1.csv 和 model_v2.csv", "file_analysis")


def test_partial_version_is_not_zero_coverage():
    comparison, issues = compare_rows(
        [{"dataset_version": "train_v3", "structure_type": "fused_ring", "sample_count": 3}],
        ("train_v2", "train_v3"),
    )
    assert comparison == []
    assert "没有返回记录" in issues[0]


@pytest.fixture
def database_file(tmp_path):
    path = tmp_path / "versions.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE molecules (molecule_id TEXT PRIMARY KEY, structure_type TEXT NOT NULL);"
            "CREATE TABLE training_molecules (molecule_id TEXT, dataset_version TEXT, is_cyclic INTEGER);"
            "INSERT INTO molecules VALUES ('M1', 'fused_ring'), ('M2', 'linear'), ('M3', 'fused_ring');"
            "INSERT INTO training_molecules VALUES ('M1', 'train_v2', 1), ('M2', 'train_v2', 0),"
            " ('M1', 'train_v3', 1), ('M2', 'train_v3', 0), ('M3', 'train_v3', 1);"
        )
    return path


def make_agent(monkeypatch, database_file, *, authorized=True):
    agent = ScientificAgent()
    monkeypatch.setattr(
        agent.resources, "discover",
        lambda user_id, thread_id: ResourceSummary(
            authorized_datasources=["training_db"] if authorized else []
        ),
    )

    async def deterministic_route(query, resources):
        return agent.router.route(query, resources)

    monkeypatch.setattr(agent.router, "route_async", deterministic_route)
    monkeypatch.setattr(
        "app.agents.scientific_agent.DatabaseService",
        lambda selected, allowed: DatabaseService(selected, allowed, db_path=database_file),
    )
    return agent


@pytest.mark.asyncio
async def test_cross_dataset_real_guarded_read_and_evidence(monkeypatch, database_file):
    agent = make_agent(monkeypatch, database_file)
    events = [item async for item in agent.stream(
        "比较 training_db 中 train_v2 和 train_v3 的结构类型与训练覆盖", "u", "cross-normal"
    )]
    assert events[-1].event == "FINAL_ANSWER"
    state = events[-1].data["state"]
    assert state["quality_status"] == "SUPPORTED_CONCLUSION"
    assert [call["tool"] for call in state["tool_calls"]] == [
        "get_table_schema", "query_checker", "execute_readonly_sql"
    ]
    assert len(state["evidence"]) == 2
    assert any(row["structure_type"] == "fused_ring" and row["delta_second_minus_first"] == 1
               for row in state["evidence"][1]["value"])
    assert any(row["structure_type"] == "fused_ring" and row["train_v3_share"] == 0.666667
               for row in state["evidence"][1]["value"])
    assert all(claim["evidence_ids"] for claim in state["claims"])
    sql_event = next(item for item in events if item.event == "TOOL_FINISHED" and item.data["tool"] == "execute_readonly_sql")
    assert sql_event.data["result"]["metadata"]["read_only"] is True
    assert sql_event.data["result"]["metadata"]["params"] == {"version_a": "train_v2", "version_b": "train_v3"}
    assert any(item.event == "SKILL_CAPABILITY_CHECK" for item in events)


@pytest.mark.asyncio
async def test_cross_dataset_missing_version_is_insufficient(monkeypatch, database_file):
    agent = make_agent(monkeypatch, database_file)
    events = [item async for item in agent.stream(
        "比较 training_db 中 train_v1 和 train_v3 的结构类型", "u", "cross-missing"
    )]
    state = events[-1].data["state"]
    assert state["quality_status"] == "INSUFFICIENT_EVIDENCE"
    assert "train_v1" in events[-1].data["answer"]
    assert len(state["evidence"]) == 1
    assert not state["claims"]


@pytest.mark.asyncio
async def test_cross_dataset_permission_and_tool_failure(monkeypatch, database_file):
    agent = make_agent(monkeypatch, database_file)
    unauthorized = [item async for item in agent.stream(
        "比较 training_db 中 train_v2 和 train_v3 的结构类型", "u", "cross-auth", datasource_id="private_db"
    )]
    assert unauthorized[-1].data["state"]["quality_status"] == "EXECUTION_FAILED"
    assert all(item.event != "EVIDENCE_ADDED" for item in unauthorized)

    def broken_execute(self, sql, params=None):
        raise TimeoutError("controlled read timeout")

    monkeypatch.setattr(DatabaseService, "execute", broken_execute)
    failed = [item async for item in agent.stream(
        "比较 training_db 中 train_v2 和 train_v3 的结构类型", "u", "cross-timeout"
    )]
    assert failed[-1].data["state"]["quality_status"] == "EXECUTION_FAILED"
    assert any(item.event == "RECOVERY_DECISION" for item in failed)
    assert all(item.event != "EVIDENCE_ADDED" for item in failed)


@pytest.mark.asyncio
async def test_cross_dataset_missing_schema_stops_before_query(monkeypatch, database_file):
    with sqlite3.connect(database_file) as db:
        db.execute("DROP TABLE molecules")
    agent = make_agent(monkeypatch, database_file)
    events = [item async for item in agent.stream(
        "比较 training_db 中 train_v2 和 train_v3 的结构类型", "u", "cross-schema"
    )]
    state = events[-1].data["state"]
    assert state["quality_status"] == "NO_DATA"
    assert "缺少" in events[-1].data["answer"]
    assert [call["tool"] for call in state["tool_calls"]] == ["get_table_schema"]


@pytest.mark.asyncio
async def test_cross_dataset_unsupported_dimension_is_not_claimed(monkeypatch, database_file):
    agent = make_agent(monkeypatch, database_file)
    events = [item async for item in agent.stream(
        "比较 training_db 中 train_v2 和 train_v3 的结构类型和数据质量", "u", "cross-quality"
    )]
    state = events[-1].data["state"]
    assert state["quality_status"] == "INSUFFICIENT_EVIDENCE"
    assert "数据质量" in events[-1].data["answer"]
    assert state["claims"] == []
