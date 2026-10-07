from __future__ import annotations

from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph


class PlanningState(TypedDict):
    intent: dict[str, Any]
    plan: list[dict[str, Any]]
    plan_version: int


def create_plan(state: PlanningState) -> dict[str, Any]:
    intent = state["intent"]
    if intent["task_type"] == "mixed_analysis":
        plan = [
            {"step_id": "file-comparison", "goal": "比较模型整体指标", "required_capabilities": ["file"],
             "preferred_tools": ["calculate_metrics"], "selected_tools": ["calculate_metrics"]},
            {"step_id": "subgroup-analysis", "goal": "分析结构子群误差", "depends_on": ["file-comparison"],
             "required_capabilities": ["file"], "preferred_tools": ["group_metrics"], "selected_tools": ["group_metrics"]},
            {"step_id": "training-coverage", "goal": "查询训练数据覆盖", "depends_on": ["subgroup-analysis"],
             "required_capabilities": ["database"],
             "preferred_tools": ["schema_retrieval", "text_to_sql", "query_checker", "execute_readonly_sql"],
             "selected_tools": ["schema_retrieval", "text_to_sql", "query_checker", "execute_readonly_sql"]},
        ]
    elif intent["task_type"] == "database_analysis":
        plan = [
            {"step_id": "schema-retrieval", "goal": "检索已授权表与关系", "required_capabilities": ["database"],
             "preferred_tools": ["schema_retrieval", "schema_relationships"],
             "selected_tools": ["schema_retrieval", "schema_relationships"]},
            {"step_id": "sql-generation", "goal": f"针对目标生成并校验 SQL：{intent['goal'][:160]}",
             "depends_on": ["schema-retrieval"], "required_capabilities": ["database"],
             "preferred_tools": ["text_to_sql", "query_checker"],
             "selected_tools": ["text_to_sql", "query_checker"]},
            {"step_id": "sql-execution", "goal": "执行只读 SQL 并形成 Evidence",
             "depends_on": ["sql-generation"], "required_capabilities": ["database"],
             "preferred_tools": ["execute_readonly_sql"], "selected_tools": ["execute_readonly_sql"]},
        ]
    else:
        tools = {"database_analysis": ["schema_retrieval", "text_to_sql", "query_checker", "execute_readonly_sql"],
                 "file_analysis": ["inspect_columns", "calculate_metrics", "group_metrics"],
                 "scientific_model": ["predict_rt"]}.get(intent["task_type"], [])
        plan = [{"step_id": "execute", "goal": intent["goal"],
                 "required_capabilities": intent.get("required_capabilities", []),
                 "preferred_tools": tools, "selected_tools": tools}]
    return {"plan": plan, "plan_version": state.get("plan_version", 0) + 1}


def build_planning_graph(checkpointer=None):
    builder = StateGraph(PlanningState)
    builder.add_node("create_plan", create_plan)
    builder.add_edge(START, "create_plan")
    builder.add_edge("create_plan", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())

