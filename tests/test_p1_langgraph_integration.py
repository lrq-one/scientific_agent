"""Real PostgreSQL/MinIO integration for the production LangGraph boundary.

This module is intentionally opt-in.  The integration profile supplies an
isolated PostgreSQL and MinIO endpoint; without those explicit variables the
module is skipped and ordinary pytest cannot contact a service.
"""
from __future__ import annotations

import shutil
import os
from pathlib import Path

import pytest
from minio import Minio

from app.agents.runtime import DecisionRuntime
from app.agents.scientific_agent import ScientificAgent
from app.models.schemas import (
    AgentDecision,
    Capability,
    CompletionCondition,
    GroundedClaim,
    GroundedResponse,
    PlanStep,
    ResourceSummary,
    SQLCandidate,
    ToolResult,
)
from app.services.checkpointing import CheckpointService
from app.services.object_storage import ObjectStorageService, PostgresFileMetadataRepository
from app.services.query_scope import UnverifiedScope, validate_scope
from app.tools.database_tools import DatabaseService
from app.tools.file_tools import FileAnalysisService
from app.services.workspace import WorkspaceService


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
ADMIN_URL = os.getenv("TEST_CHECKPOINT_URL")
MINIO_ENDPOINT = os.getenv("TEST_MINIO_ENDPOINT")
MINIO_BUCKET = os.getenv("TEST_MINIO_BUCKET")
INTEGRATION = os.getenv("SCIENTIFIC_AGENT_TEST_PROFILE") == "integration_isolated"

pytestmark = pytest.mark.skipif(
    not (INTEGRATION and POSTGRES_URL and ADMIN_URL and MINIO_ENDPOINT and MINIO_BUCKET),
    reason="requires explicit integration_isolated PostgreSQL/MinIO profile",
)


class FakeSkills:
    def __init__(self, real):
        self.real = real

    async def select_async(self, *_args, **_kwargs):
        return [], "integration_fake_llm", {"llm_called": True, "fallback": False, "model": "fake"}

    def execution_context(self, _selected):
        return ""

    def by_name(self):
        return {}


class ScriptedDecider:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    async def decide(self, _state, _tools, _skill_context):
        if not self.decisions:
            raise AssertionError("fixed integration decision script exhausted")
        return self.decisions.pop(0), {
            "llm_called": True,
            "fallback": False,
            "model": "fake-fixed-decision",
        }


class FakeResponseService:
    async def generate(self, _question, facts):
        ids = [item.get("evidence_id") for item in facts.get("evidence", []) if item.get("evidence_id")]
        claims = [GroundedClaim(text="fixed integration evidence", evidence_ids=ids[:1])] if ids else []
        return GroundedResponse(answer="isolated integration response", claims=claims), {
            "llm_called": True,
            "fallback": False,
            "model": "fake-response",
        }


def _file_plan(*, artifact=False):
    tools = ["compare_models", "save_result_table"] if artifact else ["compare_models"]
    kwargs = {
        "step_id": "1",
        "goal": "compare model files",
        "allowed_tools": tools,
        "required_capabilities": [Capability.FILE, *([Capability.ARTIFACT] if artifact else [])],
    }
    if artifact:
        kwargs["completion_predicate"] = CompletionCondition(required_tools=tools)
    return PlanStep(**kwargs)


class _RealDatabase:
    def __init__(self, selected, allowed):
        self.inner = DatabaseService(selected, allowed, database_url=POSTGRES_URL)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _resources(*, files=(), artifact=False):
    return ResourceSummary(
        available_files=list(files),
        authorized_datasources=["training_db"],
        available_artifact_formats=["csv"] if artifact else [],
        resource_metadata={
            "dataset_versions": [
                {"id": "11000000-0000-0000-0000-000000000002", "version": "train_v2", "datasource_id": "training_db"},
                {"id": "11000000-0000-0000-0000-000000000003", "version": "train_v3", "datasource_id": "training_db"},
            ]
        },
    )


