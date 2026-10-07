from pathlib import Path
import pytest

from app.services.workspace import WorkspaceError, WorkspaceService


def test_workspace_isolation(tmp_path: Path):
    service = WorkspaceService(tmp_path)
    first = service.path_for("alice", "thread-1", create=True)
    second = service.path_for("bob", "thread-1", create=True)
    assert first != second
    assert first.parent.name == "alice"
    assert second.parent.name == "bob"


@pytest.mark.parametrize("filename", ["../secret.csv", "..\\secret.csv", "folder/file.csv"])
def test_path_traversal_rejected(tmp_path: Path, filename: str):
    service = WorkspaceService(tmp_path)
    with pytest.raises(WorkspaceError, match="path traversal"):
        service.safe_file("alice", "thread", filename, create_workspace=True)

