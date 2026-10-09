"""Offline regression for scientific completion, exports and visible status."""
from __future__ import annotations

import pytest

from app.agents.goal_coverage import assess_goal_coverage, _explicit_csv_export_requested
from app.models.schemas import Capability, Evidence, PlanStep, ResourceBinding, ScientificAgentState, ToolResult
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


def test_discovered_database_does_not_create_phantom_deliverable_for_file_only_goal():
    """Replays the completion contract of the persisted model_v1/v2 failure.

    Merely discovering training_db next to the two named files must not turn a
    completed file comparison into an unrequested mixed analysis.
    """
    state = ScientificAgentState(
        user_id="unit", thread_id="unit",
        goal="比较 model_v1.csv 和 model_v2.csv，并按 structure_type 分析高误差样本",
        user_request="比较 model_v1.csv 和 model_v2.csv，并按 structure_type 分析高误差样本",
        requested_dimensions=["structure_type"],
        required_deliverables=["file_analysis"],
        resource_binding=ResourceBinding(
            datasource_id="training_db", files=["model_v1.csv", "model_v2.csv"],
            ambiguous_fields=["dataset_version"],
        ),
        plan=[PlanStep(
            step_id="1", goal="compare named files by structure type",
            selected_tools=["compare_structure_groups"],
            required_capabilities=[Capability.FILE],
        )],
        tool_calls=[{"tool": "compare_structure_groups", "tool_call_id": "file-1"}],
        observations=[ToolResult(success=True, source="model_v2.csv", data={
            "subgroups": [{"structure_type": "fused_ring", "mae": 2.4}],
        })],
        evidence=[Evidence(
            evidence_id="ev-file-1", claim="grouped file errors",
            value={"subgroups": [{"structure_type": "fused_ring", "mae": 2.4}]},
            source_type="file", source="model_v2.csv", tool_call_id="file-1",
        )],
    )
    coverage = assess_goal_coverage(state)
    assert coverage.status == "SATISFIED"
    assert coverage.missing_deliverables == []
    assert scientific_task_status({"state": {
        "quality_status": "SUPPORTED_CONCLUSION",
        "goal_coverage": coverage.model_dump(mode="json"),
    }}) == "completed"


def test_real_mixed_plan_still_requires_executed_database_analysis():
    """Counterfactual: removing the real DB execution must still fail."""
    state = ScientificAgentState(
        user_id="unit", thread_id="unit", goal="联合比较文件误差与数据库训练覆盖",
        required_deliverables=["file_analysis", "database_analysis"],
        resource_binding=ResourceBinding(
            datasource_id="training_db", files=["model_v2.csv"],
        ),
        plan=[
            PlanStep(step_id="1", goal="file error", selected_tools=["compare_models"],
                     required_capabilities=[Capability.FILE]),
            PlanStep(step_id="2", goal="database coverage", selected_tools=["execute_readonly_sql"],
                     required_capabilities=[Capability.DATABASE]),
        ],
        tool_calls=[{"tool": "compare_models", "tool_call_id": "file-1"}],
        observations=[ToolResult(success=True, source="model_v2.csv", data={"mae": 0.725})],
        evidence=[Evidence(
            evidence_id="ev-file-1", claim="file metrics", value={"mae": 0.725},
            source_type="file", source="model_v2.csv", tool_call_id="file-1",
        )],
    )
    coverage = assess_goal_coverage(state)
    assert coverage.status == "PARTIAL"
    assert coverage.missing_deliverables == ["executed_database_analysis"]