def _make_agent(tmp_path: Path, resources: ResourceSummary, decisions):
    agent = ScientificAgent()
    agent.workspace = WorkspaceService(tmp_path / "workspace")
    client = Minio(MINIO_ENDPOINT, access_key="minioadmin", secret_key="change-me", secure=False)
    storage = ObjectStorageService(
        client=client,
        repository=PostgresFileMetadataRepository(ADMIN_URL),
        bucket=MINIO_BUCKET,
        workspace=agent.workspace,
    )
    storage.ensure_bucket()
    agent.storage = storage
    agent.artifact_service.storage = storage
    agent.tool_dispatcher.workspace = agent.workspace
    agent.tool_dispatcher.storage = storage
    agent.tool_dispatcher.artifacts = agent.artifact_service
    agent.tool_dispatcher.files = FileAnalysisService()
    agent.tool_dispatcher.database_factory = lambda selected, allowed: _RealDatabase(selected, allowed)
    agent.checkpointing = CheckpointService(ADMIN_URL)
    agent.resources.discover = lambda _user, _thread: resources
    agent.resources.metadata = lambda _user, _thread, value: value
    agent.skills = FakeSkills(agent.skills)
    agent.tool_registry.skills = agent.skills
    agent.runtime = DecisionRuntime(agent)
    agent.runtime.decider = ScriptedDecider(decisions)
    agent.runtime.responses = FakeResponseService()
    return agent, storage


def _copy_demo_files(agent, user_id, thread_id):
    target = agent.workspace.path_for(user_id, thread_id, create=True)
    source = Path(__file__).parents[1] / "data" / "demo"
    for filename in ("model_v1.csv", "model_v2.csv"):
        shutil.copy(source / filename, target / filename)


def _sql_candidate(scope):
    sql = (
        "SELECT m.structure_type, COUNT(*) AS sample_count "
        "FROM training_molecules AS tm JOIN molecules AS m ON m.molecule_id = tm.molecule_id "
        "WHERE tm.dataset_version = %(dataset_version)s GROUP BY m.structure_type"
    )
    schema = DatabaseService("training_db", ["training_db"], database_url=POSTGRES_URL).schema().data
    validation = validate_scope(sql, {"dataset_version": "train_v3"}, scope, schema)
    return SQLCandidate(sql=sql, params={"dataset_version": "train_v3"}, query_scope=scope), validation


@pytest.mark.asyncio
async def test_real_langgraph_A_file_goal_and_D06_minio_artifact(tmp_path):
    agent_a, _ = _make_agent(
        tmp_path / "a",
        _resources(files=("model_v1.csv", "model_v2.csv")),
        [
            AgentDecision(action="REPLAN", plan=[_file_plan()], reason_summary="file-only plan"),
            AgentDecision(action="FINISH", reason_summary="file-only complete"),
        ],
    )
    try:
        _copy_demo_files(agent_a, "integration", "A-only")
        events_a = [item async for item in agent_a.stream("compare model_v1.csv and model_v2.csv MAE", "integration", "A-only")]
        assert events_a[-1].event == "FINAL_ANSWER"
        state_a = events_a[-1].data["state"]
        assert [call["tool"] for call in state_a["tool_calls"]] == ["compare_models"]
        assert state_a["goal_coverage"]["status"] == "SATISFIED"
    finally:
        agent_a.checkpointing.close()

    agent, storage = _make_agent(
        tmp_path / "d06",
        _resources(files=("model_v1.csv", "model_v2.csv"), artifact=True),
        [
            AgentDecision(action="REPLAN", plan=[_file_plan(artifact=True)], reason_summary="file plan"),
            AgentDecision(action="CALL_TOOL", step_id="1", tool_name="save_result_table",
                          tool_arguments={"format": "csv", "filename": "comparison.csv"},
                          input_refs={"rows": "observation:tool-1:data.subgroups"}, reason_summary="export"),
            AgentDecision(action="FINISH", reason_summary="complete"),
        ],
    )
    try:
        _copy_demo_files(agent, "integration", "A-D06")
        events = [item async for item in agent.stream("compare model_v1.csv and model_v2.csv and export csv", "integration", "A-D06")]
        assert events[-1].event == "FINAL_ANSWER"
        state = events[-1].data["state"]
        assert state["goal_coverage"]["status"] == "SATISFIED"
        assert state["artifacts"]
        artifact = state["observations"][-1]["data"]
        downloaded = storage.download_object(artifact["object_key"])
        assert downloaded.startswith(b"\xef\xbb\xbf") and b"structure_type" in downloaded
    finally:
        agent.checkpointing.close()


