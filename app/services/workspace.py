from __future__ import annotations

from pathlib import Path
import re

from app.config import WORKSPACE_ROOT


SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class WorkspaceError(ValueError):
    pass


class WorkspaceService:
    def __init__(self, root: Path = WORKSPACE_ROOT):
        self.root = root.resolve()

    def path_for(self, user_id: str, thread_id: str, create: bool = False) -> Path:
        if not SAFE_ID.fullmatch(user_id) or not SAFE_ID.fullmatch(thread_id):
            raise WorkspaceError("invalid user_id or thread_id")
        path = (self.root / user_id / thread_id).resolve()
        if self.root not in path.parents:
            raise WorkspaceError("path traversal rejected")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def safe_file(self, user_id: str, thread_id: str, filename: str, create_workspace: bool = False) -> Path:
        if Path(filename).name != filename or filename in {"", ".", ".."}:
            raise WorkspaceError("path traversal rejected")
        workspace = self.path_for(user_id, thread_id, create=create_workspace)
        target = (workspace / filename).resolve()
        if workspace not in target.parents:
            raise WorkspaceError("path traversal rejected")
        return target

    def list_files(self, user_id: str, thread_id: str) -> list[str]:
        workspace = self.path_for(user_id, thread_id, create=False)
        if not workspace.exists():
            return []
        return sorted(p.name for p in workspace.iterdir() if p.is_file() and p.suffix.lower() in {".csv", ".xlsx", ".xls"})

