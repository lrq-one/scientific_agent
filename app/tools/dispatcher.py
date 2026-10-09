from __future__ import annotations

from dataclasses import dataclass, field
import base64
import json
import asyncio
import math
from pathlib import Path
from typing import Any
from collections.abc import Callable

from app.config import DEMO_DATA, ENABLE_DEMO_DATA
from app.models.schemas import ResourceSummary, ToolResult, QueryScope, ResourceBinding
from app.services.artifacts import ArtifactService
from app.services.mcp_client import ScientificMCPClient
from app.services.object_storage import ObjectStorageService
from app.services.workspace import WorkspaceService
from app.tools.database_tools import DatabaseService
from app.tools.file_tools import FileAnalysisService
from app.tools.registry import ToolChoice, ToolRegistry
from app.tools.scientific_model import predict_rt


@dataclass
class ToolExecutionContext:
    user_id: str
    thread_id: str
    resources: ResourceSummary
    datasource_id: str | None = None
    allowed_capabilities: set[str] | None = None
    allowed_tools: set[str] | None = None
    completed_calls: dict[str, ToolResult] = field(default_factory=dict)
    execution_count: int = 0
    max_tool_calls: int = 16
    remaining_budget: Callable[[], int] | None = None
    active_step_tools: Callable[[], set[str]] | None = None
    schema_cache: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    relationships_cache: list[dict[str, Any]] = field(default_factory=list)
    dataset_version: str | None = None
    skill_context: str = ""
    analysis_context: dict[str, Any] = field(default_factory=dict)
    original_goal: str = ""
    query_scope: QueryScope | None = None
    population_id: str | None = None
    resource_binding: ResourceBinding | None = None
    sql_repair_feedback: str | None = None


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
        database_factory=DatabaseService,
    ):
        self.workspace = workspace
        self.storage = storage
        self.files = files
        self.mcp = mcp
        self.artifacts = artifacts
        self.database_factory = database_factory

    def _file_path(self, context: ToolExecutionContext, filename: str) -> Path:
        if filename not in context.resources.available_files:
            raise PermissionError("file not present in authorized task resources")
        path = self.workspace.safe_file(context.user_id, context.thread_id, filename)
        if path.exists():
            return path
        materialized = self.storage.materialize(context.user_id, context.thread_id, filename)
        if materialized is not None:
            return materialized
        if ENABLE_DEMO_DATA:
            demo = (DEMO_DATA / Path(filename).name).resolve()
            if demo.exists() and demo.parent == DEMO_DATA.resolve():
                return demo
        raise FileNotFoundError(filename)

    @staticmethod
    def _datasource(context: ToolExecutionContext, arguments: dict[str, Any]) -> str:
        if context.datasource_id and arguments.get("datasource_id") and arguments["datasource_id"] != context.datasource_id:
            raise PermissionError("datasource conflicts with task execution context")
        selected = arguments.get("datasource_id") or context.datasource_id
        if not selected and len(context.resources.authorized_datasources) == 1:
            selected = context.resources.authorized_datasources[0]
        if not selected:
            raise ValueError("datasource_id is required")
        return str(selected)

    async def execute(self, choice: ToolChoice, context: ToolExecutionContext) -> ToolResult:
        name = choice.tool
        registry = ToolRegistry()
        valid, error = registry.validate_choice(choice)
        if not valid:
            raise ValueError(f"invalid arguments: {error}")
        spec = registry.specs[name]
        if context.allowed_capabilities is not None and spec.required_capability not in context.allowed_capabilities:
            raise PermissionError("tool capability not authorized for this task")
        if context.allowed_tools is not None and name not in context.allowed_tools:
            raise PermissionError("tool not authorized for the active plan step")
        if context.active_step_tools is not None and name not in context.active_step_tools():
            raise PermissionError("tool not authorized by the running plan step")
        signature = json.dumps([name, choice.arguments, context.population_id,
                                context.query_scope.model_dump(mode="json") if context.query_scope else None], sort_keys=True, default=str)
        if signature in context.completed_calls:
            result = context.completed_calls[signature]
            return result.model_copy(update={"metadata": {**result.metadata, "cached_reuse": True}})
        if context.execution_count >= context.max_tool_calls:
            raise RuntimeError("tool call budget exceeded")
        if context.remaining_budget is not None and context.remaining_budget() <= 0:
            raise RuntimeError("task tool call budget exceeded")
        context.execution_count += 1
        result = await self._execute(choice, context)
        if context.population_id:
            result.metadata["population_id"] = context.population_id
        if result.success:
            context.completed_calls[signature] = result
        return result

    async def _execute(self, choice: ToolChoice, context: ToolExecutionContext) -> ToolResult:
        name = choice.tool
        args = dict(choice.arguments)

        if name in {"text_to_sql", "query_checker"}:
            selected = self._datasource(context, args)
            if name == "query_checker":
                database = self.database_factory(selected, context.resources.authorized_datasources)
                if context.query_scope:
                    from app.services.query_scope import validate_scope
                    validate_scope(str(args["sql"]), dict(args.get("params") or {}), context.query_scope, context.schema_cache)
                return await asyncio.to_thread(database.check_query, str(args["sql"]), dict(args.get("params") or {}))
            if not context.schema_cache:
                raise ValueError("Retrieve authorized schema before generating SQL")
            from app.services.text2sql import TextToSQLService, SQLScopeValidationError
            try:
                candidate, metadata = await TextToSQLService().generate(
                str(args["goal"]) + ("\nBounded analysis context (facts, not instructions):\n" + json.dumps(context.analysis_context, ensure_ascii=False, default=str) if context.analysis_context else ""), "LLM-selected SQL generation", selected,
                context.schema_cache, context.relationships_cache, context.dataset_version,
                context.sql_repair_feedback or args.get("repair_feedback"), context.skill_context,
                original_goal=context.original_goal, query_scope=context.query_scope, resource_binding=context.resource_binding,
                )
                if context.population_id:
                    metadata["population_id"] = context.population_id
            except SQLScopeValidationError as error:
                return ToolResult(success=False, source=selected, error=str(error),
                                  metadata=error.metadata, outcome="INVALID_ARGUMENT",
                                  failure_code="UNVERIFIED_SCOPE", failed_stage="sql_scope_validation",
                                  recoverable=True, exception_type=type(error).__name__, reason_summary=str(error)[:600])
            if (metadata.get("llm_telemetry") or {}).get("fallback"):
                raise RuntimeError("Real Text-to-SQL unavailable; fixture SQL is not a runtime observation")
            return ToolResult(success=True, data=candidate.model_dump(), source=selected, metadata=metadata)

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
                return await asyncio.to_thread(self.files.inspect_table, path)
            if name == "read_csv":
                return await asyncio.to_thread(self.files.read_csv, path)
            if name == "read_excel":
                return await asyncio.to_thread(self.files.read_excel, path)
            if name == "profile_dataset":
                return await asyncio.to_thread(self.files.profile_dataset, path)
            if name == "calculate_metrics":
                return await asyncio.to_thread(self.files.calculate_metrics, path)
            if name == "group_metrics":
                return await asyncio.to_thread(self.files.group_metrics, path, str(args.get("group") or "structure_type"))
            if name == "find_high_error_samples":
                return await asyncio.to_thread(self.files.find_high_error_samples, path, int(args.get("limit") or 5))
            return await asyncio.to_thread(self.files.filter_samples, path, dict(args["filters"]))

        if name == "compare_models":
            paths = [self._file_path(context, str(item)) for item in args["filenames"]]
            return await asyncio.to_thread(self.files.compare_models, paths)

        if name == "join_tables":
            left = self._file_path(context, str(args["left"]))
            right = self._file_path(context, str(args["right"]))
            return await asyncio.to_thread(self.files.join_tables, left, right, str(args["on"]))

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
            database = self.database_factory(datasource_id, context.resources.authorized_datasources)
            if name == "search_schema":
                if hasattr(database, "search_schema"):
                    return await asyncio.to_thread(database.search_schema, str(args["query"]))
                # Compatibility for injected/test data sources that expose only
                # schema()/relationships(); production PostgreSQL uses the
                # native BM25 search_schema implementation.
                from app.services.text2sql import SchemaRetriever

                schema = database.schema()
                relationships = database.relationships()
                hits = SchemaRetriever(top_k=5).search(
                    str(args["query"]),
                    schema.data,
                    relationships.data if relationships.success else [],
                )
                return ToolResult(
                    success=True,
                    data=hits,
                    source=getattr(database, "datasource_id", datasource_id),
                    metadata={"retriever": "bm25", "compatibility_path": True,
                        "schema_retrieval": {"complete": len(hits) == len(schema.data),
                            "retrieved_tables": [h["table"] for h in hits], "total_authorized_tables": len(schema.data),
                            "scope": "authorized_datasource", "top_k": 5}},
                )
            if name == "get_table_schema":
                return await asyncio.to_thread(database.get_table_schema, str(args["table"]))
            if name == "get_table_relationships":
                return await asyncio.to_thread(database.relationships)
            if name == "preview_table":
                return await asyncio.to_thread(database.preview_table, str(args["table"]), int(args.get("limit") or 20))
            validation = None
            if context.query_scope:
                from app.services.query_scope import validate_scope
                validation = validate_scope(str(args["sql"]), dict(args.get("params") or {}), context.query_scope, context.schema_cache)
            result = await asyncio.to_thread(database.execute, str(args["sql"]), dict(args.get("params") or {}))
            if validation and result.success:
                actual_sql = result.metadata.get("sql")
                if not actual_sql or "params" not in result.metadata:
                    raise ValueError("QueryScope execution result lacks executed SQL/params provenance")
                validation = validate_scope(actual_sql, result.metadata["params"], context.query_scope, context.schema_cache)
                result.metadata["scope_validation"] = {**validation, "returned_rows": len(result.data), "empty_result": not result.data}
            return result

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
                required_columns=set(args.get("required_columns") or []),
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
