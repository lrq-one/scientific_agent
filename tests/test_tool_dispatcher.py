from __future__ import annotations

import base64

import pytest

from app.models.schemas import ResourceSummary
from app.services.artifacts import ArtifactService
from app.services.mcp_client import ScientificMCPClient
from app.services.object_storage import ObjectStorageService
from app.services.workspace import WorkspaceService
from app.tools.dispatcher import ToolDispatcher, ToolExecutionContext
from app.tools.file_tools import FileAnalysisService
from app.tools.registry import ToolChoice


@pytest.mark.asyncio
async def test_selected_file_tool_is_the_actual_dispatch_authority(tmp_path):
    workspace = WorkspaceService(tmp_path)
    root = workspace.path_for("user", "thread", create=True)
    (root / "rows.csv").write_text("x,y\n1,2\n3,4\n", encoding="utf-8")
    storage = ObjectStorageService(workspace=workspace)
    dispatcher = ToolDispatcher(
        workspace=workspace,
        storage=storage,
        files=FileAnalysisService(),
        mcp=ScientificMCPClient(),
        artifacts=ArtifactService(storage),
    )
    context = ToolExecutionContext(
        user_id="user",
        thread_id="thread",
        resources=ResourceSummary(available_files=["rows.csv"]),
    )
    result = await dispatcher.execute(
        ToolChoice(tool="inspect_table", arguments={"filename": "rows.csv"}, reason="test"),
        context,
    )
    assert result.success is True
    assert result.data["row_count"] == 2


@pytest.mark.asyncio
async def test_structure_group_tool_is_deterministic(tmp_path):
    workspace = WorkspaceService(tmp_path)
    storage = ObjectStorageService(workspace=workspace)
    dispatcher = ToolDispatcher(
        workspace=workspace,
        storage=storage,
        files=FileAnalysisService(),
        mcp=ScientificMCPClient(),
        artifacts=ArtifactService(storage),
    )
    context = ToolExecutionContext(
        user_id="user",
        thread_id="thread",
        resources=ResourceSummary(),
    )
    result = await dispatcher.execute(
        ToolChoice(
            tool="compare_structure_groups",
            arguments={
                "rows": [
                    {"structure_type": "linear", "absolute_error": 0.2},
                    {"structure_type": "linear", "absolute_error": 0.4},
                    {"structure_type": "fused_ring", "absolute_error": 1.0},
                ],
                "group": "structure_type",
            },
            reason="test",
        ),
        context,
    )
    assert result.success is True
    by_group = {row["structure_type"]: row for row in result.data}
    assert by_group["linear"]["sample_count"] == 2
    assert by_group["linear"]["mae"] == pytest.approx(0.3)
    assert by_group["fused_ring"]["mae"] == pytest.approx(1.0)
