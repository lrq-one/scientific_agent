from __future__ import annotations

from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph


class PlanningState(TypedDict, total=False):
    intent: dict[str, Any]
    requested_stages: list[str]
    plan: list[dict[str, Any]]
    plan_version: int


def canonical_plan(intent: dict[str, Any]) -> list[dict[str, Any]]:
    task_type = intent["task_type"]
    complexity = intent.get("complexity", "simple")
    if task_type == "mixed_analysis":
        return [
            {
                "step_id": "file-comparison",
                "goal": "比较文件中的模型或结果指标",
                "required_capabilities": ["file"],
                "preferred_tools": ["calculate_metrics", "compare_models"],
                "selected_tools": ["calculate_metrics", "compare_models"],
            },
            {
                "step_id": "subgroup-analysis",
                "goal": "分析结构或数据子群差异",
                "depends_on": ["file-comparison"],
                "required_capabilities": ["file"],
                "preferred_tools": ["group_metrics", "find_high_error_samples"],
                "selected_tools": ["group_metrics", "find_high_error_samples"],
            },
            {
                "step_id": "training-coverage",
                "goal": "查询训练数据覆盖或数据库证据",
                "depends_on": ["file-comparison"],
                "required_capabilities": ["database"],
                "preferred_tools": [
                    "search_schema",
                    "get_table_relationships",
                    "text_to_sql",
                    "query_checker",
                    "execute_readonly_sql",
                ],
                "selected_tools": [
                    "search_schema",
                    "get_table_relationships",
                    "text_to_sql",
                    "query_checker",
                    "execute_readonly_sql",
                ],
            },
            {
                "step_id": "cross-resource-reconcile",
                "goal": "按稳定分子标识核对文件结果与版本化数据库证据",
                "depends_on": ["file-comparison", "training-coverage"],
                "required_capabilities": ["file", "database"],
                "preferred_tools": ["join_tables", "execute_readonly_sql"],
                "selected_tools": ["join_tables", "execute_readonly_sql"],
            },
        ]
    if task_type == "file_analysis" and complexity == "complex":
        return [
            {
                "step_id": "file-comparison",
                "goal": "比较文件中的模型或结果指标",
                "required_capabilities": ["file"],
                "preferred_tools": ["calculate_metrics", "compare_models"],
                "selected_tools": ["calculate_metrics", "compare_models"],
            },
            {
                "step_id": "subgroup-analysis",
                "goal": "检查结构子群或高误差样本",
                "depends_on": ["file-comparison"],
                "required_capabilities": ["file"],
                "preferred_tools": ["group_metrics", "find_high_error_samples"],
                "selected_tools": ["group_metrics", "find_high_error_samples"],
            },
        ]
    if task_type == "database_analysis":
        return [
            {
                "step_id": "schema-retrieval",
                "goal": "检索已授权表与关系",
                "required_capabilities": ["database"],
                "preferred_tools": ["search_schema", "get_table_relationships"],
                "selected_tools": ["search_schema", "get_table_relationships"],
            },
            {
                "step_id": "sql-generation",
                "goal": f"针对目标生成并校验 SQL：{intent['goal'][:160]}",
                "depends_on": ["schema-retrieval"],
                "required_capabilities": ["database"],
                "preferred_tools": ["text_to_sql", "query_checker"],
                "selected_tools": ["text_to_sql", "query_checker"],
            },
            {
                "step_id": "sql-execution",
                "goal": "执行只读 SQL 并形成 Evidence",
                "depends_on": ["sql-generation"],
                "required_capabilities": ["database"],
                "preferred_tools": ["execute_readonly_sql"],
                "selected_tools": ["execute_readonly_sql"],
            },
        ]
    tools = {
        "file_analysis": ["inspect_table", "calculate_metrics", "group_metrics"],
        "scientific_model": ["predict_rt"],
    }.get(task_type, [])
    return [
        {
            "step_id": "execute",
            "goal": intent["goal"],
            "required_capabilities": intent.get("required_capabilities", []),
            "preferred_tools": tools,
            "selected_tools": tools,
        }
    ]


def mandatory_stage_ids(intent: dict[str, Any]) -> set[str]:
    task_type = intent["task_type"]
    if task_type == "database_analysis":
        return {"schema-retrieval", "sql-generation", "sql-execution"}
    if task_type == "mixed_analysis":
        return {"file-comparison", "training-coverage"}
    if task_type == "file_analysis" and intent.get("complexity") == "complex":
        return {"file-comparison"}
    return {"execute"}


def _dependency_closure(plan: list[dict[str, Any]], requested: set[str]) -> set[str]:
    by_id = {step["step_id"]: step for step in plan}
    keep = set(requested)
    changed = True
    while changed:
        changed = False
        for step_id in list(keep):
            for dependency in by_id.get(step_id, {}).get("depends_on", []):
                if dependency not in keep:
                    keep.add(dependency)
                    changed = True
    return keep


def create_plan(state: PlanningState) -> dict[str, Any]:
    intent = state["intent"]
    full = canonical_plan(intent)
    available = {step["step_id"] for step in full}
    requested = {item for item in state.get("requested_stages", []) if item in available}
    requested |= mandatory_stage_ids(intent)
    requested = _dependency_closure(full, requested)
    plan = [step for step in full if step["step_id"] in requested]
    return {"plan": plan, "plan_version": state.get("plan_version", 0) + 1}


def build_planning_graph(checkpointer=None):
    builder = StateGraph(PlanningState)
    builder.add_node("create_plan", create_plan)
    builder.add_edge(START, "create_plan")
    builder.add_edge("create_plan", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())
