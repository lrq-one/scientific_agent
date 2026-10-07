from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
import re
import uuid

from app.config import DEMO_DATA, ENABLE_DEMO_DATA, MAX_REPLANS, MAX_TOOL_CALLS, TASK_TIMEOUT
from app.agents.planning_graph import build_planning_graph
from app.agents.planning_policy import PlanningPolicy
from app.agents.request_router import RequestRouter
from app.agents.deep_runtime import DeepAgentRuntime
from app.models.schemas import Capability, Evidence, GroundedClaim, PlanStep, RequestIntent, ResourceSummary, SSEEvent, ScientificAgentState, ToolResult
from app.services.resources import ResourceService
from app.services.skills import SkillService
from app.services.text2sql import SchemaRetriever, TextToSQLService
from app.services.mcp_client import ScientificMCPClient
from app.services.object_storage import ObjectStorageService
from app.services.workspace import WorkspaceService
from app.services.checkpointing import checkpoint_service
from app.services.artifacts import ArtifactService
from app.services.cross_dataset import SQL as CROSS_DATASET_SQL, compare_rows, missing_columns, parse_versions
from app.services.recovery import bounded_transient_retry, classify_failure
from app.tools.database_tools import DatabaseService
from app.tools.file_tools import FileAnalysisService
from app.tools.registry import ToolRegistry
from app.tools.dispatcher import ToolDispatcher, ToolExecutionContext


def event(name: str, message: str, **data) -> SSEEvent:
    return SSEEvent(event=name, message=message, data=data)


class ToolLimitExceeded(RuntimeError):
    pass


class PlanDependencyError(RuntimeError):
    pass


