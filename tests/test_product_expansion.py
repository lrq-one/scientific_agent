from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.datasources.postgres import PostgresDatasource, PostgresSchemaInspector
from app.services.artifacts import ArtifactService
from app.services.conversation_history import ConversationNotFound, ConversationRepository, deterministic_title
from app.services.llm_config import llm_settings
from app.services.object_storage import ObjectStorageService
from app.services.skills import SkillService
from app.services.text2sql import SchemaRetriever
from app.services.workspace import WorkspaceService
from app.tools.registry import ToolRegistry


def test_skill_catalog_preserves_core_and_v2_business_workflows():
    skills = SkillService().load()
    names = {skill["name"] for skill in skills}
    expected = {
        "model_comparison",
        "mass_spec_error_analysis",
        "training_coverage_analysis",
        "dataset_quality_audit",
        "structure_subgroup_analysis",
        "model_regression_diagnosis",
        "rt_prediction_review",
        "experiment_reproducibility_check",
        "scientific_result_summary",
        "cross_dataset_comparison",
        "model_data_consistency_check",
        "distribution_shift_analysis",
        "experiment_run_diagnosis",
        "annotation_candidate_analysis",
        "spectrum_quality_analysis",
    }
    assert expected <= names
    required_sections = [
        "# Purpose", "# When to Use", "# When NOT to Use", "# Preconditions",
        "# Required Inputs", "# Procedure", "# Tool Policy", "# Decision Rules",
        "# Evidence Requirements", "# HITL Conditions", "# Failure / Recovery",
        "# Stop Conditions", "# Output Contract", "# Example Tasks",
    ]
    for skill in skills:
        assert all(section in skill["instructions"] for section in required_sections)


def test_tool_registry_metadata_and_deterministic_filters():
    registry = ToolRegistry()
    assert len(registry.all()) == 23
    for spec in registry.all():
        payload = spec.model_dump()
        assert {"name", "description", "input_schema", "output_contract", "required_capability", "risk_level", "timeout_seconds", "side_effect", "allowed_roles"} <= payload.keys()
    candidates = registry.candidates(
        available_capabilities={"file"},
        role="researcher",
        selected_skills=["model_comparison"],
        current_step_tools=["calculate_metrics"],
    )
    assert [item.name for item in candidates] == ["calculate_metrics"]
    assert all(item.required_capability != "database" for item in candidates)
    assert registry.candidates(available_capabilities={"file"}, role="guest", selected_skills=["model_comparison"]) == []


def test_schema_retriever_returns_columns_relationships_and_scores():
    schema = {
        "predictions": [{"name": "model_run_id", "description": "run key", "table_description": "model predictions"}, {"name": "absolute_error", "description": "prediction error"}],
        "datasets": [{"name": "name", "description": "dataset name", "table_description": "dataset catalog"}],
    }
    relationships = [{"source_table": "predictions", "source_column": "model_run_id", "target_table": "model_runs", "target_column": "id"}]
    hits = SchemaRetriever(top_k=2).search("largest prediction error by model run", schema, relationships)
    assert hits[0]["table"] == "predictions"
    assert hits[0]["columns"] == schema["predictions"]
    assert hits[0]["relationships"] == relationships
    assert isinstance(hits[0]["score"], float)


class FakeMinio:
    def __init__(self):
        self.objects = {}

    def bucket_exists(self, bucket):
        return True

    def put_object(self, bucket, key, stream, size, content_type=None):
        self.objects[(bucket, key)] = (stream.read(size), content_type)


def test_artifact_tools_create_real_png_and_csv(tmp_path: Path):
    client = FakeMinio()
    storage = ObjectStorageService(client=client, repository=object(), bucket="artifacts", workspace=WorkspaceService(tmp_path))
    service = ArtifactService(storage)
    chart = service.plot_metric_comparison("u", "t", {"v1": {"mae": 0.5, "rmse": 0.7}, "v2": {"mae": 0.4, "rmse": 0.6}})
    table = service.save_result_table("u", "t", [{"model": "v1", "mae": 0.5}], output_format="csv")
    png = client.objects[("artifacts", chart["object_key"])][0]
    csv = client.objects[("artifacts", table["object_key"])][0]
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"model,mae" in csv


