from __future__ import annotations

from dataclasses import dataclass
import base64
import math
from pathlib import Path
from typing import Any

from app.models.schemas import ResourceSummary, ToolResult
from app.services.artifacts import ArtifactService
from app.services.mcp_client import ScientificMCPClient
from app.services.object_storage import ObjectStorageService
from app.services.workspace import WorkspaceService
from app.tools.database_tools import DatabaseService
from app.tools.file_tools import FileAnalysisService
from app.tools.registry import ToolChoice
from app.tools.scientific_model import predict_rt


@dataclass
class ToolExecutionContext:
    user_id: str
    thread_id: str
    resources: ResourceSummary
    datasource_id: str | None = None


class ToolDispatcher:
    """Trusted executor for validated ToolChoice objects.

    The LLM may choose only from ToolRegistry candidates. This dispatcher owns
    the actual callable mapping so a selected tool is no longer trace-only.
    """

    def __init__(
        self,
        *,
        workspace: WorkspaceService,
        storage: ObjectStorageService,
        files: FileAnalysisService,
        mcp: ScientificMCPClient,
        artifacts: ArtifactService,
    ):
        self.workspace = workspace
        self.storage = storage
        self.files = files
        self.mcp = mcp
        self.artifacts = artifacts

    def _file_path(self, context: ToolExecutionContext, filename: str) -> Path:
        path = self.workspace.safe_file(context.user_id, context.thread_id, filename)
        if path.exists():
            return path
        materialized = self.storage.materialize(context.user_id, context.thread_id, filename)
        if materialized is not None:
            return materialized
        raise FileNotFoundError(filename)

    @staticmethod
    def _datasource(context: ToolExecutionContext, arguments: dict[str, Any]) -> str:
        selected = arguments.get("datasource_id") or context.datasource_id
        if not selected and len(context.resources.authorized_datasources) == 1:
            selected = context.resources.authorized_datasources[0]
        if not selected:
            raise ValueError("datasource_id is required")
        return str(selected)

    async def execute(self, choice: ToolChoice, context: ToolExecutionContext) -> ToolResult:
        name = choice.tool
        args = dict(choice.arguments)

        if name == "list_workspace_files":
            return ToolResult(
                success=True,
                data=context.resources.available_files,
                source=f"workspace:{context.thread_id}",
                metadata={"dispatcher": True},
            )
        if name in {
            "inspect_table", "read_csv", "read_excel", "profile_dataset",
            "calculate_metrics", "group_metrics", "find_high_error_samples",
            "filter_samples",
        }:
            filename = str(args["filename"])
            path = self._file_path(context, filename)
            if name == "inspect_table":
                return self.files.inspect_table(path)
            if name == "read_csv":
                return self.files.read_csv(path)
            if name == "read_excel":
                return self.files.read_excel(path)
            if name == "profile_dataset":
                return self.files.profile_dataset(path)
            if name == "calculate_metrics":
                return self.files.calculate_metrics(path)
            if name == "group_metrics":
                return self.files.group_metrics(path, str(args.get("group") or "structure_type"))
            if name == "find_high_error_samples":
                return self.files.find_high_error_samples(path, int(args.get("limit") or 5))
            return self.files.filter_samples(path, dict(args["filters"]))

        if name == "compare_models":
            paths = [self._file_path(context, str(item)) for item in args["filenames"]]
            return self.files.compare_models(paths)

        if name == "join_tables":
            left = self._file_path(context, str(args["left"]))
            right = self._file_path(context, str(args["right"]))
            return self.files.join_tables(left, right, str(args["on"]))

        if name in {
            "list_datasources", "search_schema", "get_table_schema",
            "get_table_relationships", "preview_table", "execute_readonly_sql",
        }:
            if name == "list_datasources":
                return ToolResult(
                    success=True,
                    data=context.resources.authorized_datasources,
                    source="authorized_datasources",
                    metadata={"dispatcher": True},
                )
            datasource_id = self._datasource(context, args)
            database = DatabaseService(datasource_id, context.resources.authorized_datasources)
            if name == "search_schema":
                return database.search_schema(str(args["query"]))
            if name == "get_table_schema":
                return database.get_table_schema(str(args["table"]))
            if name == "get_table_relationships":
                return database.relationships()
            if name == "preview_table":
                return database.preview_table(str(args["table"]), int(args.get("limit") or 20))
            return database.execute(str(args["sql"]), dict(args.get("params") or {}))

        if name == "compare_structure_groups":
            rows = list(args["rows"])
            group = str(args["group"])
            grouped: dict[str, list[float]] = {}
            for row in rows:
                if not isinstance(row, dict) or group not in row:
                    continue
                error = row.get("absolute_error")
                if error is None and row.get("observed_rt") is not None and row.get("predicted_rt") is not None:
                    error = abs(float(row["predicted_rt"]) - float(row["observed_rt"]))
                if error is None:
                    continue
                value = float(error)
                if math.isfinite(value):
                    grouped.setdefault(str(row[group]), []).append(value)
            if not grouped:
                return ToolResult(
                    success=False,
                    source="compare_structure_groups",
                    error="no rows contained both the requested group and an error value",
                )
            summary = [
                {
                    group: label,
                    "sample_count": len(values),
                    "mae": sum(values) / len(values),
                }
                for label, values in sorted(grouped.items())
            ]
            return ToolResult(
                success=True,
                data=summary,
                source="compare_structure_groups",
                metadata={"deterministic": True},
            )

        if name == "get_molecule_features":
            return await self.mcp.call("get_molecule_features", {"molecule_id": str(args["molecule_id"])})

        if name == "predict_rt":
            return predict_rt(str(args["smiles"]))

        if name == "save_result_table":
            if not self.storage.configured:
                return ToolResult(success=False, source="artifact", error="object_storage_unavailable")
            fmt = str(args["format"])
            filename = str(args.get("filename") or f"result_table.{fmt}")
            artifact = self.artifacts.save_result_table(
                context.user_id,
                context.thread_id,
                list(args["rows"]),
                filename,
                fmt,
            )
            return ToolResult(success=True, data=artifact, source=artifact["object_key"])

        if name == "plot_metric_comparison":
            if not self.storage.configured:
                return ToolResult(success=False, source="artifact", error="object_storage_unavailable")
            artifact = self.artifacts.plot_metric_comparison(
                context.user_id,
                context.thread_id,
                dict(args["metrics"]),
            )
            return ToolResult(success=True, data=artifact, source=artifact["object_key"])

        if name == "save_chart":
            if not self.storage.configured:
                return ToolResult(success=False, source="artifact", error="object_storage_unavailable")
            try:
                content = base64.b64decode(str(args["image_base64"]), validate=True)
            except Exception:
                return ToolResult(success=False, source="artifact", error="invalid_base64_png")
            if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                return ToolResult(success=False, source="artifact", error="chart_content_is_not_png")
            filename = str(args["filename"])
            artifact = self.artifacts.save_chart(
                context.user_id,
                context.thread_id,
                content,
                filename,
            )
            return ToolResult(success=True, data=artifact, source=artifact["object_key"])

        return ToolResult(
            success=False,
            source=f"dispatcher:{name}",
            error="tool_dispatch_not_implemented",
            metadata={"tool": name},
        )
