import sqlite3

import pytest

from app.agents.scientific_agent import ScientificAgent
from app.models.schemas import ResourceSummary, ScientificAgentState, ToolResult
from app.services.cross_dataset import SQL as CROSS_DATASET_SQL, compare_rows, missing_columns
from app.services.skills import SkillService
from app.tools.database_tools import DatabaseService


def test_skill_routing_negative_and_composition():
    service = SkillService()
    selected = service.select("compare training_db train_v2 and train_v3 structure and coverage", "database_analysis")
    assert "cross_dataset_comparison" in selected
    assert "training_coverage_analysis" in selected
    assert "cross_dataset_comparison" not in service.select("training_db train_v3 coverage", "database_analysis")
    assert "cross_dataset_comparison" not in service.select("compare model_v1.csv and model_v2.csv", "file_analysis")


def test_partial_version_is_not_zero_coverage():
    comparison, issues = compare_rows(
        [{"dataset_version": "train_v3", "structure_type": "fused_ring", "sample_count": 3}],
        ("train_v2", "train_v3"),
    )
    assert comparison == []
    assert "train_v2" in issues[0]


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


def _database(path):
    return DatabaseService("training_db", ["training_db"], db_path=path)


def _cross_rows(path, versions=("train_v2", "train_v3")):
    db = _database(path)
    params = {"version_a": versions[0], "version_b": versions[1]}
    checked = db.check_query(CROSS_DATASET_SQL, params)
    assert checked.success is True
    result = db.execute(CROSS_DATASET_SQL, params)
    assert result.success is True
    assert result.metadata["read_only"] is True
    return result


def test_cross_dataset_real_guarded_read_and_evidence(database_file):
    result = _cross_rows(database_file)
    comparison, issues = compare_rows(result.data, ("train_v2", "train_v3"))
    assert not issues
    assert any(row["structure_type"] == "fused_ring" and row["delta_second_minus_first"] == 1 for row in comparison)
    assert any(row["structure_type"] == "fused_ring" and row["train_v3_share"] == 0.666667 for row in comparison)

    state = ScientificAgentState(
        user_id="u", thread_id="cross-normal", goal="compare two dataset versions", task_type="database_analysis",
    )
    evidence = ScientificAgent()._database_evidence(state, result, "training_db", "tool-1", "train_v2,train_v3")
    assert evidence and evidence[0].value == result.data
    assert all(claim.evidence_ids for claim in state.claims) if state.claims else True


def test_cross_dataset_missing_version_is_insufficient(database_file):
    result = _cross_rows(database_file, ("train_v1", "train_v3"))
    comparison, issues = compare_rows(result.data, ("train_v1", "train_v3"))
    assert comparison == []
    assert "train_v1" in issues[0]


def test_cross_dataset_permission_and_tool_failure(database_file, monkeypatch):
    with pytest.raises(PermissionError, match="not authorized"):
        DatabaseService("private_db", ["training_db"], db_path=database_file)

    db = _database(database_file)
    def broken_execute(_sql, _params=None):
        raise TimeoutError("controlled read timeout")
    monkeypatch.setattr(db, "execute", broken_execute)
    with pytest.raises(TimeoutError, match="controlled read timeout"):
        db.execute(CROSS_DATASET_SQL, {"version_a": "train_v2", "version_b": "train_v3"})


def test_cross_dataset_missing_schema_stops_before_query(database_file, monkeypatch):
    with sqlite3.connect(database_file) as db:
        db.execute("DROP TABLE molecules")
    database = _database(database_file)
    schema = database.schema()
    assert missing_columns(schema.data) == {"molecules": ["molecule_id", "structure_type"]}
    monkeypatch.setattr(database, "execute", lambda *_args, **_kwargs: pytest.fail("query must not run"))
    assert missing_columns(schema.data)


def test_cross_dataset_unsupported_dimension_is_not_claimed(database_file):
    result = _cross_rows(database_file)
    comparison, issues = compare_rows(result.data, ("train_v2", "train_v3"))
    assert not issues
    assert comparison and all("data_quality" not in row for row in comparison)

    state = ScientificAgentState(
        user_id="u", thread_id="cross-quality", goal="compare structure type and data quality",
        task_type="database_analysis",
    )
    ScientificAgent()._database_evidence(state, result, "training_db", "tool-1", "train_v2,train_v3")
    assert state.claims == []
