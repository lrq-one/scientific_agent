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
            available_artifact_formats=["csv", "xlsx", "png"] if self.storage.configured else [],
        )

    def metadata(self, user_id: str, thread_id: str, resources: ResourceSummary) -> ResourceSummary:
        """Read identity facts through the authorized readonly datasource only.

        No scientific analysis, no LLM, and no access to conversation/private
        tables. Failures remain explicit rather than inventing fixture identities.
        """
        from app.tools.database_tools import DatabaseService
        from app.tools.file_tools import FileAnalysisService
        result = resources.model_copy(deep=True)
        columns = {
            "datasets": {"id", "name", "dataset_id", "dataset_name"},
            "dataset_versions": {"id", "dataset_id", "version", "version_name", "label"},
            "model_runs": {"id", "run_name", "dataset_version_id", "experiment_id"},
            "experiments": {"id", "dataset_version_id"},
        }
        for datasource in resources.authorized_datasources:
            try:
                db = DatabaseService(datasource, resources.authorized_datasources)
                schema = db.schema().data
                for table, wanted in columns.items():
                    names = sorted({c["name"] for c in schema.get(table, [])} & wanted)
                    if not names:
                        continue
                    rows = db.execute('SELECT ' + ', '.join('"'+n+'"' for n in names) + ' FROM "'+table+'"')
                    if rows.success:
                        result.resource_metadata.setdefault(table, []).extend(
                            {**row, "datasource_id": datasource} for row in rows.data)
            except Exception as exc:
                result.metadata_errors.append(f"{datasource}: identity metadata unavailable ({type(exc).__name__})")
        for filename in resources.available_files:
            try:
                path = self.workspace.safe_file(user_id, thread_id, filename)
                if not path.exists():
                    path = self.storage.materialize(user_id, thread_id, filename)
                if path is None or not path.exists():
                    continue
                table = FileAnalysisService().read_table(path)
                identities = {}
                for key in ("dataset_version", "dataset_id", "model_run"):
                    if key in table:
                        identities[key] = [str(v) for v in table[key].dropna().unique()[:20]]
                result.resource_metadata.setdefault("files", []).append(
                    {"filename": filename, "columns": list(table.columns), **identities})
            except Exception as exc:
                result.metadata_errors.append(f"{filename}: file metadata unavailable ({type(exc).__name__})")
        return result

