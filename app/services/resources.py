from __future__ import annotations

import os

from app.config import DEMO_DATA, ENABLE_DEMO_DATA
from app.models.schemas import ResourceSummary
from app.services.workspace import WorkspaceService
from app.services.object_storage import ObjectStorageService


class ResourceService:
    """Discovers facts before semantic routing; the model never invents capabilities."""

    def __init__(self, workspace: WorkspaceService | None = None, storage: ObjectStorageService | None = None):
        self.workspace = workspace or WorkspaceService()
        self.storage = storage or ObjectStorageService(workspace=self.workspace)

    def discover(self, user_id: str, thread_id: str) -> ResourceSummary:
        files = self.workspace.list_files(user_id, thread_id)
        files += self.storage.list_files(user_id, thread_id)
        demo_files = sorted(path.name for path in DEMO_DATA.glob("model_*.csv")) if ENABLE_DEMO_DATA else []
        files = sorted(set(files + demo_files))
        datasources = ["training_db"] if user_id and os.getenv("DATABASE_URL") else []
        # MODEL_PATH alone is not a verified inference capability: predict_rt
        # currently has no checkpoint loader or preprocessing adapter.
        models: list[str] = []
        return ResourceSummary(
            available_files=files,
            authorized_datasources=datasources,
            available_scientific_models=models,
            available_mcp_tools=["get_molecule_features"],
        )

