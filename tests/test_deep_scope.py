import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from app.agents.deep_runtime import DeepAgentRuntime, DeterministicRuntimeModel
from app.services.checkpointing import CheckpointService
from app.services.workspace import WorkspaceService


@pytest.mark.asyncio
async def test_deep_subtask_rejects_builtin_filesystem_write_before_execution(monkeypatch, tmp_path):
    class OutOfScopeModel(DeterministicRuntimeModel):
        def _generate(self, *args, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{
                "name": "write_file", "args": {"file_path": "/forbidden.txt", "content": "out of scope"}, "id": "blocked-write",
            }]))])
    runtime = DeepAgentRuntime(CheckpointService(database_url=""), WorkspaceService(tmp_path))
    monkeypatch.setattr(runtime, "_model", lambda: OutOfScopeModel())
    with pytest.raises(PermissionError, match="sub-agent tool scope violation"):
        await runtime.run_bounded_subtask("核对 M0001", "user", "thread", [],
            plan_step={"step_id": "molecule-enrichment"}, allowed_tool_scope={"get_molecule_features"}, budget=2)
    assert not (tmp_path / "user" / "thread" / "forbidden.txt").exists()