def test_artifact_values_xlsx_isolation_and_empty_policy(tmp_path: Path):
    import pandas as pd

    client = FakeMinio()
    storage = ObjectStorageService(client=client, repository=object(), bucket="artifacts", workspace=WorkspaceService(tmp_path))
    service = ArtifactService(storage)
    rows = [{"model": "v1", "mae": 0.5, "rmse": 0.7}, {"model": "v2", "mae": 0.4, "rmse": 0.6}]
    first = service.save_result_table("u", "task-a", rows, "metrics.xlsx", "xlsx", {"model", "mae", "rmse"})
    second = service.save_result_table("u", "task-b", rows, "metrics.csv", "csv", {"model", "mae", "rmse"})
    xlsx_bytes = client.objects[("artifacts", first["object_key"])][0]
    csv_bytes = client.objects[("artifacts", second["object_key"])][0]
    assert first["object_key"] != second["object_key"]
    assert first["object_key"].startswith("u/task-a/")
    assert second["object_key"].startswith("u/task-b/")
    assert pd.read_excel(BytesIO(xlsx_bytes)).to_dict("records") == rows
    assert pd.read_csv(BytesIO(csv_bytes)).to_dict("records") == rows
    with pytest.raises(ValueError, match="without rows"):
        service.save_result_table("u", "task-a", [], "empty.csv", "csv")
    with pytest.raises(ValueError, match="required columns"):
        service.save_result_table("u", "task-a", [{"model": "v1"}], "partial.csv", "csv", {"model", "mae"})


def test_deterministic_conversation_title():
    assert deterministic_title("  compare   two models  ") == "compare two models"
    assert deterministic_title("x" * 80).endswith("…")


