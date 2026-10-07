from __future__ import annotations

import re
from typing import Any


REQUIRED_SCHEMA = {
    "training_molecules": {"dataset_version", "molecule_id"},
    "molecules": {"molecule_id", "structure_type"},
}

SQL = (
    "SELECT tm.dataset_version, m.structure_type, COUNT(*) AS sample_count "
    "FROM training_molecules AS tm "
    "JOIN molecules AS m ON m.molecule_id = tm.molecule_id "
    "WHERE tm.dataset_version IN (%(version_a)s, %(version_b)s) "
    "GROUP BY tm.dataset_version, m.structure_type "
    "ORDER BY tm.dataset_version, m.structure_type"
)


def parse_versions(query: str) -> tuple[str, str] | None:
    versions = list(dict.fromkeys(
        match.group(0).lower().replace("-", "_")
        for match in re.finditer(r"train[_-]?v\d+", query, re.I)
    ))
    return (versions[0], versions[1]) if len(versions) == 2 else None


def missing_columns(schema: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    missing: dict[str, list[str]] = {}
    for table, required in REQUIRED_SCHEMA.items():
        present = {str(column.get("name", "")) for column in schema.get(table, [])}
        absent = sorted(required - present)
        if absent:
            missing[table] = absent
    return missing


def compare_rows(rows: list[dict[str, Any]], versions: tuple[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
    issues: list[str] = []
    seen = {str(row.get("dataset_version")) for row in rows}
    for version in versions:
        if version not in seen:
            issues.append(f"版本 {version} 没有返回记录；不能推断其在完整数据源中不存在")
    if issues:
        return [], issues
    groups: dict[tuple[str, str], int] = {}
    for row in rows:
        version = str(row.get("dataset_version"))
        structure = row.get("structure_type")
        count = row.get("sample_count")
        if version not in versions or not isinstance(structure, str) or not isinstance(count, int) or count < 0:
            return [], ["查询结果缺少有效的版本、结构类型或非负整数计数"]
        key = (version, structure)
        if key in groups:
            return [], ["查询结果存在重复的版本/结构分组"]
        groups[key] = count
    totals = {version: sum(count for (found_version, _), count in groups.items() if found_version == version)
              for version in versions}
    return [
        {
            "structure_type": structure,
            versions[0]: groups.get((versions[0], structure), 0),
            versions[1]: groups.get((versions[1], structure), 0),
            f"{versions[0]}_total": totals[versions[0]],
            f"{versions[1]}_total": totals[versions[1]],
            f"{versions[0]}_share": round(groups.get((versions[0], structure), 0) / totals[versions[0]], 6) if totals[versions[0]] else None,
            f"{versions[1]}_share": round(groups.get((versions[1], structure), 0) / totals[versions[1]], 6) if totals[versions[1]] else None,
            "delta_second_minus_first": groups.get((versions[1], structure), 0) - groups.get((versions[0], structure), 0),
        }
        for structure in sorted({key[1] for key in groups})
    ], []