class ScientificAgent:
    def __init__(self):
        self.workspace = WorkspaceService()
        self.storage = ObjectStorageService(workspace=self.workspace)
        self.resources = ResourceService(self.workspace, self.storage)
        self.router = RequestRouter()
        self.skills = SkillService()
        self.files = FileAnalysisService()
        self.text2sql = TextToSQLService()
        self.schema_retriever = SchemaRetriever(top_k=5)
        self.mcp = ScientificMCPClient()
        self.artifact_service = ArtifactService(self.storage)
        self.tool_registry = ToolRegistry(skills=self.skills)
        self.tool_dispatcher = ToolDispatcher(
            workspace=self.workspace,
            storage=self.storage,
            files=self.files,
            mcp=self.mcp,
            artifacts=self.artifact_service,
        )
        self.pending: dict[str, dict] = {}
        self.checkpointing = checkpoint_service
        self.planning_graph = build_planning_graph(self.checkpointing.checkpointer)
        self.planning_policy = PlanningPolicy()
        self.deep_runtime = DeepAgentRuntime(self.checkpointing, self.workspace)

    @staticmethod
    def _artifact_preferences(query: str) -> tuple[bool, bool]:
        text = query.lower()
        suppress_chart = any(word in text for word in ("不要画图", "不画图", "无需图", "no chart"))
        wants_chart = not suppress_chart and any(
            word in text for word in ("画图", "图表", "可视化", "png", "chart", "plot")
        )
        wants_table = any(
            word in text for word in ("表格", "结果表", "导出", "保存", "csv", "xlsx", "excel", "table")
        )
        return wants_table, wants_chart

    @staticmethod
    def _preferred_file_tool(query: str) -> str:
        text = query.lower()
        if any(word in text for word in ("高误差", "最大误差", "top error", "highest error")):
            return "find_high_error_samples"
        if any(word in text for word in ("缺失", "重复", "质量", "missing", "duplicate", "profile")):
            return "profile_dataset"
        if any(word in text for word in ("mae", "rmse", "指标", "预测误差", "模型误差")):
            return "calculate_metrics"
        return "inspect_table"

    @staticmethod
    def _start_plan_step(state: ScientificAgentState, step_id: str) -> PlanStep:
        step = next((item for item in state.plan if item.step_id == step_id), None)
        if step is None:
            raise PlanDependencyError(f"plan step missing: {step_id}")
        statuses = {item.step_id: item.status for item in state.plan}
        blocked = [dependency for dependency in step.depends_on if statuses.get(dependency) != "completed"]
        if blocked:
            raise PlanDependencyError(f"plan step {step_id} blocked by {blocked}")
        if step.status != "pending":
            raise PlanDependencyError(f"plan step {step_id} cannot start from {step.status}")
        step.status = "running"
        return step

    @staticmethod
    def _finish_plan_step(step: PlanStep, summary: str) -> None:
        if step.status != "running":
            raise PlanDependencyError(f"plan step {step.step_id} is not running")
        step.status = "completed"
        step.result_summary = summary

    def _file_path(self, user_id: str, thread_id: str, name: str) -> Path:
        workspace_path = self.workspace.safe_file(user_id, thread_id, name)
        if workspace_path.exists():
            return workspace_path
        materialized = self.storage.materialize(user_id, thread_id, name)
        if materialized is not None:
            return materialized
        if ENABLE_DEMO_DATA:
            demo_path = (DEMO_DATA / Path(name).name).resolve()
            if demo_path.exists() and demo_path.parent == DEMO_DATA.resolve():
                return demo_path
        raise FileNotFoundError(name)

    @staticmethod
    def _file_dataset_version(path: Path) -> str | None:
        try:
            return "synthetic_demo" if path.resolve().parent == DEMO_DATA.resolve() else None
        except OSError:
            return None

    def _record_tool(self, state: ScientificAgentState, tool: str, result: ToolResult) -> str:
        if state.tool_call_count >= MAX_TOOL_CALLS:
            raise ToolLimitExceeded(f"tool call limit ({MAX_TOOL_CALLS}) reached")
        call_id = f"tool-{state.tool_call_count + 1}"
        state.tool_call_count += 1
        state.tool_calls.append({"tool_call_id": call_id, "tool": tool, "success": result.success})
        state.observations.append(result)
        if not result.success:
            state.failure_count += 1
        return call_id

    def _add_evidence(self, state: ScientificAgentState, claim: str, value, source_type: str, source: str, call_id: str, dataset_version: str | None = None) -> Evidence:
        evidence = Evidence(
            evidence_id=f"ev-{len(state.evidence) + 1}", claim=claim, value=value,
            source_type=source_type, source=source, tool_call_id=call_id,
            dataset_version=dataset_version,
        )
        state.evidence.append(evidence)
        return evidence

    def _database_evidence(
        self,
        state: ScientificAgentState,
        result: ToolResult,
        source: str,
        call_id: str,
        dataset_version: str | None,
    ) -> list[Evidence]:
        """Preserve the complete SQL result and derive fused-ring coverage without assuming one schema."""
        rows = [dict(row) for row in result.data if isinstance(row, dict)] if isinstance(result.data, list) else []
        if not rows:
            state.uncertainties.append(
                "只读 SQL 查询返回 0 rows；空结果不能解释为科学对象不存在，也不能支撑确定性结论。"
            )
            return []
        has_error_metric = any(
            any("error" in str(key).lower() or "mae" in str(key).lower() or "rmse" in str(key).lower() for key in row)
            for row in rows
        )
        details = self._add_evidence(
            state,
            "训练覆盖与预测误差统计" if has_error_metric else "训练覆盖统计",
            rows,
            "database",
            source,
            call_id,
            dataset_version,
        )
        fused_row = next(
            (
                row
                for row in rows
                if row.get("structure_type") == "fused_ring" or row.get("is_fused_ring") is True
            ),
            None,
        )
        if fused_row is None:
            if "fused" in state.goal.lower():
                state.uncertainties.append(
                    "查询返回的行中没有 fused_ring 分组，不能据此把 fused_ring 覆盖数推断为 0。"
                )
            return [details]
        fused_count = fused_row.get(
            "sample_count",
            fused_row.get("train_molecule_count", fused_row.get("molecule_count")),
        )
        if fused_count is None:
            state.uncertainties.append("fused_ring 分组缺少样本数/训练覆盖字段。")
            return [details]
        coverage = self._add_evidence(
            state,
            "训练集中 fused_ring 覆盖数",
            fused_count,
            "database",
            source,
            call_id,
            dataset_version or "synthetic_demo_all_versions",
        )
        return [details, coverage]

    def _make_plan(self, intent) -> list[PlanStep]:
        if intent.task_type == "mixed_analysis":
            return [
                PlanStep(step_id="file-comparison", goal="比较模型整体指标", preferred_tools=["calculate_metrics"]),
                PlanStep(step_id="subgroup-analysis", goal="分析结构子群误差", depends_on=["file-comparison"], preferred_tools=["group_metrics"]),
                PlanStep(step_id="training-coverage", goal="查询训练数据覆盖", depends_on=["subgroup-analysis"], preferred_tools=["schema_retrieval", "execute_readonly_sql"]),
            ]
        return [PlanStep(step_id="execute", goal=intent.goal, preferred_tools=list(intent.required_capabilities))]

    async def _run_cross_dataset_comparison(
        self,
        state: ScientificAgentState,
        resources: ResourceSummary,
        datasource_id: str | None,
    ) -> AsyncIterator[SSEEvent]:
        """Executable Skill contract: authorized schema -> guarded SQL -> raw Evidence -> comparison."""
        versions = parse_versions(state.goal)
        selected = datasource_id or "training_db"
        if not versions:
            state.uncertainties.append("比较数据集版本需要明确两个不同的 train_vN 版本")
            state.final_answer = self._finalize(state)
            yield event("FINAL_ANSWER", "缺少版本", answer=state.final_answer, state=state.model_dump(mode="json"))
            return
        eligible = self.tool_registry.candidates(
            available_capabilities={"database"}, role="researcher",
            selected_skills=["cross_dataset_comparison"],
        )
        allowed_tools = {spec.name for spec in eligible}
        if not {"get_table_schema", "execute_readonly_sql"}.issubset(allowed_tools):
            state.uncertainties.append("Skill 所需的只读数据库工具未获授权")
            state.final_answer = self._finalize(state)
            yield event("FINAL_ANSWER", "能力检查未通过", answer=state.final_answer, state=state.model_dump(mode="json"))
            return
        yield event(
            "SKILL_CAPABILITY_CHECK", "已验证数据集比较 Skill 合约和工具权限",
            skill="cross_dataset_comparison", datasource=selected,
            required_capabilities=["database"],
            selected_tools=["get_table_schema", "query_checker", "execute_readonly_sql"],
            candidate_tools=sorted(allowed_tools),
        )
        try:
            database = DatabaseService(selected, resources.authorized_datasources)
            yield event("TOOL_STARTED", "检查数据集比较所需 Schema", tool="get_table_schema", datasource=selected)
            schema_result = await bounded_transient_retry(lambda: asyncio.to_thread(database.schema))
            self._record_tool(state, "get_table_schema", schema_result)
            yield event("TOOL_FINISHED", "Schema 检查完成", tool="get_table_schema", result=schema_result.model_dump())
            missing = missing_columns(schema_result.data)
            if missing:
                state.uncertainties.append(f"缺少数据集比较所需表或字段：{missing}")
                state.final_answer = self._finalize(state)
                yield event("FINAL_ANSWER", "所需 Schema 不完整", answer=state.final_answer, state=state.model_dump(mode="json"))
                return
            params = {"version_a": versions[0], "version_b": versions[1]}
            yield event("TOOL_STARTED", "校验参数化只读 SQL", tool="query_checker", datasource=selected)
            checked = await bounded_transient_retry(lambda: asyncio.to_thread(database.check_query, CROSS_DATASET_SQL, params))
            self._record_tool(state, "query_checker", checked)
            yield event("TOOL_FINISHED", "只读 SQL 校验完成", tool="query_checker", result=checked.model_dump())
            yield event("TOOL_STARTED", "查询两个数据集版本的结构组成", tool="execute_readonly_sql", datasource=selected)
            result = await bounded_transient_retry(lambda: asyncio.to_thread(database.execute, CROSS_DATASET_SQL, params))
            call_id = self._record_tool(state, "execute_readonly_sql", result)
            yield event("TOOL_FINISHED", "版本结构组成查询完成", tool="execute_readonly_sql", result=result.model_dump())
        except Exception as exc:
            decision = classify_failure(exc, tool="cross_dataset_comparison")
            failed = ToolResult(success=False, source=selected, error=str(exc), metadata={"failure_kind": decision.failure_kind})
            self._record_tool(state, "cross_dataset_comparison", failed)
            yield event("RECOVERY_DECISION", "数据集比较失败并安全终止", failure_kind=decision.failure_kind,
                        action="fail_safely", reason=decision.reason[:500])
            state.final_answer = self._finalize(state)
            yield event("FINAL_ANSWER", "数据集比较失败", answer=state.final_answer, state=state.model_dump(mode="json"))
            return
        rows = result.data if isinstance(result.data, list) else []
        if not rows:
            state.uncertainties.append("只读 SQL 返回 0 rows；无法比较两个版本")
        else:
            raw = self._add_evidence(
                state, "两个数据集版本的结构类型原始计数", rows, "database", selected,
                call_id, ",".join(versions),
            )
            yield event("EVIDENCE_ADDED", "已保存原始版本计数", evidence=raw.model_dump())
            comparison, issues = compare_rows(rows, versions)
            state.uncertainties.extend(issues)
            if comparison:
                derived = self._add_evidence(
                    state, "两个数据集版本的结构类型计数差", comparison, "database", selected,
                    call_id, ",".join(versions),
                )
                yield event("EVIDENCE_ADDED", "已计算结构类型计数差", evidence=derived.model_dump())
        if any(word in state.goal for word in ("数据质量", "缺失率", "重复率", "预测", "误差", "OOD", "分布漂移")):
            state.uncertainties.append("当前结构计数查询不足以回答数据质量、预测表现或分布漂移问题")
        state.final_answer = self._finalize(state)
        yield event("FINAL_ANSWER", "数据集比较完成", answer=state.final_answer, state=state.model_dump(mode="json"))

    async def _reconcile_file_database(
        self,
        state: ScientificAgentState,
        resources: ResourceSummary,
        file_path: Path,
        datasource_id: str | None,
        dataset_version: str,
    ) -> AsyncIterator[SSEEvent]:
        """Join the selected file's molecule IDs to one versioned DB membership, without writes."""
        selected = datasource_id or "training_db"
        try:
            frame = self.files.read_table(file_path)
            required = {"molecule_id", "structure_type"}
            if not required.issubset(frame.columns):
                state.uncertainties.append(f"跨资源关联缺少文件列：{sorted(required - set(frame.columns))}")
                return
            if frame.empty or frame["molecule_id"].isna().any() or frame["molecule_id"].duplicated().any():
                state.uncertainties.append("跨资源关联要求非空且唯一的文件 molecule_id")
                return
            if frame["structure_type"].isna().any():
                state.uncertainties.append("跨资源关联的文件 structure_type 存在 NULL")
                return
            if len(frame) > 100:
                state.uncertainties.append("文件 molecule_id 超过 100 行的跨资源关联上限；未执行截断式关联")
                return
            ids = [str(value) for value in frame["molecule_id"]]
            placeholders = ", ".join(f"%(molecule_{index})s" for index in range(len(ids)))
            params = {"dataset_version": dataset_version, **{
                f"molecule_{index}": molecule_id for index, molecule_id in enumerate(ids)
            }}
            sql = (
                "SELECT tm.molecule_id, tm.dataset_version, m.structure_type "
                "FROM training_molecules AS tm JOIN molecules AS m ON m.molecule_id = tm.molecule_id "
                f"WHERE tm.dataset_version = %(dataset_version)s AND tm.molecule_id IN ({placeholders}) "
                "ORDER BY tm.molecule_id"
            )
            database = DatabaseService(selected, resources.authorized_datasources)
            yield event("TOOL_STARTED", "按 molecule_id 关联文件与版本化数据库", tool="cross_resource_join",
                        datasource=selected, file=file_path.name, dataset_version=dataset_version)
            await asyncio.to_thread(database.check_query, sql, params)
            result = await asyncio.to_thread(database.execute, sql, params)
            result.metadata.update({"file": file_path.name, "join_key": "molecule_id"})
            call_id = self._record_tool(state, "cross_resource_join", result)
            yield event("TOOL_FINISHED", "跨资源只读关联完成", tool="cross_resource_join", result=result.model_dump())
        except Exception as exc:
            failed = ToolResult(success=False, source=selected, error=str(exc),
                                metadata={"failure_kind": classify_failure(exc, tool="cross_resource_join").failure_kind})
            self._record_tool(state, "cross_resource_join", failed)
            yield event("TOOL_FINISHED", "跨资源关联失败", tool="cross_resource_join", result=failed.model_dump())
            return
        file_call = next(
            (call["tool_call_id"] for call in reversed(state.tool_calls) if call["tool"] == "group_metrics"),
            call_id,
        )
        file_rows = [{"molecule_id": molecule_id, "structure_type": str(structure)}
                     for molecule_id, structure in zip(ids, frame["structure_type"], strict=True)]
        file_evidence = self._add_evidence(state, "用于跨资源关联的文件分子与结构类型", file_rows,
                                           "file", file_path.name, file_call)
        yield event("EVIDENCE_ADDED", "已保存文件侧关联键", evidence=file_evidence.model_dump())
        raw_rows = result.data if isinstance(result.data, list) else []
        if not raw_rows:
            state.uncertainties.append("版本化数据库中未找到文件分子的关联记录；不能推断其科学上不存在")
            return
        raw = self._add_evidence(state, "文件分子在版本化训练集中的原始关联行", raw_rows, "database",
                                 selected, call_id, dataset_version)
        yield event("EVIDENCE_ADDED", "已保存跨资源原始关联证据", evidence=raw.model_dump())
        file_structure = dict(zip(ids, frame["structure_type"].astype(str), strict=True))
        db_structure = {str(row["molecule_id"]): str(row["structure_type"]) for row in raw_rows}
        conflicts = [molecule_id for molecule_id in ids if molecule_id in db_structure
                     and file_structure[molecule_id] != db_structure[molecule_id]]
        unmatched = [molecule_id for molecule_id in ids if molecule_id not in db_structure]
        summary = {
            "file": file_path.name,
            "dataset_version": dataset_version,
            "file_molecule_count": len(ids),
            "matched_count": len(ids) - len(unmatched),
            "unmatched_molecule_ids": unmatched,
            "structure_conflict_molecule_ids": conflicts,
        }
        evidence = self._add_evidence(state, "文件与训练集按 molecule_id 关联核对", summary,
                                      "database", selected, call_id, dataset_version)
        yield event("EVIDENCE_ADDED", "已形成跨资源关联摘要", evidence=evidence.model_dump())
        if conflicts:
            state.uncertainties.append(f"文件与数据库的结构类型冲突：{conflicts}")
        if unmatched:
            state.uncertainties.append(f"目标版本中未关联的文件分子：{unmatched}；不推断其在其他版本不存在")

    async def _run_database_branch(
        self,
        state: ScientificAgentState,
        resources: ResourceSummary,
        intent: RequestIntent,
        query: str,
        datasource_id: str | None,
        dataset_version: str | None,
    ) -> AsyncIterator[SSEEvent]:
        selected = datasource_id or "training_db"
        if not state.plan:
            state.plan = [PlanStep(
                step_id="training-coverage", goal=query, required_capabilities=[Capability.DATABASE],
                preferred_tools=["schema_retrieval", "text_to_sql", "query_checker", "execute_readonly_sql"],
                selected_tools=["schema_retrieval", "text_to_sql", "query_checker", "execute_readonly_sql"],
            )]
            yield event("PLAN_CREATED", "已建立数据库执行计划", plan=[step.model_dump(mode="json") for step in state.plan], max_replans=MAX_REPLANS)
        if intent.task_type == "mixed_analysis" or state.plan[0].step_id == "training-coverage":
            coverage_step = self._start_plan_step(state, "training-coverage")
            yield event("PLAN_STEP_STARTED", "开始执行计划步骤", step_id="training-coverage", status="running")
        else:
            coverage_step = None
        schema_step = self._start_plan_step(state, "schema-retrieval") if any(
            step.step_id == "schema-retrieval" for step in state.plan
        ) else None
        if schema_step:
            yield event("PLAN_STEP_STARTED", "开始检索 Schema 计划步骤", step_id=schema_step.step_id, status="running")
        database = DatabaseService(selected, resources.authorized_datasources)
        yield event("TOOL_STARTED", "正在检索相关数据库 Schema", tool="search_schema", datasource=selected)
        try:
            if hasattr(database, "search_schema"):
                schema_hits = await bounded_transient_retry(
                    lambda: asyncio.to_thread(database.search_schema, query, 5)
                )
            else:
                raw_schema = await bounded_transient_retry(lambda: asyncio.to_thread(database.schema))
                hits = self.schema_retriever.search(query, raw_schema.data, [])
                schema_hits = ToolResult(
                    success=True,
                    data=hits,
                    source=selected,
                    metadata={"retriever": "bm25", "compatibility_path": True},
                )
            if not schema_hits.success or not isinstance(schema_hits.data, list) or not schema_hits.data:
                raise RuntimeError(schema_hits.error or "schema retrieval returned no candidates")
            schema_map = {
                str(hit["table"]): hit.get("columns", [])
                for hit in schema_hits.data
                if isinstance(hit, dict) and hit.get("table")
            }
            schema = ToolResult(
                success=bool(schema_map),
                data=schema_map,
                source=selected,
                metadata={
                    **(schema_hits.metadata or {}),
                    "retrieved_tables": list(schema_map),
                    "top_k": 5,
                },
                error=None if schema_map else "schema retrieval returned no table definitions",
            )
            if not schema.success:
                raise RuntimeError(schema.error)
        except Exception as exc:
            decision = classify_failure(exc, tool="search_schema")
            failed = ToolResult(success=False, source=selected, error=str(exc), metadata={"failure_kind": decision.failure_kind})
            self._record_tool(state, "search_schema", failed)
            if schema_step:
                schema_step.status = "failed"
                schema_step.error = str(exc)[:500]
            yield event("TOOL_FINISHED", "相关 Schema 检索失败", tool="search_schema", result=failed.model_dump())
            yield event("RECOVERY_DECISION", "结构检索未完成，安全终止", failure_kind=decision.failure_kind,
                        action="fail_safely", reason=decision.reason[:500])
            state.final_answer = self._finalize(state)
            yield event("FINAL_ANSWER", "分析未完成", answer=state.final_answer, state=state.model_dump(mode="json"))
            return
        self._record_tool(state, "search_schema", schema)
        yield event("TOOL_FINISHED", "相关 Schema 检索完成", tool="search_schema", result=schema.model_dump())
        try:
            relationships = await bounded_transient_retry(lambda: asyncio.to_thread(database.relationships))
        except Exception as exc:
            decision = classify_failure(exc, tool="get_table_relationships")
            failed = ToolResult(success=False, source=selected, error=str(exc), metadata={"failure_kind": decision.failure_kind})
            self._record_tool(state, "get_table_relationships", failed)
            if schema_step:
                schema_step.status = "failed"
                schema_step.error = str(exc)[:500]
            yield event("TOOL_FINISHED", "表关系检索失败", tool="get_table_relationships", result=failed.model_dump())
            yield event("RECOVERY_DECISION", "表关系检索未完成，安全终止", failure_kind=decision.failure_kind,
                        action="fail_safely", reason=decision.reason[:500])
            state.final_answer = self._finalize(state)
            yield event("FINAL_ANSWER", "分析未完成", answer=state.final_answer, state=state.model_dump(mode="json"))
            return
        self._record_tool(state, "get_table_relationships", relationships)
        yield event("TOOL_FINISHED", "表关系检索完成", tool="get_table_relationships", result=relationships.model_dump())
        if schema_step:
            schema_step.observations = [
                {"tool": "search_schema", "success": schema.success, "tables": list(schema.data)},
                {"tool": "get_table_relationships", "success": relationships.success},
            ]
            self._finish_plan_step(schema_step, "相关 Schema 与关系已检索")
            yield event("PLAN_STEP_FINISHED", "Schema 计划步骤完成", step_id=schema_step.step_id, status="completed")
        sql_step = self._start_plan_step(state, "sql-generation") if any(
            step.step_id == "sql-generation" for step in state.plan
        ) else None
        if sql_step:
            yield event("PLAN_STEP_STARTED", "开始生成与校验 SQL", step_id=sql_step.step_id, status="running")
        yield event("TOOL_STARTED", "正在生成结构化 Text-to-SQL", tool="text_to_sql", datasource=selected)
        sql_goal = query
        sql_schema = schema.data
        sql_relationships = relationships.data
        if intent.task_type == "mixed_analysis":
            version_clause = f"数据集版本 {dataset_version} 的" if dataset_version else "各已知数据集版本的"
            sql_goal = (
                f"统计 training_molecules 与 molecules 中{version_clause}训练分子数量，"
                "按 molecules.structure_type 分组；返回 structure_type 和 sample_count。"
                "不要查询文件、预测表或模型指标。"
            )
            coverage_tables = ("training_molecules", "molecules")
            if set(coverage_tables).issubset(schema.data):
                sql_schema = {name: schema.data[name] for name in coverage_tables}
                sql_relationships = [relation for relation in relationships.data
                                     if relation.get("source_table") in coverage_tables
                                     and relation.get("target_table") in coverage_tables]
        primary_skill = next(
            (name for name in state.selected_skills if name != "scientific_result_summary"),
            None,
        )
        skill_context = self.skills.execution_context(state.selected_skills)
        sql_current_step = (
            "training-coverage"
            if intent.task_type == "mixed_analysis"
            else f"skill:{primary_skill}" if primary_skill else "database-analysis"
        )
        candidate, generation = await bounded_transient_retry(lambda: self.text2sql.generate(
            goal=sql_goal,
            current_step=sql_current_step,
            datasource=selected,
            full_schema=sql_schema,
            relationships=sql_relationships,
            dataset_version=dataset_version,
            skill_context=skill_context,
        ))
        generation_result = ToolResult(
            success=True,
            data=candidate.model_dump(),
            source=selected,
            metadata=generation,
        )
        self._record_tool(state, "text_to_sql", generation_result)
        yield event("TOOL_FINISHED", "结构化 SQL 已生成", tool="text_to_sql", result=generation_result.model_dump())
        yield event("TOOL_STARTED", "正在执行 SQL Guard 与 Query Checker", tool="query_checker", datasource=selected)
        repaired = False
        try:
            checked = database.check_query(candidate.sql, candidate.params)
        except Exception as exc:
            decision = classify_failure(exc, tool="query_checker")
            failed_check = ToolResult(success=False, source=selected, error=str(exc), metadata={"failure_kind": decision.failure_kind})
            self._record_tool(state, "query_checker", failed_check)
            if sql_step:
                sql_step.status = "failed"
                sql_step.error = str(exc)[:500]
            yield event("TOOL_FINISHED", "SQL 校验失败", tool="query_checker", result=failed_check.model_dump())
            if decision.action != "replan" or state.replan_count >= MAX_REPLANS:
                yield event("RECOVERY_DECISION", "SQL 校验失败，安全终止", failure_kind=decision.failure_kind,
                            action="fail_safely", reason=decision.reason[:500])
                state.final_answer = self._finalize(state)
                yield event("FINAL_ANSWER", "SQL 校验未通过", answer=state.final_answer, state=state.model_dump(mode="json"))
                return
            original_plan = [step.model_dump(mode="json") for step in state.plan]
            previous_sql = (candidate.sql, tuple(sorted(candidate.params.items())))
            state.replan_count += 1
            state.plan_version += 1
            revised_step = PlanStep(
                step_id=f"sql-repair-{state.replan_count}",
                goal="根据 PostgreSQL 校验错误修复 SQL 列、别名或表引用",
                depends_on=[schema_step.step_id] if schema_step else [],
                required_capabilities=[Capability.DATABASE],
                preferred_tools=["text_to_sql", "query_checker"],
                selected_tools=["text_to_sql", "query_checker"],
                observations=[{"failure_kind": decision.failure_kind, "error": str(exc)[:500]}],
            )
            state.plan.append(revised_step)
            execution_step = next((step for step in state.plan if step.step_id == "sql-execution"), None)
            if execution_step:
                execution_step.depends_on = [revised_step.step_id]
            yield event(
                "PLAN_REVISED", "根据 SQL 校验观察调整计划",
                original_plan=original_plan, observation=failed_check.model_dump(),
                replan_reason=decision.reason[:500],
                revised_plan=[step.model_dump(mode="json") for step in state.plan],
                plan_version=state.plan_version,
            )
            self._start_plan_step(state, revised_step.step_id)
            yield event("PLAN_STEP_STARTED", "开始执行 SQL 修复步骤", step_id=revised_step.step_id, status="running")
            candidate, generation = await bounded_transient_retry(lambda: self.text2sql.generate(
                goal=sql_goal, current_step="sql-repair", datasource=selected,
                full_schema=sql_schema, relationships=sql_relationships,
                dataset_version=dataset_version, repair_feedback=str(exc),
                skill_context=skill_context,
            ))
            if (candidate.sql, tuple(sorted(candidate.params.items()))) == previous_sql:
                revised_step.status = "failed"
                revised_step.error = "duplicate SQLCandidate after replan"
                yield event("PLAN_STEP_FINISHED", "重规划未产生新 SQL", step_id=revised_step.step_id, status="failed", error=revised_step.error)
                state.final_answer = self._finalize(state)
                yield event("FINAL_ANSWER", "SQL 修复未成功", answer=state.final_answer, state=state.model_dump(mode="json"))
                return
            regenerated = ToolResult(success=True, data=candidate.model_dump(), source=selected, metadata=generation)
            self._record_tool(state, "text_to_sql", regenerated)
            yield event("TOOL_FINISHED", "重规划 SQL 已生成", tool="text_to_sql", result=regenerated.model_dump())
            try:
                checked = database.check_query(candidate.sql, candidate.params)
            except Exception as repair_exc:
                revised_step.status = "failed"
                revised_step.error = str(repair_exc)[:500]
                yield event("PLAN_STEP_FINISHED", "重规划 SQL 校验失败", step_id=revised_step.step_id, status="failed", error=revised_step.error)
                state.final_answer = self._finalize(state)
                yield event("FINAL_ANSWER", "SQL 修复未通过校验", answer=state.final_answer, state=state.model_dump(mode="json"))
                return
            failed_check.metadata["recovered"] = True
            repaired = True
        self._record_tool(state, "query_checker", checked)
        yield event("TOOL_FINISHED", "SQL Guard 与 Query Checker 通过", tool="query_checker", result=checked.model_dump())
        if repaired:
            self._finish_plan_step(state.plan[-1], "修复后的 SQL 通过 Query Checker")
            state.plan[-1].observations.append({"query_checker": checked.model_dump()})
            yield event("PLAN_STEP_FINISHED", "重规划 SQL 校验通过", step_id=state.plan[-1].step_id, status="completed")
        elif sql_step:
            sql_step.observations = [{"tool": "text_to_sql", "candidate": candidate.model_dump()},
                                     {"tool": "query_checker", "success": checked.success}]
            self._finish_plan_step(sql_step, "SQLCandidate 已通过 Guard 与 PostgreSQL EXPLAIN")
            yield event("PLAN_STEP_FINISHED", "SQL 计划步骤完成", step_id=sql_step.step_id, status="completed")
        execution_step = self._start_plan_step(state, "sql-execution") if any(
            step.step_id == "sql-execution" for step in state.plan
        ) else None
        if execution_step:
            yield event("PLAN_STEP_STARTED", "开始只读执行计划步骤", step_id=execution_step.step_id, status="running")
        yield event("TOOL_STARTED", "正在执行只读查询", tool="execute_readonly_sql", datasource=selected)
        try:
            coverage = await bounded_transient_retry(
                lambda: asyncio.to_thread(database.execute, candidate.sql, candidate.params)
            )
        except Exception as exc:
            decision = classify_failure(exc, tool="execute_readonly_sql")
            failed = ToolResult(success=False, source=selected, error=str(exc), metadata={"failure_kind": decision.failure_kind})
            self._record_tool(state, "execute_readonly_sql", failed)
            for step in (execution_step, coverage_step):
                if step:
                    step.status = "failed"
                    step.error = str(exc)[:500]
            yield event("TOOL_FINISHED", "只读查询执行失败", tool="execute_readonly_sql", result=failed.model_dump())
            yield event("RECOVERY_DECISION", "只读查询无法完成，安全终止", failure_kind=decision.failure_kind,
                        action="fail_safely", reason=decision.reason[:500])
            state.final_answer = self._finalize(state)
            yield event("FINAL_ANSWER", "分析未完成", answer=state.final_answer, state=state.model_dump(mode="json"))
            return
        call_id = self._record_tool(state, "execute_readonly_sql", coverage)
        active_data_step = execution_step or coverage_step
        if active_data_step:
            active_data_step.observations.append({"tool": "execute_readonly_sql", "success": coverage.success,
                                                  "row_count": len(coverage.data) if isinstance(coverage.data, list) else None})
        yield event("TOOL_FINISHED", "只读查询完成", tool="execute_readonly_sql", result=coverage.model_dump())
        if coverage.success and coverage.data == []:
            yield event(
                "RECOVERY_DECISION", "只读查询为空，安全终止确定性推断",
                failure_kind="empty_result", action="fail_safely",
                reason="0 rows is not evidence of scientific absence; preserve query scope and request clarification in final answer",
                new_tool_calls=0,
            )
        for evidence in self._database_evidence(state, coverage, selected, call_id, dataset_version):
            if state.plan:
                target_step = active_data_step or (state.plan[-1] if repaired else next(
                    (step for step in state.plan if step.step_id == "training-coverage"), state.plan[-1]
                ))
                target_step.evidence_ids.append(evidence.evidence_id)
            yield event("EVIDENCE_ADDED", "已获得新的科研证据", evidence=evidence.model_dump())
        if execution_step:
            self._finish_plan_step(execution_step, "只读 SQL 已执行并形成 Evidence")
            state.current_step = len(state.plan)
            yield event("PLAN_STEP_FINISHED", "只读执行计划步骤完成", step_id=execution_step.step_id, status="completed")
        if coverage_step:
            self._finish_plan_step(coverage_step, "已完成 schema retrieval 与只读 coverage 查询")
            state.current_step = len(state.plan)
            yield event("PLAN_STEP_FINISHED", "计划步骤完成", step_id="training-coverage", status="completed")

    async def stream(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        datasource_id: str | None = None,
        dataset_version: str | None = None,
        skip_hitl: bool = False,
    ) -> AsyncIterator[SSEEvent]:
        async with asyncio.timeout(TASK_TIMEOUT):
            resources = self.resources.discover(user_id, thread_id)
            yield event("UNDERSTANDING_INTENT", "正在理解科研任务", resources=resources.model_dump())
            intent = await self.router.route_async(query, resources)
            explicit_versions = list(dict.fromkeys(
                match.group(0).lower().replace("-", "_")
                for match in re.finditer(r"train[_-]?v\d+", query, re.I)
            ))
            effective_dataset_version = dataset_version or (explicit_versions[0] if len(explicit_versions) == 1 else None)
            candidate_capabilities = {cap.value for cap in intent.required_capabilities}
            if self.storage.configured:
                candidate_capabilities.add("artifact")
            selected_skills, skill_selection_source, skill_selection_telemetry = await self.skills.select_async(
                query,
                intent.task_type,
                candidate_capabilities,
            )
            state = ScientificAgentState(
                user_id=user_id, thread_id=thread_id, goal=query, domain=intent.domain,
                task_type=intent.task_type, complexity=intent.complexity,
                available_files=resources.available_files,
                available_datasources=resources.authorized_datasources,
                available_models=resources.available_scientific_models,
                available_tools=[cap.value for cap in intent.required_capabilities],
                selected_skills=selected_skills,
            )
            yield event(
                "INTENT_RESOLVED",
                "已识别任务类型",
                intent=intent.model_dump(mode="json"),
                selected_skills=state.selected_skills,
                skill_selection_source=skill_selection_source,
                skill_llm_telemetry=skill_selection_telemetry,
            )

            known_filenames = re.findall(r"[\w.-]+\.(?:csv|xlsx|xls)", query, flags=re.I)
            molecule_match = re.search(r"\b((?:M|T)\d{3,})\b", query, flags=re.I)
            deep_mcp_results: dict[str, ToolResult] = {}
            first_tool = {
                "file_analysis": self._preferred_file_tool(query) if intent.complexity == "simple" else "calculate_metrics",
                "mixed_analysis": "calculate_metrics",
                "database_analysis": "search_schema",
                "scientific_model": "predict_rt",
            }.get(intent.task_type)
            step_filter = (
                None
                if intent.task_type == "file_analysis" and intent.complexity == "simple"
                else [first_tool] if first_tool else None
            )
            candidates = self.tool_registry.candidates(
                available_capabilities=candidate_capabilities,
                role="researcher",
                selected_skills=state.selected_skills,
                current_step_tools=step_filter,
            )
            selection_files = known_filenames or (
                list(resources.available_files) if len(resources.available_files) == 1 else []
            )
            argument_context = {
                "query": query,
                "filename": selection_files[0] if len(selection_files) == 1 else None,
                "filenames": selection_files or None,
                "group": "structure_type" if any(word in query.lower() for word in ("结构", "structure", "fused", "cyclic")) else None,
                "datasource_id": datasource_id,
                "dataset_version": effective_dataset_version,
                "molecule_id": molecule_match.group(1).upper() if molecule_match else None,
            }
            argument_context = {key: value for key, value in argument_context.items() if value is not None}
            selected_choice, selection_source, selection_telemetry = await self.tool_registry.select(
                query,
                "initial",
                candidates,
                first_tool,
                argument_context=argument_context,
            )
            yield event(
                "TOOL_CANDIDATES",
                "已完成工具候选过滤",
                **self.tool_registry.routing_trace(
                    candidates,
                    selected_choice.tool if selected_choice else None,
                    selection_source,
                ),
                llm_telemetry=selection_telemetry,
            )

            if intent.task_type == "database_analysis" and "cross_dataset_comparison" in state.selected_skills:
                async for skill_event in self._run_cross_dataset_comparison(state, resources, datasource_id):
                    yield skill_event
                return

            if intent.task_type == "database_analysis" and "覆盖" in query and not effective_dataset_version and not skip_hitl:
                hitl_state = await self.checkpointing.start_hitl(
                    {
                        "query": query,
                        "user_id": user_id,
                        "thread_id": thread_id,
                        "datasource_id": datasource_id,
                        "intent": intent.model_dump(mode="json"),
                        "resources": resources.model_dump(mode="json"),
                        "selected_skills": state.selected_skills,
                    }
                )
                interrupts = hitl_state.get("__interrupt__", [])
                if interrupts:
                    payload = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
                    yield event(
                        "WAITING_FOR_USER",
                        "等待用户补充信息",
                        question=payload.get("question", "请提供 dataset_version"),
                        field="dataset_version",
                        checkpoint="postgres" if self.checkpointing.persistent else "memory",
                    )
                    return

            if intent.need_planning:
                requested_stages, planning_source, planning_telemetry = await self.planning_policy.select_async(
                    intent.model_dump(mode="json"),
                    state.selected_skills,
                )
                graph_state = await asyncio.to_thread(
                    self.planning_graph.invoke,
                    {
                        "intent": intent.model_dump(mode="json"),
                        "requested_stages": requested_stages,
                        "plan": [],
                        "plan_version": 0,
                    },
                    {"configurable": {"thread_id": f"plan:{thread_id}"}},
                )
                state.plan = [PlanStep.model_validate(step) for step in graph_state["plan"]]
                state.plan_version = graph_state["plan_version"]
                yield event(
                    "PLAN_CREATED",
                    "已生成受约束的分析计划",
                    plan=[p.model_dump() for p in state.plan],
                    requested_stages=requested_stages,
                    planning_source=planning_source,
                    llm_telemetry=planning_telemetry,
                    max_replans=MAX_REPLANS,
                )
                # DeepAgents is used only when it has a concrete bounded sub-task:
                # explicit molecule-level MCP enrichment. It is not invoked as a
                # decorative extra hop for ordinary SQL/file workflows.
                if molecule_match and "mcp" in state.available_tools:
                    yield event("TOOL_STARTED", "正在启动 DeepAgents 科研子任务", tool="deepagents_runtime")
                    runtime_trace = await self.deep_runtime.run_scaffold(
                        query, user_id, thread_id, state.selected_skills
                    )
                    state.tool_calls.append({
                        "tool_call_id": f"runtime-{len(state.tool_calls) + 1}",
                        "tool": "deepagents_runtime",
                        "success": True,
                    })
                    yield event(
                        "TOOL_FINISHED",
                        "DeepAgents 科研子任务完成",
                        tool="deepagents_runtime",
                        result=runtime_trace,
                    )
                    for runtime_call in runtime_trace.get("tool_calls", []):
                        if runtime_call.get("tool") != "mcp:get_molecule_features":
                            continue
                        result_payload = runtime_call.get("result")
                        if not isinstance(result_payload, dict):
                            continue
                        mcp_result = ToolResult.model_validate(result_payload)
                        molecule_id = str(runtime_call.get("molecule_id") or molecule_match.group(1)).upper()
                        deep_mcp_results[molecule_id] = mcp_result
                        mcp_call_id = self._record_tool(state, "mcp:get_molecule_features", mcp_result)
                        if mcp_result.success:
                            payload = (
                                mcp_result.data.get("result", mcp_result.data)
                                if isinstance(mcp_result.data, dict) else {}
                            )
                            structure_type = payload.get("structure_type") if isinstance(payload, dict) else None
                            mcp_evidence = self._add_evidence(
                                state,
                                f"{molecule_id} 结构类型",
                                structure_type,
                                "mcp",
                                "mcp:get_molecule_features",
                                mcp_call_id,
                                None,
                            )
                            yield event(
                                "EVIDENCE_ADDED",
                                "DeepAgents 已获得 MCP 科研证据",
                                evidence=mcp_evidence.model_dump(),
                            )
                        else:
                            decision = classify_failure(
                                mcp_result.error or "MCP service unavailable",
                                tool="mcp:get_molecule_features",
                            )
                            state.uncertainties.append(
                                f"DeepAgents MCP 子任务未获得 {molecule_id} 分子特征："
                                f"{mcp_result.error or decision.failure_kind}"
                            )
                            yield event(
                                "RECOVERY_DECISION",
                                "DeepAgents MCP 子任务失败，主计划继续使用其他已验证证据",
                                failure_kind=decision.failure_kind,
                                action=decision.action,
                                reason=decision.reason[:500],
                            )
            execute_step = (
                self._start_plan_step(state, "execute")
                if any(step.step_id == "execute" for step in state.plan) else None
            )
            if execute_step:
                yield event("PLAN_STEP_STARTED", "开始执行计划步骤", step_id="execute", status="running")

            if intent.task_type in {"file_analysis", "mixed_analysis"}:
                names = re.findall(r"[\w.-]+\.(?:csv|xlsx|xls)", query, flags=re.I)
                if not names and len(resources.available_files) == 1:
                    names = [resources.available_files[0]]
                if not names:
                    self.pending[thread_id] = {"query": query, "user_id": user_id, "datasource_id": datasource_id}
                    question = (
                        "当前有多个可用文件，请明确要分析的文件名。"
                        if resources.available_files
                        else "请上传用于分析的 CSV/Excel 文件。"
                    )
                    yield event(
                        "WAITING_FOR_USER",
                        "等待用户补充信息",
                        question=question,
                        missing_files=[],
                        available_files=resources.available_files,
                    )
                    return
                missing = [name for name in names if name not in resources.available_files]
                if missing:
                    self.pending[thread_id] = {"query": query, "user_id": user_id, "datasource_id": datasource_id}
                    yield event("WAITING_FOR_USER", "等待用户补充信息", question=f"请上传缺失文件：{', '.join(missing)}", missing_files=missing)
                    return
                if intent.complexity == "simple":
                    if selected_choice is None:
                        state.uncertainties.append("当前文件任务没有形成可执行且参数完整的 ToolCall")
                        state.final_answer = self._finalize(state)
                        yield event("FINAL_ANSWER", "未形成可执行工具调用", answer=state.final_answer, state=state.model_dump(mode="json"))
                        return
                    # The selected and validated ToolChoice is now the execution authority
                    # for simple file tasks rather than trace-only metadata.
                    context = ToolExecutionContext(
                        user_id=user_id,
                        thread_id=thread_id,
                        resources=resources,
                        datasource_id=datasource_id,
                    )
                    yield event(
                        "TOOL_STARTED",
                        "正在执行已选择的文件工具",
                        tool=selected_choice.tool,
                        arguments=selected_choice.arguments,
                        selection_source=selection_source,
                    )
                    try:
                        result = await self.tool_dispatcher.execute(selected_choice, context)
                    except Exception as exc:
                        result = ToolResult(
                            success=False,
                            source=names[0],
                            error=str(exc),
                            metadata={"error_type": type(exc).__name__, "dispatcher": True},
                        )
                    call_id = self._record_tool(state, selected_choice.tool, result)
                    yield event(
                        "TOOL_FINISHED",
                        "文件工具执行完成",
                        tool=selected_choice.tool,
                        result=result.model_dump(),
                    )
                    if not result.success:
                        state.final_answer = self._finalize(state)
                        yield event("FINAL_ANSWER", "分析未完成", answer=state.final_answer, state=state.model_dump(mode="json"))
                        return
                    path = self._file_path(user_id, thread_id, names[0])
                    evidence_value = (
                        result.data.get("row_count")
                        if selected_choice.tool == "inspect_table" and isinstance(result.data, dict)
                        else result.data
                    )
                    evidence = self._add_evidence(
                        state,
                        f"{names[0]} · {selected_choice.tool} 结果",
                        evidence_value,
                        "file",
                        names[0],
                        call_id,
                        self._file_dataset_version(path),
                    )
                    yield event("EVIDENCE_ADDED", "已获得新的科研证据", evidence=evidence.model_dump())
                else:
                    await asyncio.sleep(0)
                    model_results = {}
                    comparison_step = (
                        self._start_plan_step(state, "file-comparison")
                        if any(step.step_id == "file-comparison" for step in state.plan)
                        else None
                    )
                    if comparison_step:
                        yield event("PLAN_STEP_STARTED", "开始执行计划步骤", step_id="file-comparison", status="running")
                    for name in names[:2]:
                        path = self._file_path(user_id, thread_id, name)
                        yield event("TOOL_STARTED", "正在计算指标", tool="calculate_metrics", source=name)
                        result = self.files.calculate_metrics(path)
                        call_id = self._record_tool(state, "calculate_metrics", result)
                        if comparison_step:
                            comparison_step.observations.append({"tool": "calculate_metrics", "source": name, "success": result.success})
                        yield event("TOOL_FINISHED", "指标计算完成", tool="calculate_metrics", result=result.model_dump())
                        if not result.success:
                            if comparison_step:
                                comparison_step.status = "failed"
                                comparison_step.error = result.error
                            state.final_answer = self._finalize(state)
                            yield event("FINAL_ANSWER", "分析未完成", answer=state.final_answer, state=state.model_dump(mode="json"))
                            return
                        model_results[name] = result.data
                        evidence = self._add_evidence(
                            state, f"{name} 整体 MAE", result.data["mae"], "file", name, call_id,
                            self._file_dataset_version(path),
                        )
                        if comparison_step:
                            comparison_step.evidence_ids.append(evidence.evidence_id)
                        yield event("EVIDENCE_ADDED", "已获得新的科研证据", evidence=evidence.model_dump())

                    if len(names) >= 2:
                        yield event("TOOL_STARTED", "正在按 molecule_id 配对比较模型", tool="compare_models",
                                    sources=names[:2])
                        pairwise = self.files.compare_models([
                            self._file_path(user_id, thread_id, name) for name in names[:2]
                        ])
                        pairwise_call = self._record_tool(state, "compare_models", pairwise)
                        if comparison_step:
                            comparison_step.observations.append({"tool": "compare_models", "success": pairwise.success,
                                                                 "comparable": (pairwise.data or {}).get("comparable") if isinstance(pairwise.data, dict) else None})
                        yield event("TOOL_FINISHED", "配对模型比较完成", tool="compare_models", result=pairwise.model_dump())
                        if not pairwise.success:
                            if comparison_step:
                                comparison_step.status = "failed"
                                comparison_step.error = pairwise.error
                            state.final_answer = self._finalize(state)
                            yield event("FINAL_ANSWER", "模型配对比较失败", answer=state.final_answer,
                                        state=state.model_dump(mode="json"))
                            return
                        if not pairwise.data.get("comparable"):
                            state.uncertainties.append(
                                "两个模型文件的 molecule_id 未完整对齐；不能据独立总体指标推断配对性能改进"
                            )
                        else:
                            paired_evidence = self._add_evidence(
                                state, "按 molecule_id 配对的模型误差比较", pairwise.data, "file",
                                ",".join(names[:2]), pairwise_call,
                                (
                                    "synthetic_demo"
                                    if all(
                                        self._file_dataset_version(self._file_path(user_id, thread_id, name)) == "synthetic_demo"
                                        for name in names[:2]
                                    )
                                    else None
                                ),
                            )
                            if comparison_step:
                                comparison_step.evidence_ids.append(paired_evidence.evidence_id)
                            yield event("EVIDENCE_ADDED", "已获得配对模型比较证据", evidence=paired_evidence.model_dump())

                    wants_table, wants_chart = self._artifact_preferences(query)
                    if self.storage.configured and model_results and (wants_table or wants_chart):
                        if wants_chart:
                            try:
                                chart = self.artifact_service.plot_metric_comparison(
                                    user_id, thread_id, model_results, "metric_comparison.png"
                                )
                                chart_result = ToolResult(success=True, data=chart, source=chart["object_key"])
                                self._record_tool(state, "plot_metric_comparison", chart_result)
                                state.artifacts.append(chart["object_key"])
                                yield event("ARTIFACT_CREATED", "已生成模型指标比较图", artifact=chart)
                            except Exception as exc:
                                failed = ToolResult(
                                    success=False,
                                    source="artifact:metric_comparison.png",
                                    error=str(exc),
                                    metadata={"error_type": type(exc).__name__, "recovered": True},
                                )
                                self._record_tool(state, "plot_metric_comparison", failed)
                                yield event(
                                    "RECOVERY_DECISION",
                                    "图表产物生成失败，继续保留已验证 Evidence",
                                    failure_kind="artifact_failure",
                                    action="continue_without_artifact",
                                    reason=str(exc)[:500],
                                )
                        if wants_table:
                            try:
                                table_rows = [{"model": name, **values} for name, values in model_results.items()]
                                table = self.artifact_service.save_result_table(
                                    user_id, thread_id, table_rows, "model_metrics.csv", "csv",
                                    required_columns={"model", "mae", "rmse"},
                                )
                                table_result = ToolResult(success=True, data=table, source=table["object_key"])
                                self._record_tool(state, "save_result_table", table_result)
                                state.artifacts.append(table["object_key"])
                                yield event("ARTIFACT_CREATED", "已保存模型指标表", artifact=table)
                            except Exception as exc:
                                failed = ToolResult(
                                    success=False,
                                    source="artifact:model_metrics.csv",
                                    error=str(exc),
                                    metadata={"error_type": type(exc).__name__, "recovered": True},
                                )
                                self._record_tool(state, "save_result_table", failed)
                                yield event(
                                    "RECOVERY_DECISION",
                                    "结果表生成失败，继续保留已验证 Evidence",
                                    failure_kind="artifact_failure",
                                    action="continue_without_artifact",
                                    reason=str(exc)[:500],
                                )

                    if comparison_step:
                        self._finish_plan_step(comparison_step, f"已比较 {len(model_results)} 个模型文件")
                        yield event("PLAN_STEP_FINISHED", "计划步骤完成", step_id="file-comparison", status="completed")

                    target_name = names[1] if len(names) > 1 else names[0]
                    target_path = self._file_path(user_id, thread_id, target_name)
                    run_subgroup = any(step.step_id == "subgroup-analysis" for step in state.plan)
                    if run_subgroup:
                        subgroup_step = (
                            self._start_plan_step(state, "subgroup-analysis")
                            if any(step.step_id == "subgroup-analysis" for step in state.plan)
                            else None
                        )
                        if subgroup_step:
                            yield event("PLAN_STEP_STARTED", "开始执行计划步骤", step_id="subgroup-analysis", status="running")
                        yield event("TOOL_STARTED", "正在分析结构子群", tool="group_metrics", source=target_name)
                        subgroup = self.files.group_metrics(target_path)
                        call_id = self._record_tool(state, "group_metrics", subgroup)
                        if subgroup_step:
                            subgroup_step.observations.append({"tool": "group_metrics", "success": subgroup.success})
                        yield event("TOOL_FINISHED", "结构子群分析完成", tool="group_metrics", result=subgroup.model_dump())
                        if not subgroup.success:
                            if subgroup_step:
                                subgroup_step.status = "failed"
                                subgroup_step.error = subgroup.error
                            state.final_answer = self._finalize(state)
                            yield event("FINAL_ANSWER", "分析未完成", answer=state.final_answer, state=state.model_dump(mode="json"))
                            return
                        fused = next((row for row in subgroup.data if str(row["structure_type"]).lower() == "fused_ring"), None)
                        if fused:
                            evidence = self._add_evidence(
                                state, f"{target_name} fused_ring MAE", fused["mae"], "file", target_name, call_id,
                                self._file_dataset_version(target_path),
                            )
                            if subgroup_step:
                                subgroup_step.evidence_ids.append(evidence.evidence_id)
                            yield event("EVIDENCE_ADDED", "已获得新的科研证据", evidence=evidence.model_dump())
                        elif "fused" in query.lower():
                            state.uncertainties.append("文件分析没有返回 fused_ring 结构子群；不能推断其样本量或误差为 0。")
                        if "mcp" in state.available_tools:
                            molecule_match = re.search(r"\b((?:M|T)\d{3,})\b", query, flags=re.I)
                            if molecule_match is None:
                                state.uncertainties.append(
                                    "任务需要 MCP 分子级信息，但没有明确 molecule_id；为避免误查其他分子，未调用 MCP。"
                                )
                            else:
                                molecule_id = molecule_match.group(1).upper()
                                yield event(
                                    "TOOL_STARTED",
                                    "正在调用 Scientific MCP Tool",
                                    tool="get_molecule_features",
                                    transport="stdio",
                                    molecule_id=molecule_id,
                                )
                                molecule = await self.mcp.call("get_molecule_features", {"molecule_id": molecule_id})
                                mcp_call_id = self._record_tool(state, "mcp:get_molecule_features", molecule)
                                yield event(
                                    "TOOL_FINISHED",
                                    "MCP 分子特征已返回",
                                    tool="get_molecule_features",
                                    molecule_id=molecule_id,
                                    result=molecule.model_dump(),
                                )
                                if molecule.success:
                                    payload = molecule.data.get("result", molecule.data) if isinstance(molecule.data, dict) else {}
                                    structure_type = payload.get("structure_type") if isinstance(payload, dict) else None
                                    mcp_evidence = self._add_evidence(
                                        state,
                                        f"{molecule_id} 结构类型",
                                        structure_type,
                                        "mcp",
                                        "mcp:get_molecule_features",
                                        mcp_call_id,
                                        None,
                                    )
                                    yield event("EVIDENCE_ADDED", "已获得 MCP 科研证据", evidence=mcp_evidence.model_dump())
                                    if subgroup_step:
                                        subgroup_step.evidence_ids.append(mcp_evidence.evidence_id)
                                else:
                                    decision = classify_failure(molecule.error or "MCP service unavailable", tool="mcp:get_molecule_features")
                                    if decision.action == "alternative_tool" and subgroup.success:
                                        molecule.metadata["recovered"] = True
                                        molecule.metadata["alternative_tool"] = "group_metrics"
                                        state.uncertainties.append(
                                            f"MCP 分子特征不可用；仅保留文件 group_metrics 子群证据，未验证 {molecule_id} 的单分子结构。"
                                        )
                                    yield event(
                                        "RECOVERY_DECISION",
                                        "MCP 不可用，使用已验证的子群统计作为有限替代",
                                        failure_kind=decision.failure_kind,
                                        action=decision.action,
                                        alternative_tool="group_metrics" if decision.action == "alternative_tool" else None,
                                        reason=decision.reason[:500],
                                    )
                        if subgroup_step:
                            self._finish_plan_step(subgroup_step, "已完成 structure_type 子群误差计算")
                            state.current_step += 1
                            yield event("PLAN_STEP_FINISHED", "计划步骤完成", step_id="subgroup-analysis", status="completed")

            if intent.task_type in {"database_analysis", "mixed_analysis"}:
                async for database_event in self._run_database_branch(
                    state, resources, intent, query, datasource_id, effective_dataset_version
                ):
                    yield database_event
                if state.final_answer is not None:
                    return
                if intent.task_type == "mixed_analysis" and any(
                    step.step_id == "cross-resource-reconcile" for step in state.plan
                ):
                    join_version = effective_dataset_version
                    reconcile_step = self._start_plan_step(state, "cross-resource-reconcile")
                    yield event(
                        "PLAN_STEP_STARTED",
                        "开始核对文件与数据库证据",
                        step_id=reconcile_step.step_id,
                        status="running",
                    )
                    if join_version:
                        async for join_event in self._reconcile_file_database(
                            state, resources, target_path, datasource_id, join_version
                        ):
                            yield join_event
                        self._finish_plan_step(reconcile_step, "已完成文件与版本化数据库的 molecule_id 核对")
                        yield event(
                            "PLAN_STEP_FINISHED",
                            "跨资源核对完成",
                            step_id=reconcile_step.step_id,
                            status="completed",
                        )
                    else:
                        reconcile_step.status = "failed"
                        reconcile_step.error = "dataset_version required for cross-resource reconciliation"
                        state.uncertainties.append("跨资源核对需要明确 dataset_version；当前未执行跨资源关联。")
                        yield event(
                            "PLAN_STEP_FINISHED",
                            "跨资源核对缺少数据集版本",
                            step_id=reconcile_step.step_id,
                            status="failed",
                            error=reconcile_step.error,
                        )

            if intent.task_type == "general":
                state.uncertainties.append("未识别到需要调用的已授权资源")

            if execute_step:
                execute_step.observations = [result.model_dump(mode="json") for result in state.observations]
                execute_step.evidence_ids = [item.evidence_id for item in state.evidence]
                self._finish_plan_step(execute_step, f"执行 {len(state.observations)} 次工具观察，获得 {len(state.evidence)} 条 Evidence")
                yield event("PLAN_STEP_FINISHED", "计划步骤完成", step_id="execute", status="completed")

            state.final_answer = self._finalize(state)
            yield event("FINAL_ANSWER", "分析完成", answer=state.final_answer, state=state.model_dump(mode="json"))

    @staticmethod
    def _evidence_quality_issues(state: ScientificAgentState) -> list[str]:
        issues: list[str] = []
        for result in state.observations:
            if not result.success and not result.metadata.get("recovered"):
                issues.append(f"工具执行失败：{result.error or result.source}")
        if not state.evidence:
            issues.append("没有可用于回答目标问题的 Evidence")
        if state.task_type in {"database_analysis", "mixed_analysis"} and not any(
            item.source_type == "database" for item in state.evidence
        ):
            issues.append("数据库任务缺少已保存的数据库 Evidence")
        if state.task_type in {"database_analysis", "mixed_analysis"}:
            database_rows = [
                row
                for item in state.evidence
                if item.source_type == "database" and isinstance(item.value, list)
                for row in item.value
                if isinstance(row, dict)
            ]
            fields = {str(key).lower() for row in database_rows for key in row}
            goal = state.goal.lower()
            required_fields = {
                field for field in fields
                if field in {"structure_type", "is_fused_ring", "sample_count", "mae", "rmse"}
                or "count" in field or "error" in field
            }
            for field in sorted(required_fields):
                if any(row.get(field) is None for row in database_rows if field in row):
                    issues.append(f"查询结果字段 {field} 包含 NULL，不能用缺失值形成确定性结论")
            if ("结构类型" in goal or "fused" in goal) and database_rows and not (
                {"structure_type", "is_fused_ring"} & fields
            ):
                issues.append("查询结果缺少结构类型字段，无法回答结构分组问题")
            if ("覆盖" in goal or "样本数" in goal) and database_rows and not any(
                "count" in field or "coverage" in field for field in fields
            ):
                issues.append("查询结果缺少样本数或覆盖字段，无法回答训练覆盖问题")
            if ("误差" in goal or "预测表现" in goal) and database_rows and not any(
                "error" in field or "mae" in field or "rmse" in field for field in fields
            ):
                issues.append("查询结果缺少误差指标字段，无法回答预测误差问题")
            if ("不足" in goal or "显著" in goal or "差异" in goal) and database_rows:
                counts = [row.get("sample_count", row.get("train_molecule_count")) for row in database_rows]
                if any(isinstance(count, (int, float)) and count < 5 for count in counts):
                    issues.append("样本量小于 5，不能仅据此形成总体或显著性结论")
            if ("覆盖不足" in goal or "覆盖不够" in goal) and any(
                row.get("sample_count", row.get("train_molecule_count")) == 0 for row in database_rows
            ):
                issues.append("覆盖数为 0 仅是本次查询观测值；未验证查询范围、筛选条件和版本")
        requested_versions = list(re.finditer(r"train[_-]?v\d+", state.goal, flags=re.I))
        if len(requested_versions) == 1:
            requested_version = requested_versions[0]
            expected = requested_version.group(0).replace("-", "_").lower()
            for item in state.evidence:
                if item.source_type == "database" and item.dataset_version and item.dataset_version.lower() != expected:
                    issues.append(f"Evidence {item.evidence_id} 数据版本 {item.dataset_version} 与目标 {expected} 不一致")
        observed: dict[tuple[str, str, str | None], str] = {}
        for item in state.evidence:
            key = (item.claim, item.source, item.dataset_version)
            value = repr(item.value)
            if key in observed and observed[key] != value:
                issues.append(f"Evidence 对同一来源、版本和结论 {item.claim} 的数值相互矛盾")
            observed[key] = value
        for result in state.observations:
            if isinstance(result.data, list) and result.metadata.get("max_rows"):
                if len(result.data) >= int(result.metadata["max_rows"]):
                    issues.append("查询达到行数上限，可能只返回部分结果")
        for item in state.evidence:
            missing = [
                name
                for name, value in {
                    "claim": item.claim,
                    "source": item.source,
                    "tool_call_id": item.tool_call_id,
                }.items()
                if not value
            ]
            if missing:
                issues.append(f"Evidence {item.evidence_id} 缺少必需字段：{', '.join(missing)}")
        issues.extend(state.uncertainties)
        return list(dict.fromkeys(issues))

    @staticmethod
    def _format_evidence_value(value) -> str:
        if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
            columns: list[str] = []
            for row in value[:20]:
                for key in row:
                    if key not in columns:
                        columns.append(str(key))
            columns = columns[:8]
            if columns:
                lines = [
                    "| " + " | ".join(columns) + " |",
                    "| " + " | ".join("---" for _ in columns) + " |",
                ]
                for row in value[:8]:
                    lines.append(
                        "| "
                        + " | ".join(str(row.get(column, "")).replace("|", "\\|") for column in columns)
                        + " |"
                    )
                if len(value) > 8:
                    lines.append(f"\n（共 {len(value)} 行，这里展示前 8 行）")
                return "\n".join(lines)
        if isinstance(value, dict):
            return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, default=str)[:4000] + "\n```"
        if isinstance(value, list):
            preview = value[:20]
            suffix = f" …（共 {len(value)} 项）" if len(value) > 20 else ""
            return f"{preview}{suffix}"
        return str(value)

    def _finalize(self, state: ScientificAgentState) -> str:
        state.claims = []
        quality_issues = self._evidence_quality_issues(state)
        state.quality_issues = quality_issues
        if any(not result.success and not result.metadata.get("recovered") for result in state.observations):
            state.quality_status = "EXECUTION_FAILED"
        elif any("相互矛盾" in issue for issue in quality_issues):
            state.quality_status = "CONFLICTING_EVIDENCE"
        elif not state.evidence:
            state.quality_status = "NO_DATA"
        elif quality_issues:
            state.quality_status = "INSUFFICIENT_EVIDENCE"
        else:
            state.quality_status = "SUPPORTED_CONCLUSION"

        if quality_issues:
            heading = {
                "EXECUTION_FAILED": "执行未完成",
                "CONFLICTING_EVIDENCE": "证据存在冲突",
                "NO_DATA": "没有可用数据",
                "INSUFFICIENT_EVIDENCE": "证据不足",
            }.get(state.quality_status, "当前无法形成可靠结论")
            lines = [
                state.quality_status,
                "",
                f"## {heading}",
                "",
                "我不能基于当前记录给出确定性科研结论，原因是：",
                *[f"- {issue}" for issue in quality_issues],
            ]
            if any("0 rows" in issue for issue in quality_issues):
                lines.append("- 0 rows 只表示本次查询在当前范围内没有返回记录，不等价于科学上不存在。")
            if state.evidence:
                lines += ["", "**已经获得但不足以单独支撑结论的证据**"]
                for item in state.evidence[:6]:
                    lines.append(
                        f"- `{item.evidence_id}` {item.claim}（来源：{item.source}"
                        + (f"；版本：{item.dataset_version}" if item.dataset_version else "")
                        + "）"
                    )
            return "\n".join(lines)

        lines = ["## 分析结论", "", "**证据**"]
        for item in state.evidence:
            formatted = self._format_evidence_value(item.value)
            claim_text = f"{item.claim}：{item.value}"
            linked_ids = [item.evidence_id]
            if item.claim == "文件与训练集按 molecule_id 关联核对":
                linked_ids = [
                    prior.evidence_id
                    for prior in state.evidence
                    if prior.claim in {
                        "用于跨资源关联的文件分子与结构类型",
                        "文件分子在版本化训练集中的原始关联行",
                        item.claim,
                    }
                ]
            state.claims.append(GroundedClaim(text=claim_text, evidence_ids=linked_ids))
            version = f"；版本：{item.dataset_version}" if item.dataset_version else ""
            lines += [
                "",
                f"**{item.claim}**",
                formatted,
                f"来源：`{item.source}`{version}；Evidence `{item.evidence_id}`；ToolCall `{item.tool_call_id}`",
            ]

        lines += ["", "**解释**"]
        fused_error = next((e for e in state.evidence if "fused_ring MAE" in e.claim), None)
        coverage = next((e for e in state.evidence if "覆盖数" in e.claim), None)
        if fused_error and coverage:
            interpretation = (
                f"当前观测到 fused-ring 误差证据为 {fused_error.value}，"
                f"对应覆盖数为 {coverage.value}。这两项可以用于发现值得检查的关联，"
                "但不能仅凭它们证明训练覆盖导致了误差差异。"
            )
            state.claims.append(
                GroundedClaim(
                    text=interpretation,
                    evidence_ids=[fused_error.evidence_id, coverage.evidence_id],
                    category="interpretation",
                )
            )
            lines.append(
                f"- {interpretation}（Evidence `{fused_error.evidence_id}`、`{coverage.evidence_id}`）"
            )
        elif state.task_type == "database_analysis":
            lines.append("- 上述结论只基于当前授权数据库查询返回的观测值；不把相关性或计数自动解释为因果。")
        elif state.task_type == "file_analysis":
            lines.append("- 上述数值由文件分析工具确定性计算得到；没有使用语言模型进行数值心算。")
        elif state.task_type == "mixed_analysis":
            lines.append("- 文件侧与数据库侧证据分别保留来源；只有在稳定关联键核对成功时才做跨资源对应解释。")
        else:
            lines.append("- 结论仅使用上面列出的已记录 Evidence。")

        synthetic = [
            item for item in state.evidence
            if item.dataset_version and item.dataset_version.startswith("synthetic_demo")
        ]
        if synthetic:
            lines += [
                "",
                "**数据说明**",
                "- 当前结果包含明确标记为 synthetic demo 的 Evidence，只用于验证执行链路，不能外推为真实实验结论。",
            ]
        return "\n".join(lines)

    async def resume(self, thread_id: str, answer: str, user_id: str | None = None) -> AsyncIterator[SSEEvent]:
        if self.checkpointing.checkpoint_exists(thread_id):
            try:
                resumed = await self.checkpointing.resume_hitl(thread_id, answer)
            except Exception as exc:
                yield event("ERROR", "检查点恢复失败", error=str(exc), thread_id=thread_id)
                return
            if resumed.get("status") == "ready":
                yield event(
                    "PLAN_STEP_STARTED",
                    "已从持久检查点恢复",
                    step_id="training-coverage",
                    dataset_version=resumed["dataset_version"],
                )
                intent = RequestIntent.model_validate(resumed["intent"])
                resources = ResourceSummary.model_validate(resumed["resources"])
                resumed_user = resumed.get("user_id") or user_id or "unknown"
                state = ScientificAgentState(
                    user_id=resumed_user,
                    thread_id=thread_id,
                    goal=resumed["query"],
                    domain=intent.domain,
                    task_type=intent.task_type,
                    complexity=intent.complexity,
                    available_files=resources.available_files,
                    available_datasources=resources.authorized_datasources,
                    available_models=resources.available_scientific_models,
                    available_tools=[cap.value for cap in intent.required_capabilities],
                    selected_skills=resumed.get("selected_skills", []),
                )
                async for item in self._run_database_branch(
                    state,
                    resources,
                    intent,
                    resumed["query"],
                    resumed.get("datasource_id"),
                    resumed["dataset_version"],
                ):
                    yield item
                if state.final_answer is not None:
                    return
                state.final_answer = self._finalize(state)
                yield event("FINAL_ANSWER", "分析完成", answer=state.final_answer, state=state.model_dump(mode="json"))
                return
        pending = self.pending.pop(thread_id, None)
        if not pending:
            yield event("ERROR", "没有可恢复的任务", thread_id=thread_id)
            return
        query = f"{pending['query']}\n用户补充：{answer}"
        async for item in self.stream(query, pending["user_id"], thread_id, pending.get("datasource_id")):
            yield item

