from pathlib import Path

import pytest

from app.agents.deep_runtime import DeepAgentRuntime
from app.services.checkpointing import CheckpointService
from app.services.workspace import WorkspaceService


@pytest.mark.asyncio
async def test_selected_skill_enters_real_deepagents_runtime(tmp_path: Path):
    runtime = DeepAgentRuntime(CheckpointService(database_url=None), WorkspaceService(tmp_path))
    trace = await runtime.run_scaffold("比较模型", "user", "thread", ["model_comparison"])
    assert trace["runtime"] == "deepagents.create_deep_agent"
    assert trace["skills"] == ["model_comparison"]
    assert trace["tool_calls"][0]["tool"] == "register_runtime_context"
    assert trace["workspace_backend"] == "FilesystemBackend"