@pytest.mark.asyncio
async def test_real_langgraph_B_schema_sql_checker_executor(tmp_path, monkeypatch):
    async def fake_generate(self, goal, current_step, datasource, full_schema, relationships,
                            dataset_version=None, repair_feedback=None, skill_context=None,
                            original_goal=None, query_scope=None, resource_binding=None):
        candidate, validation = _sql_candidate(query_scope)
        return candidate, {"generator": "integration_fake_text2sql", "sql_candidate_status": "scope_verified",
                           "scope_validation": validation, "llm_telemetry": {"llm_called": True, "fallback": False}}

    monkeypatch.setattr("app.services.text2sql.TextToSQLService.generate", fake_generate)
    agent, _ = _make_agent(
        tmp_path,
        _resources(),
        [
            AgentDecision(action="CALL_TOOL", tool_name="search_schema", tool_arguments={"query": "train_v3 structure coverage"}, reason_summary="schema"),
            AgentDecision(action="FINISH", reason_summary="complete"),
        ],
    )
    try:
        events = [item async for item in agent.stream("统计 training_db train_v3 的结构类型训练覆盖", "integration", "B", "training_db")]
        state = events[-1].data["state"]
        assert [call["tool"] for call in state["tool_calls"]] == ["search_schema", "text_to_sql", "query_checker", "execute_readonly_sql"]
        assert state["observations"][3]["metadata"]["backend"] == "postgres"
        assert state["observations"][3]["metadata"]["scope_validation"]["verified"] is True
        assert state["goal_coverage"]["status"] == "SATISFIED"
    finally:
        agent.checkpointing.close()


@pytest.mark.asyncio
async def test_real_langgraph_C_unverified_scope_is_diagnostic_then_repaired(tmp_path, monkeypatch):
    attempts = []

    async def fake_generate(self, goal, current_step, datasource, full_schema, relationships,
                            dataset_version=None, repair_feedback=None, skill_context=None,
                            original_goal=None, query_scope=None, resource_binding=None):
        attempts.append(repair_feedback)
        candidate = SQLCandidate(sql="SELECT * FROM training_molecules", params={}, query_scope=query_scope)
        if not repair_feedback:
            from app.services.text2sql import SQLScopeValidationError
            raise SQLScopeValidationError(UnverifiedScope("complex population branch is not proven"), candidate, {"generator": "integration_fake_text2sql"})
        repaired, validation = _sql_candidate(query_scope)
        return repaired, {"generator": "integration_fake_text2sql_repair", "sql_candidate_status": "scope_verified",
                           "scope_validation": validation, "llm_telemetry": {"llm_called": True, "fallback": False}}

    monkeypatch.setattr("app.services.text2sql.TextToSQLService.generate", fake_generate)
    agent, _ = _make_agent(
        tmp_path,
        _resources(),
        [
            AgentDecision(action="CALL_TOOL", tool_name="search_schema", tool_arguments={"query": "train_v3 prediction error"}, reason_summary="schema"),
            AgentDecision(action="FINISH", reason_summary="bounded recovery"),
        ],
    )
    try:
        events = [item async for item in agent.stream("检查 training_db train_v3 的训练覆盖", "integration", "C", "training_db")]
        state = events[-1].data["state"]
        tools = [call["tool"] for call in state["tool_calls"]]
        assert tools == ["search_schema", "text_to_sql", "text_to_sql", "query_checker", "execute_readonly_sql"]
        assert state["observations"][1]["metadata"]["sql_candidate_status"] == "diagnostic_only"
        assert state["observations"][2]["metadata"]["sql_candidate_status"] == "scope_verified"
        assert attempts[0] is None and attempts[1]
        assert not any(call["tool"] == "query_checker" for call in state["tool_calls"][:3])
    finally:
        agent.checkpointing.close()


def test_real_postgres_checkpoint_survives_service_recreation():
    import asyncio
    import uuid

    thread_id = f"integration-{uuid.uuid4()}"
    first = CheckpointService(ADMIN_URL)
    try:
        interrupted = asyncio.run(first.start_hitl({"query": "coverage", "user_id": "integration", "thread_id": thread_id, "datasource_id": "training_db"}))
        assert interrupted["__interrupt__"] and first.persistent is True
    finally:
        first.close()
    second = CheckpointService(ADMIN_URL)
    try:
        resumed = asyncio.run(second.resume_hitl(thread_id, "train_v3"))
        assert resumed["status"] == "ready" and resumed["dataset_version"] == "train_v3"
    finally:
        second.close()
