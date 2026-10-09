"""Production LangGraph replay with fixed decisions and isolated dependencies.

The graph, runtime nodes, registry and dispatcher are real.  Only provider and
external-storage/database boundaries are replaced with deterministic fakes; no
Qwen/OpenAI, PostgreSQL or MinIO call is possible in these tests.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

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
from app.services.query_scope import UnverifiedScope, validate_scope


class FakeSkills:
    def __init__(self, real):
        self.real = real

    async def select_async(self, *_args, **_kwargs):
        # This is an injected provider result, not a heuristic runtime path.
        return [], "fixed_fake_llm", {"llm_called": True, "fallback": False, "model": "fake"}

    def execution_context(self, selected):
        return ""

    def by_name(self):
        return {}


class ScriptedDecider:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = 0

    async def decide(self, _state, _tools, _skill_context):
        self.calls += 1
        if not self.decisions:
            raise AssertionError("fixed decision script exhausted")
        return self.decisions.pop(0), {
            "llm_called": True,
            "fallback": False,
            "model": "fake-fixed-decision",
        }


class FakeResponseService:
    async def generate(self, _question, facts):
        ids = [item.get("evidence_id") for item in facts.get("evidence", []) if item.get("evidence_id")]
        claims = [GroundedClaim(text="fixed offline evidence", evidence_ids=ids[:1])] if ids else []
        return GroundedResponse(answer="offline grounded response", claims=claims), {
            "llm_called": True,
            "fallback": False,
            "model": "fake-response",
        }


class FakeStorage:
    configured = True

    def __init__(self, root: Path):
        self.root = root
        self.writes: list[Path] = []

    def list_files(self, *_args):
        return []

    def materialize(self, *_args):
        return None

    def upload_artifact(self, owner_id, thread_id, filename, _content_type, content, artifact_type, metadata=None):
        target = self.root / "artifacts" / owner_id / thread_id / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self.writes.append(target)
        return {
            "artifact_id": f"artifact-{len(self.writes)}",
            "artifact_type": artifact_type,
            "object_key": str(target),
            "filename": filename,
            "metadata": {"size": len(content), **(metadata or {})},
        }


class FakeDatabase:
    datasource_id = "training_db"

    schema_data = {
        "training_molecules": [
            {"name": "molecule_id", "type": "text"},
            {"name": "dataset_version", "type": "text"},
            {"name": "structure_type", "type": "text"},
        ],
        "molecules": [
            {"name": "molecule_id", "type": "text"},
            {"name": "structure_type", "type": "text"},
        ],
    }

    def __init__(self, selected, allowed):
        if selected not in allowed:
            raise PermissionError("datasource not authorized")

    def schema(self):
        return ToolResult(success=True, data=self.schema_data, source="training_db")

    def relationships(self):
        return ToolResult(success=True, data=[{
            "source_table": "training_molecules", "source_column": "molecule_id",
            "target_table": "molecules", "target_column": "molecule_id",
        }], source="training_db")

    def search_schema(self, query):
        return ToolResult(
            success=True,
            data=[{"table": name, "columns": columns, "relationships": []}
                  for name, columns in self.schema_data.items()],
            source="training_db",
            metadata={"schema_retrieval": {"complete": True, "scope": "authorized_datasource"}},
        )

    def check_query(self, sql, params=None):
        return ToolResult(success=True, data={"valid": True}, source="training_db",
                          metadata={"backend": "fake", "sql": sql, "params": params or {}})

    def execute(self, sql, params=None):
        version = (params or {}).get("dataset_version", "train_v3")
        rows = [
            {"structure_type": "linear", "sample_count": 3},
            {"structure_type": "cyclic", "sample_count": 2},
            {"structure_type": "aromatic", "sample_count": 1},
            {"structure_type": "fused_ring", "sample_count": 1},
        ]
        return ToolResult(
            success=True, data=rows, source="training_db",
            metadata={"backend": "fake", "sql": sql, "params": {"dataset_version": version}, "read_only": True},
        )


def _file_plan(*, artifact=False):
    tools = ["compare_models", "save_result_table"] if artifact else ["compare_models"]
    kwargs = {
        "step_id": "1",
        "goal": "compare model files",
        "allowed_tools": tools,
        "required_capabilities": [Capability.FILE, *( [Capability.ARTIFACT] if artifact else [])],
    }
    if artifact:
        kwargs.update({
            "completion_predicate": CompletionCondition(required_tools=tools),
        })
    return PlanStep(**kwargs)


def _make_agent(tmp_path: Path, resources: ResourceSummary, decisions, *, db=None):
    agent = ScientificAgent()
    # Replace the constructor's process-global saver with an isolated in-memory
    # saver before rebuilding the production graph.
    agent.checkpointing = CheckpointService(database_url="")
    agent.workspace.root = (tmp_path / "workspace").resolve()
    storage = FakeStorage(tmp_path)
    agent.storage = storage
    agent.artifact_service.storage = storage
    agent.tool_dispatcher.workspace = agent.workspace
    agent.tool_dispatcher.storage = storage
    agent.tool_dispatcher.artifacts = agent.artifact_service
    if db is not None:
        agent.tool_dispatcher.database_factory = lambda selected, allowed: db(selected, allowed)
    agent.resources.discover = lambda _user, _thread: resources
    agent.resources.metadata = lambda _user, _thread, value: value
    agent.skills = FakeSkills(agent.skills)
    agent.tool_registry.skills = agent.skills
    agent.runtime = DecisionRuntime(agent)
    agent.runtime.decider = ScriptedDecider(decisions)
    agent.runtime.responses = FakeResponseService()
    return agent, storage


def _copy_demo_files(agent, thread_id, tmp_path):
    target = agent.workspace.path_for("offline", thread_id, create=True)
    for filename in ("model_v1.csv", "model_v2.csv"):
        shutil.copy(Path(__file__).parents[1] / "data" / "demo" / filename, target / filename)


@pytest.mark.asyncio
async def test_langgraph_no_model_A_file_comparison_and_plan_boundary(tmp_path):
    decisions = [
        (AgentDecision(action="REPLAN", plan=[_file_plan()], reason_summary="install file plan")),
        (AgentDecision(action="FINISH", reason_summary="evidence complete")),
    ]
    resources = ResourceSummary(available_files=["model_v1.csv", "model_v2.csv"])
    agent, _ = _make_agent(tmp_path, resources, decisions)
    _copy_demo_files(agent, "A", tmp_path)
    events = [item async for item in agent.stream("compare model_v1.csv and model_v2.csv MAE", "offline", "A")]
    final = events[-1]
    assert final.event == "FINAL_ANSWER"
    state = final.data["state"]
    assert [call["tool"] for call in state["tool_calls"]] == ["compare_models"]
    assert state["goal_coverage"]["status"] == "SATISFIED"
    assert not state["errors"]
    assert not [item for item in events if item.event == "DECISION_REJECTED"]
    assert any(item.event == "OBSERVATION_RECORDED" for item in events)


def _sql_candidate(scope):
    sql = "SELECT m.structure_type, COUNT(*) AS sample_count FROM training_molecules AS tm JOIN molecules AS m ON m.molecule_id = tm.molecule_id WHERE tm.dataset_version = %(dataset_version)s GROUP BY m.structure_type"
    validation = validate_scope(sql, {"dataset_version": "train_v3"}, scope, FakeDatabase.schema_data)
    return SQLCandidate(sql=sql, params={"dataset_version": "train_v3"}, query_scope=scope), validation


@pytest.mark.asyncio
async def test_langgraph_no_model_B_schema_sql_checker_execute(tmp_path, monkeypatch):
    async def fake_generate(self, goal, current_step, datasource, full_schema, relationships,
                            dataset_version=None, repair_feedback=None, skill_context=None,
                            original_goal=None, query_scope=None, resource_binding=None):
        candidate, validation = _sql_candidate(query_scope)
        return candidate, {"generator": "fake_text2sql", "sql_candidate_status": "scope_verified",
                           "scope_validation": validation, "llm_telemetry": {"llm_called": True, "fallback": False}}

    monkeypatch.setattr("app.services.text2sql.TextToSQLService.generate", fake_generate)
    decisions = [
        AgentDecision(action="CALL_TOOL", tool_name="search_schema", tool_arguments={"query": "train_v3 structure coverage"}, reason_summary="schema"),
        AgentDecision(action="FINISH", reason_summary="coverage complete"),
    ]
    resources = ResourceSummary(
        authorized_datasources=["training_db"],
        resource_metadata={"dataset_versions": [{"id": "v3-id", "version": "train_v3", "datasource_id": "training_db"}]},
    )
    agent, _ = _make_agent(tmp_path, resources, decisions, db=FakeDatabase)
    events = [item async for item in agent.stream("统计 training_db 中 train_v3 的结构类型训练覆盖", "offline", "B", "training_db")]
    final = events[-1]
    state = final.data["state"]
    assert final.event == "FINAL_ANSWER"
    assert [call["tool"] for call in state["tool_calls"]] == [
        "search_schema", "text_to_sql", "query_checker", "execute_readonly_sql"
    ]
    assert state["observations"][1]["metadata"]["sql_candidate_status"] == "scope_verified"
    assert state["observations"][2]["metadata"]["sql_candidate_status"] == "checked"
    assert state["observations"][3]["metadata"]["scope_validation"]["verified"] is True
    assert state["goal_coverage"]["status"] == "SATISFIED"
    assert state["quality_status"] in {"SUPPORTED_CONCLUSION", "INSUFFICIENT_EVIDENCE"}


@pytest.mark.asyncio
async def test_langgraph_no_model_C_scope_failure_is_diagnostic_and_repaired_once(tmp_path, monkeypatch):
    attempts = []

    async def fake_generate(self, goal, current_step, datasource, full_schema, relationships,
                            dataset_version=None, repair_feedback=None, skill_context=None,
                            original_goal=None, query_scope=None, resource_binding=None):
        attempts.append(repair_feedback)
        candidate = SQLCandidate(sql="SELECT * FROM training_molecules", params={}, query_scope=query_scope)
        if not repair_feedback:
            from app.services.text2sql import SQLScopeValidationError
            raise SQLScopeValidationError(UnverifiedScope("complex population branch is not proven"), candidate, {"generator": "fake_text2sql"})
        repaired, validation = _sql_candidate(query_scope)
        return repaired, {"generator": "fake_text2sql_repair", "sql_candidate_status": "scope_verified",
                           "scope_validation": validation, "llm_telemetry": {"llm_called": True, "fallback": False}}

    monkeypatch.setattr("app.services.text2sql.TextToSQLService.generate", fake_generate)
    decisions = [
        AgentDecision(action="CALL_TOOL", tool_name="search_schema", tool_arguments={"query": "train_v3 prediction error"}, reason_summary="schema"),
        AgentDecision(action="FINISH", reason_summary="bounded recovery complete"),
    ]
    resources = ResourceSummary(
        authorized_datasources=["training_db"],
        resource_metadata={"dataset_versions": [{"id": "v3-id", "version": "train_v3", "datasource_id": "training_db"}]},
    )
    agent, _ = _make_agent(tmp_path, resources, decisions, db=FakeDatabase)
    events = [item async for item in agent.stream("检查 training_db train_v3 prediction error", "offline", "C", "training_db")]
    final = events[-1]
    state = final.data["state"]
    tools = [call["tool"] for call in state["tool_calls"]]
    assert tools == ["search_schema", "text_to_sql", "text_to_sql", "query_checker", "execute_readonly_sql"]
    assert state["observations"][1]["metadata"]["sql_candidate_status"] == "diagnostic_only"
    assert state["observations"][1]["success"] is False
    assert state["observations"][2]["metadata"]["sql_candidate_status"] == "scope_verified"
    assert attempts[0] is None and attempts[1]
    recovery = [item for item in events if item.event == "RECOVERY_DECISION"]
    assert recovery and recovery[0].data["candidate_status"] == "diagnostic_only"
    assert not any(item.event == "DECISION_REJECTED" for item in events)
    assert state["goal_coverage"]["status"] in {"SATISFIED", "PARTIAL"}


@pytest.mark.asyncio
async def test_langgraph_no_model_D06_artifact_and_rerun_isolated(tmp_path):
    def script():
        return [
            AgentDecision(action="REPLAN", plan=[_file_plan(artifact=True)], reason_summary="install export plan"),
            AgentDecision(action="CALL_TOOL", step_id="1", tool_name="save_result_table",
                          tool_arguments={"format": "csv", "filename": "comparison.csv"},
                          input_refs={"rows": "observation:tool-1:data.subgroups"}, reason_summary="export"),
            AgentDecision(action="FINISH", reason_summary="export complete"),
        ]

    resources = ResourceSummary(available_files=["model_v1.csv", "model_v2.csv"], available_artifact_formats=["csv"])
    agent, storage = _make_agent(tmp_path, resources, script())
    _copy_demo_files(agent, "D06", tmp_path)
    first = [item async for item in agent.stream("compare model_v1.csv and model_v2.csv and export csv", "offline", "D06")]
    assert first[-1].event == "FINAL_ANSWER"
    assert first[-1].data["state"]["artifacts"]
    assert storage.writes and storage.writes[0].exists()

    # RERUN uses a fresh thread and therefore starts with a fresh tool/evidence
    # namespace, while the same production graph and dispatcher are retained.
    agent.runtime.decider = ScriptedDecider(script())
    _copy_demo_files(agent, "D06-rerun", tmp_path)
    second = [item async for item in agent.stream("compare model_v1.csv and model_v2.csv and export csv", "offline", "D06-rerun")]
    assert second[-1].event == "FINAL_ANSWER"
    assert [call["tool_call_id"] for call in second[-1].data["state"]["tool_calls"]] == ["tool-1", "tool-2"]


@pytest.mark.asyncio
async def test_langgraph_no_model_hitl_interrupt_and_resume(tmp_path):
    decisions = [
        AgentDecision(action="ASK_USER", question_to_user="请确认训练版本", missing_information=["user_choice"], reason_summary="scope clarification"),
        AgentDecision(action="FINISH", reason_summary="confirmed"),
    ]
    resources = ResourceSummary(
        authorized_datasources=["training_db"],
        resource_metadata={"dataset_versions": [{"id": "v3-id", "version": "train_v3", "datasource_id": "training_db"}]},
    )
    agent, _ = _make_agent(tmp_path, resources, decisions, db=FakeDatabase)
    first = [item async for item in agent.stream("分析 training_db 的训练覆盖", "offline", "HITL", "training_db")]
    assert first[-1].event == "WAITING_FOR_USER"
    resumed = [item async for item in agent.resume("HITL", "train_v3", "offline")]
    assert any(item.event == "HITL_RESUMED" for item in resumed)
    assert resumed[-1].event == "FINAL_ANSWER"
    assert resumed[-1].data["state"]["hitl_answers"][-1]["answer"] == "train_v3"
