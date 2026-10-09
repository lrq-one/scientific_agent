"""Deterministic goal/evidence coverage checks; no routing or LLM calls."""
from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp

from app.models.schemas import GoalCoverage
from app.services.query_scope import missing_population_coverage


DATA_TOOLS = {"execute_readonly_sql", "read_csv", "read_excel", "filter_samples",
              "calculate_metrics", "group_metrics", "find_high_error_samples",
              "compare_models", "compare_structure_groups", "join_tables"}
ARTIFACT_TOOLS = {"save_result_table", "save_chart", "plot_metric_comparison"}


def _group_columns(sql: str) -> set[str]:
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except (sqlglot.errors.ParseError, ValueError):
        return set()
    group = tree.args.get("group")
    if not group:
        return set()
    dimensions = set()
    for expression in group.expressions:
        if isinstance(expression, exp.Literal) and not expression.is_string and str(expression.this).isdigit():
            position = int(expression.this) - 1
            if 0 <= position < len(tree.selects):
                expression = tree.selects[position]
        elif isinstance(expression, exp.Column) and not expression.table:
            expression = next((projection for projection in tree.selects
                               if projection.alias == expression.name), expression)
        if isinstance(expression, exp.Alias):
            expression = expression.this
        if isinstance(expression, exp.Column):
            dimensions.add(expression.name)
    return dimensions


def _row_columns(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(key) for row in value if isinstance(row, dict) for key in row}
    if isinstance(value, dict):
        return {str(key) for key in value} | set().union(*(_row_columns(item) for item in value.values()))
    return set()


def _requested_dimensions(state) -> list[str]:
    dimensions = list(state.query_scope.grouping)
    question = state.user_request or state.goal
    columns = {column["name"] for table in state.schema_cache.values()
               for column in table if column.get("name")}
    mentioned = {column for column in columns
                 if re.search(r"(?<![A-Za-z0-9_])" + re.escape(column) + r"(?![A-Za-z0-9_])", question)}
    for hint in state.requested_dimensions:
        if hint in columns or hint in dimensions:
            dimensions.append(hint)
            continue
        # Output descriptions/aggregate labels are not column identifiers. Only
        # resolve one explicitly user-named schema field embedded in such a
        # label, independently of whether the executed SQL actually covers it.
        matches = [column for column in mentioned
                   if re.search(r"(?<![A-Za-z0-9])" + re.escape(column) + r"(?![A-Za-z0-9])", hint)]
        dimensions.append(matches[0] if len(matches) == 1 else hint)
    # Preserve an explicit schema-like categorical field even when it was not
    # available before schema retrieval. This makes replacement equivalence a
    # fact to verify rather than an LLM assertion.
    identifiers = re.findall(r"\b[A-Za-z][A-Za-z0-9_]*(?:_type|_category|_class)\b", question)
    dimensions.extend(identifiers)
    return list(dict.fromkeys(dimensions))


def _explicit_csv_export_requested(question: str) -> bool:
    """Check the user's literal deliverable, not an LLM-authored Plan hint."""
    return any(re.search(pattern, question, re.I) for pattern in (
        r"(?:导出|生成|保存|输出|下载|写入|export|download|save)[^。！？\\n]{0,50}csv",
        r"csv[^。！？\\n]{0,30}(?:文件|导出|生成|保存|下载|file|export|download)",
    ))


def assess_goal_coverage(state, *, non_empirical: bool = False) -> GoalCoverage:
    if non_empirical:
        return GoalCoverage(status="SATISFIED", reason="non-empirical answer basis")

    required = _requested_dimensions(state)
    observed: set[str] = set()
    successful_data = []
    executed_sql = []
    for call, result in zip(state.tool_calls, state.observations):
        if not result.success or call.get("tool") not in DATA_TOOLS:
            continue
        successful_data.append(result)
        observed.update(_row_columns(result.data))
        sql = str(result.metadata.get("sql") or "")
        observed.update(_group_columns(sql))
        if call.get("tool") == "execute_readonly_sql":
            executed_sql.append(result)

    missing_dimensions = []
    for dimension in required:
        if dimension in observed:
            continue
        # Similar spelling/types do not establish semantic equivalence.
        missing_dimensions.append(dimension)

    missing_populations = missing_population_coverage(state, executed_sql) if state.populations or state.requires_population_binding or executed_sql else []
    requested_artifact = any((step.selected_tools or step.preferred_tools) and
                             set(step.selected_tools or step.preferred_tools) & ARTIFACT_TOOLS
                             for step in state.plan)
    requested_artifact = requested_artifact or "artifact" in state.required_deliverables
    missing_deliverables = ["artifact"] if requested_artifact and not state.artifacts else []
    # A CSV explicitly requested in the original user goal cannot disappear
    # because a model omitted it from Plan/required_deliverables. A chart or
    # a generated filename is not proof that the requested CSV was saved.
    if _explicit_csv_export_requested(state.user_request or state.goal):
        persisted_csv = any(
            call.get("tool") == "save_result_table" and result.success
            and isinstance(result.data, dict)
            and result.data.get("artifact_type") == "csv"
            and result.data.get("artifact_id") in state.artifacts
            for call, result in zip(state.tool_calls, state.observations)
        )
        if not persisted_csv:
            missing_deliverables.append("csv_export")
    required_capabilities = {cap.value for step in state.plan for cap in step.required_capabilities}
    required_capabilities.update(item.removesuffix("_analysis") for item in state.required_deliverables
                                 if item.endswith("_analysis"))
    if state.resource_binding.datasource_id and state.resource_binding.files:
        required_capabilities.update({"database", "file"})
    if "database" in required_capabilities and not executed_sql:
        missing_deliverables.append("executed_database_analysis")
    if state.datasource_id and executed_sql and any(result.source != state.datasource_id for result in executed_sql):
        missing_deliverables.append("datasource:" + state.datasource_id)
    if "file" in required_capabilities and not any(item.source_type == "file" for item in state.evidence):
        missing_deliverables.append("file_analysis")
    nonempty = [result for result in successful_data if result.data not in (None, []) and not (
        isinstance(result.data, dict) and result.data.get("comparable") is False)]

    if not successful_data:
        status = "UNSATISFIED" if state.errors or state.blocking_issues else "UNVERIFIABLE"
        reason = "no successful empirical result"
    elif not nonempty:
        status, reason = "UNVERIFIABLE", "successful execution returned no rows/data"
    elif missing_dimensions or missing_populations or missing_deliverables:
        status, reason = "PARTIAL", "persisted evidence does not cover every required goal component"
    else:
        status, reason = "SATISFIED", "all deterministically checkable goal components are covered"
    return GoalCoverage(status=status, required_dimensions=required,
                        observed_dimensions=sorted(observed), missing_dimensions=missing_dimensions,
                        missing_populations=missing_populations, missing_deliverables=missing_deliverables,
                        reason=reason)
