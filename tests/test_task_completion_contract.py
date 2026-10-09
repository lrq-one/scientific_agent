"""Offline regression for scientific completion, exports and visible status."""
from __future__ import annotations

import pytest

from app.agents.goal_coverage import assess_goal_coverage, _explicit_csv_export_requested
from app.models.schemas import ScientificAgentState, ToolResult
from app.services.task_completion import scientific_task_status, RUNTIME_PROTOCOL_VERSION


@pytest.mark.parametrize(("quality", "coverage", "expected"), [
    ("EXECUTION_FAILED", "UNSATISFIED", "failed"),
    ("INSUFFICIENT_EVIDENCE", "PARTIAL", "failed"),
    ("CONFLICTING_EVIDENCE", "UNSATISFIED", "failed"),
    ("SUPPORTED_CONCLUSION", "PARTIAL", "failed"),
    ("SUPPORTED_CONCLUSION", "UNVERIFIABLE", "failed"),
    ("SUPPORTED_CONCLUSION", "SATISFIED", "completed"),
    ("NO_DATA", "UNVERIFIABLE", "completed"),
])
def test_final_answer_alone_never_makes_incomplete_research_success(quality, coverage, expected):
    payload = {"state": {"quality_status": quality, "goal_coverage": {"status": coverage}}}
    assert scientific_task_status(payload) == expected


def test_old_minimal_final_answer_payload_retains_backwards_compatibility():
    assert scientific_task_status({"answer": "plain response"}) == "completed"
    assert RUNTIME_PROTOCOL_VERSION.startswith("2026-10-09-plan-schema")


@pytest.mark.parametrize(("question","expected"),[
    ("将实际结果导出 CSV。", True),
    ("把真实结果保存为csv文件", True),
    ("Export results to CSV", True),
    ("比较 CSV 文件 model_v1.csv 和 model_v2.csv 的误差", False),
    ("只是查看CSV数据来源，不需要导出", False),
    ("计算 MAE，不保存结果", False),
])
def test_explicit_csv_export_intent_is_not_conflated_with_input_file(question,expected):
    assert bool(_explicit_csv_export_requested(question)) is expected


def _analysis_state(goal):
    state = ScientificAgentState(user_id="unit", thread_id="unit", goal=goal)
    state.tool_calls = [{"tool": "execute_readonly_sql", "tool_call_id": "sql-1"}]
    state.observations = [ToolResult(
        success=True, source="training_db", data=[
            {"structure_type": "fused_ring", "mae": 12.5, "run_name": "baseline-run"},
            {"structure_type": "fused_ring", "mae": 11.5, "run_name": "candidate-run"},
        ],
    )]
    return state


def test_missing_csv_is_detected_even_when_llm_omits_deliverable_and_plan():
    state = _analysis_state("比较 baseline-run 与 candidate-run 的 MAE，将实际结果导出 CSV。")
    coverage = assess_goal_coverage(state)
    assert coverage.status == "PARTIAL"
    assert "csv_export" in coverage.missing_deliverables


def test_unrelated_chart_artifact_does_not_satisfy_csv_export():
    state = _analysis_state("对比 MAE 并导出 CSV")
    state.artifacts = ["chart-1"]
    state.tool_calls.append({"tool": "save_chart", "tool_call_id": "chart-call"})
    state.observations.append(ToolResult(
        success=True, source="minio", data={"artifact_type": "chart", "artifact_id": "chart-1"}
    ))
    assert "csv_export" in assess_goal_coverage(state).missing_deliverables


def test_confirmed_csv_save_satisfies_explicit_csv_deliverable():
    state = _analysis_state("对比 MAE 并导出 CSV")
    state.artifacts.append("csv-1")
    state.tool_calls.append({"tool": "save_result_table", "tool_call_id": "csv-call"})
    state.observations.append(ToolResult(
        success=True, source="minio", data={
            "artifact_type": "csv", "artifact_id": "csv-1",
            "filename": "actual_mae.csv",
        },
    ))
    coverage = assess_goal_coverage(state)
    assert "csv_export" not in coverage.missing_deliverables
    assert coverage.status == "SATISFIED"


def test_csv_name_without_confirmed_uploaded_artifact_does_not_count():
    state = _analysis_state("输出 CSV 文件")
    state.tool_calls.append({"tool": "save_result_table", "tool_call_id": "csv-call"})
    state.observations.append(ToolResult(
        success=True, source="minio", data={"artifact_type": "csv", "artifact_id": "not-persisted"},
    ))
    assert "csv_export" in assess_goal_coverage(state).missing_deliverables