def test_preferred_llm_environment_aliases(monkeypatch):
    monkeypatch.setenv("LLM_API_BASE", "https://llm.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-secret-not-real")
    monkeypatch.setenv("LLM_MODEL", "qwen3.7-flash")
    monkeypatch.setenv("OPENAI_API_BASE", "https://legacy.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "legacy")
    monkeypatch.setenv("LLM_MODEL_NAME", "legacy-model")
    settings = llm_settings()
    assert settings.api_base == "https://llm.example/v1"
    assert settings.api_key == "test-secret-not-real"
    assert settings.model == "qwen3.7-flash"
    assert settings.source == {"api_base": "LLM_API_BASE", "api_key": "LLM_API_KEY", "model": "LLM_MODEL"}


POSTGRES_URL = os.getenv("ADMIN_DATABASE_URL")


@pytest.mark.skipif(not POSTGRES_URL, reason="requires product PostgreSQL schema")
def test_conversation_message_persistence_reload_and_isolation():
    first = ConversationRepository(POSTGRES_URL)
    conversation = first.create("history-user")
    first.set_first_query_title(conversation["id"], "比较两个模型的表现")
    task_id = first.start_task(conversation["id"], f"thread-{conversation['id']}")
    first.add_message(conversation["id"], "user", "question", task_id)
    first.add_message(conversation["id"], "assistant", "answer", task_id)
    first.add_event(task_id, "FINAL_ANSWER", {"answer": "answer"})
    first.update_task(task_id, status="completed", intent={"task_type": "file_analysis"}, selected_skills=["model_comparison"])

    reloaded = ConversationRepository(POSTGRES_URL)
    assert [item["content"] for item in reloaded.messages(conversation["id"], "history-user")] == ["question", "answer"]
    detail = reloaded.get(conversation["id"], "history-user")
    assert [item["content"] for item in detail["messages"]] == ["question", "answer"]
    assert detail["tasks"][0]["thread_id"].startswith("thread-")
    assert detail["events"][0]["event_type"] == "FINAL_ANSWER"
    assert detail["id"] != detail["tasks"][0]["thread_id"]
    with pytest.raises(ConversationNotFound):
        reloaded.get(conversation["id"], "other-user")
    second = reloaded.create("history-user")
    assert second["id"] != conversation["id"]
    assert len(reloaded.list("history-user")) >= 2


@pytest.mark.skipif(not POSTGRES_URL, reason="requires product PostgreSQL schema")
def test_expanded_postgres_schema_relationships():
    inspector = PostgresSchemaInspector(PostgresDatasource("training_db", POSTGRES_URL))
    schema = inspector.tables()
    expected = {"datasets", "dataset_versions", "molecules", "molecular_features", "experiments", "model_versions", "model_runs", "predictions", "training_memberships", "retention_time_measurements", "msms_spectra", "annotations"}
    assert expected <= set(schema)
    relationships = inspector.relationships()
    assert any(row["source_table"] == "dataset_versions" and row["target_table"] == "datasets" for row in relationships)
    assert any(row["source_table"] == "predictions" and row["target_table"] == "model_runs" for row in relationships)


@pytest.mark.skipif(not POSTGRES_URL, reason="requires product PostgreSQL schema")
def test_conversation_api_crud_and_user_isolation():
    from app.main import app

    headers = {"X-User-Id": "api-history-user"}
    with TestClient(app) as client:
        created = client.post("/api/conversations", headers=headers, json={"title": "first"})
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        assert client.get(f"/api/conversations/{conversation_id}", headers=headers).status_code == 200
        assert client.get(f"/api/conversations/{conversation_id}/messages", headers=headers).json() == {"items": []}
        renamed = client.patch(f"/api/conversations/{conversation_id}", headers=headers, json={"title": "renamed"})
        assert renamed.json()["title"] == "renamed"
        assert client.get(f"/api/conversations/{conversation_id}", headers={"X-User-Id": "other"}).status_code == 404
        assert client.delete(f"/api/conversations/{conversation_id}", headers=headers).status_code == 204
        assert client.get(f"/api/conversations/{conversation_id}", headers=headers).status_code == 404


@pytest.mark.skipif(not POSTGRES_URL, reason="requires product PostgreSQL schema")
def test_conversation_stream_persists_user_and_assistant_messages():
    from app.main import app

    headers = {"X-User-Id": "stream-history-user"}
    with TestClient(app) as client:
        created = client.post("/api/conversations", headers=headers, json={"title": "新建科研任务"}).json()
        response = client.post(
            f"/api/conversations/{created['id']}/chat/stream",
            headers=headers,
            json={"query": "model_v1.csv 有多少行？", "thread_id": f"stream-{created['id']}", "datasource_id": None},
        )
        assert response.status_code == 200
        assert "event: FINAL_ANSWER" in response.text
        persisted = client.get(f"/api/conversations/{created['id']}/messages", headers=headers).json()["items"]
        assert [item["role"] for item in persisted] == ["user", "assistant"]
        assert persisted[1]["task_id"]
        detail = client.get(f"/api/conversations/{created['id']}", headers=headers).json()
        assert detail["title"] == "model_v1.csv 有多少行？"
        assert detail["tasks"][0]["status"] == "completed"
        assert any(item["event_type"] == "TOOL_CANDIDATES" for item in detail["events"])


@pytest.mark.skipif(not POSTGRES_URL, reason="requires product PostgreSQL schema")
def test_persisted_db_hitl_resume_keeps_conversation_task_and_thread_ids(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Exercise the actual configured reader; an unconfigured datasource must
    # not be advertised or implicitly granted by the authorization policy.
    monkeypatch.setenv("DATABASE_URL", os.getenv("TEST_POSTGRES_URL", "postgresql://agent_reader:reader_demo@127.0.0.1:55432/scientific_agent"))
    from app.main import app

    headers = {"X-User-Id": f"hitl-regression-{os.getpid()}"}
    thread_id = f"hitl-api-{os.getpid()}"
    with TestClient(app) as client:
        conversation_id = client.post(
            "/api/conversations", headers=headers, json={"title": "HITL regression"}
        ).json()["id"]
        first = client.post(
            f"/api/conversations/{conversation_id}/chat/stream",
            headers=headers,
            json={
                "query": "分析 training_db 的训练覆盖情况",
                "thread_id": thread_id,
                "datasource_id": "training_db",
            },
        )
        waiting_payload = next(
            json.loads(line[6:])
            for line in first.text.splitlines()
            if line.startswith("data: ") and '"task_id"' in line and '"field": "dataset_version"' in line
        )
        task_id = waiting_payload["task_id"]
        before = client.get(f"/api/conversations/{conversation_id}", headers=headers).json()
        task_before = next(item for item in before["tasks"] if item["id"] == task_id)
        assert task_before["conversation_id"] == conversation_id
        assert task_before["thread_id"] == thread_id
        assert task_before["status"] == "waiting_for_user"

        resumed = client.post(
            "/api/agent/resume",
            headers=headers,
            json={
                "thread_id": thread_id,
                "answer": "train_v3",
                "conversation_id": conversation_id,
                "task_id": task_id,
            },
        )
        assert resumed.status_code == 200
        assert "event: FINAL_ANSWER" in resumed.text
        assert "检查点恢复失败" not in resumed.text

        after = client.get(f"/api/conversations/{conversation_id}", headers=headers).json()
        task_after = next(item for item in after["tasks"] if item["id"] == task_id)
        assert task_after["conversation_id"] == conversation_id
        assert task_after["thread_id"] == thread_id
        assert task_after["status"] == "completed"
        assert any(item["task_id"] == task_id for item in after["evidence"])
        messages = client.get(f"/api/conversations/{conversation_id}/messages", headers=headers).json()["items"]
        assert messages[-1]["role"] == "assistant"
        assert messages[-1]["task_id"] == task_id
        assert messages[-1]["content"] != "structure_type"
