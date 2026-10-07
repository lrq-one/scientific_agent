from pathlib import Path
import sqlite3

import pytest

from app.agents.scientific_agent import ScientificAgent
from app.config import DEMO_DATA
from app.models.schemas import ResourceSummary, ScientificAgentState
from app.tools.database_tools import DatabaseService


@pytest.mark.asyncio
async def test_real_file_sql_join_tracks_unmatched_and_raw_rows(monkeypatch, tmp_path: Path):
    database_file = tmp_path / "matching.db"
    with sqlite3.connect(database_file) as db:
        db.executescript(
            "CREATE TABLE molecules (molecule_id TEXT PRIMARY KEY, structure_type TEXT);"
            "CREATE TABLE training_molecules (molecule_id TEXT, dataset_version TEXT);"
            "INSERT INTO molecules VALUES ('M001','linear'),('M002','cyclic');"
            "INSERT INTO training_molecules VALUES ('M001','train_v3'),('M002','train_v3');"
        )
    monkeypatch.setattr(
        "app.agents.scientific_agent.DatabaseService",
        lambda selected, allowed: DatabaseService(selected, allowed, db_path=database_file),
    )
    agent = ScientificAgent()
    state = ScientificAgentState(user_id="u", thread_id="join-a", goal="比较 model_v2.csv 与 train_v3")
    events = [item async for item in agent._reconcile_file_database(
        state, ResourceSummary(authorized_datasources=["training_db"]),
        DEMO_DATA / "model_v2.csv", "training_db", "train_v3",
    )]
    assert events[0].event == "TOOL_STARTED"
    assert events[1].event == "TOOL_FINISHED"
    assert events[1].data["result"]["metadata"]["read_only"] is True
    assert events[1].data["result"]["metadata"]["params"]["dataset_version"] == "train_v3"
    assert [item.event for item in events].count("EVIDENCE_ADDED") == 3
    assert state.evidence[0].value
    assert state.evidence[0].source_type == "file"
    summary = state.evidence[2].value
    assert summary["file_molecule_count"] == 8
    assert summary["matched_count"] == 2
    assert len(summary["unmatched_molecule_ids"]) == 6
    assert state.evidence[1].tool_call_id == state.evidence[2].tool_call_id


@pytest.mark.asyncio
async def test_successful_cross_resource_claim_links_both_sources(monkeypatch, tmp_path: Path):
    database_file = tmp_path / "matching.db"
    file_path = tmp_path / "model.csv"
    file_path.write_text("molecule_id,structure_type\nM001,linear\nM002,cyclic\n", encoding="utf-8")
    with sqlite3.connect(database_file) as db:
        db.executescript(
            "CREATE TABLE molecules (molecule_id TEXT PRIMARY KEY, structure_type TEXT);"
            "CREATE TABLE training_molecules (molecule_id TEXT, dataset_version TEXT);"
            "INSERT INTO molecules VALUES ('M001','linear'),('M002','cyclic');"
            "INSERT INTO training_molecules VALUES ('M001','train_v3'),('M002','train_v3');"
        )
    monkeypatch.setattr(
        "app.agents.scientific_agent.DatabaseService",
        lambda selected, allowed: DatabaseService(selected, allowed, db_path=database_file),
    )
    agent = ScientificAgent()
    state = ScientificAgentState(user_id="u", thread_id="join-good", goal="核对文件与 train_v3")
    events = [item async for item in agent._reconcile_file_database(
        state, ResourceSummary(authorized_datasources=["training_db"]), file_path, "training_db", "train_v3",
    )]
    assert [item.event for item in events].count("EVIDENCE_ADDED") == 3
    agent._finalize(state)
    assert state.quality_status == "SUPPORTED_CONCLUSION"
    joined_claim = next(claim for claim in state.claims if "按 molecule_id 关联核对" in claim.text)
    assert joined_claim.evidence_ids == [item.evidence_id for item in state.evidence]


@pytest.mark.asyncio
async def test_demo_file_and_training_fixture_do_not_false_join(monkeypatch):
    monkeypatch.setattr(
        "app.agents.scientific_agent.DatabaseService",
        lambda selected, allowed: DatabaseService(selected, allowed, db_path=DEMO_DATA / "training_demo.db"),
    )
    agent = ScientificAgent()
    state = ScientificAgentState(user_id="u", thread_id="join-demo", goal="compare")
    events = [item async for item in agent._reconcile_file_database(
        state, ResourceSummary(authorized_datasources=["training_db"]),
        DEMO_DATA / "model_v2.csv", "training_db", "train_v3",
    )]
    assert events[1].event == "TOOL_FINISHED"
    assert events[1].data["result"]["data"] == []
    assert len(state.evidence) == 1
    assert state.evidence[0].source_type == "file"
    assert "未找到" in state.uncertainties[0]


@pytest.mark.asyncio
async def test_cross_resource_join_rejects_missing_key_and_unauthorized_source(monkeypatch, tmp_path: Path):
    bad_file = tmp_path / "bad.csv"
    bad_file.write_text("sample,structure_type\nM001,linear\n", encoding="utf-8")
    agent = ScientificAgent()
    state = ScientificAgentState(user_id="u", thread_id="join-b", goal="compare")
    events = [item async for item in agent._reconcile_file_database(
        state, ResourceSummary(authorized_datasources=["training_db"]), bad_file, "training_db", "train_v3",
    )]
    assert events == []
    assert state.evidence == []
    assert "molecule_id" in state.uncertainties[0]

    other = ScientificAgentState(user_id="u", thread_id="join-c", goal="compare")
    denied = [item async for item in agent._reconcile_file_database(
        other, ResourceSummary(authorized_datasources=[]), DEMO_DATA / "model_v2.csv", "private_db", "train_v3",
    )]
    assert denied[-1].data["result"]["success"] is False
    assert other.evidence == []
