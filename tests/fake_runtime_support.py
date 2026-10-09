"""Deterministic provider/storage boundaries for offline LangGraph contract tests."""
from __future__ import annotations

import shutil
from pathlib import Path

from app.agents.runtime import DecisionRuntime
from app.agents.scientific_agent import ScientificAgent
from app.models.schemas import AgentDecision, Capability, GroundedClaim, GroundedResponse, PlanStep, ResourceSummary
from app.services.checkpointing import CheckpointService
from app.services.workspace import WorkspaceService


class FakeSkills:
    async def select_async(self, *_args, **_kwargs):
        return [], "fixed_offline_provider", {"llm_called": True, "fallback": False, "model": "fake"}

    def execution_context(self, _selected):
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
        claims = [GroundedClaim(text="offline grounded evidence", evidence_ids=ids[:1])] if ids else []
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


def make_agent(tmp_path: Path, resources: ResourceSummary, decisions, *, database_factory=None):
    agent = ScientificAgent()
    agent.checkpointing = CheckpointService(database_url="")
    agent.workspace = WorkspaceService(tmp_path / "workspace")
    storage = FakeStorage(tmp_path)
    agent.storage = storage
    agent.artifact_service.storage = storage
    agent.tool_dispatcher.workspace = agent.workspace
    agent.tool_dispatcher.storage = storage
    agent.tool_dispatcher.artifacts = agent.artifact_service
    if database_factory is not None:
        agent.tool_dispatcher.database_factory = database_factory
    agent.resources.discover = lambda _user, _thread: resources
    agent.resources.metadata = lambda _user, _thread, value: value
    agent.skills = FakeSkills()
    agent.tool_registry.skills = agent.skills
    agent.runtime = DecisionRuntime(agent)
    agent.runtime.decider = ScriptedDecider(decisions)
    agent.runtime.responses = FakeResponseService()
    return agent, storage


def copy_demo_files(agent, user_id: str, thread_id: str):
    target = agent.workspace.path_for(user_id, thread_id, create=True)
    source = Path(__file__).parents[1] / "data" / "demo"
    for filename in ("model_v1.csv", "model_v2.csv"):
        shutil.copy(source / filename, target / filename)


def file_plan(*, step_id="1", artifact=False):
    tools = ["compare_models", "save_result_table"] if artifact else ["compare_models"]
    kwargs = {
        "step_id": step_id,
        "goal": "compare model files",
        "selected_tools": tools,
        "required_capabilities": [Capability.FILE, *( [Capability.ARTIFACT] if artifact else [])],
    }
    return PlanStep(**kwargs)


def database_step(step_id: str, tool: str, *, depends_on=()):
    return PlanStep(
        step_id=step_id,
        goal=f"{tool} training data",
        depends_on=list(depends_on),
        selected_tools=[tool],
        required_capabilities=[Capability.DATABASE],
    )
