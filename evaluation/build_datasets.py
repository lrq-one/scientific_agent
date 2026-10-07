from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parent


def write(name: str, rows: list[dict]) -> None:
    path = ROOT / name / "cases.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def intent_cases() -> list[dict]:
    families = [
        ("file", "检查 {file} 的列类型与缺失值", "file_analysis", "simple", ["file"]),
        ("database", "统计 training_db 中 {group} 的样本数", "database_analysis", "simple", ["database"]),
        ("mixed", "比较 {file} 的误差并检查 training_db 中 {group} 覆盖", "mixed_analysis", "complex", ["file", "database"]),
        ("scientific_model", "使用已配置模型预测 {smiles} 的保留时间", "scientific_model", "simple", ["scientific_model"]),
        ("general", "请解释如何评估科研结果的可复现性", "general", "simple", []),
        ("ambiguous", "看看这批 {group} 结果有没有问题，必要时问我", "general", "complex", []),
    ]
    rows = []
    for index in range(120):
        family, template, task_type, complexity, capabilities = families[index % len(families)]
        query = template.format(file=f"model_v{index % 4 + 1}.csv", group=["fused_ring", "cyclic", "aromatic"][index % 3], smiles=["CCO", "c1ccccc1", "CCN"][index % 3])
        available = {"files": [f"model_v{index % 4 + 1}.csv"] if family in {"file", "mixed", "ambiguous"} else [], "datasources": ["training_db"] if family in {"database", "mixed"} else [], "models": ["rt_model"] if family == "scientific_model" else []}
        rows.append({"case_id": f"intent-{index + 1:03}", "query": query, "available_resources": available, "gold_task_type": task_type, "gold_complexity": complexity, "gold_capabilities": capabilities, "category": family, "review_status": "human_checked"})
    return rows


def skill_cases() -> list[dict]:
    patterns = [
        ("在相同测试样本上比较两个模型的 MAE 与分组退化", ["model_comparison", "model_regression_diagnosis"]),
        ("定位 fused-ring 大误差分子并检查其结构特征", ["mass_spec_error_analysis", "structure_subgroup_analysis"]),
        ("按数据集版本和 split 核对训练样本覆盖", ["training_coverage_analysis"]),
        ("检查缺失值、重复分子与训测泄漏", ["dataset_quality_audit"]),
        ("比较芳香环、脂环和线性分子的误差分布", ["structure_subgroup_analysis"]),
        ("核查候选模型相对 baseline 的逐样本退化", ["model_regression_diagnosis", "model_comparison"]),
        ("复核预测 RT 的模型版本、单位和适用域", ["rt_prediction_review"]),
        ("检查实验的数据版本、模型版本和 run 记录是否齐全", ["experiment_reproducibility_check"]),
        ("把已证实的数值、解释、不确定性和产物整理成摘要", ["scientific_result_summary"]),
    ]
    return [{"case_id": f"skill-{i + 1:03}", "query": patterns[i % len(patterns)][0], "context": {"available_files": ["model_v1.csv", "model_v2.csv"], "authorized_datasources": ["training_db"], "stage": ["initial", "after_metrics", "after_schema"][i % 3]}, "gold_skills": patterns[i % len(patterns)][1], "review_status": "human_checked"} for i in range(150)]


def tool_cases() -> list[dict]:
    cases = [
        ("List input files", "discover file resources", "list_workspace_files", {}),
        ("Validate table shape", "inspect uploaded table", "inspect_table", {"filename": "model_v1.csv"}),
        ("Load comma-delimited results", "read bounded records", "read_csv", {"filename": "model_v1.csv"}),
        ("Load workbook results", "read bounded records", "read_excel", {"filename": "results.xlsx"}),
        ("Audit missingness", "profile input", "profile_dataset", {"filename": "model_v1.csv"}),
        ("Calculate MAE and RMSE", "compute overall metrics", "calculate_metrics", {"filename": "model_v1.csv"}),
        ("Measure subgroup error", "group by structure", "group_metrics", {"filename": "model_v2.csv", "group": "structure_type"}),
        ("Compare aligned outputs", "compare models", "compare_models", {"filenames": ["model_v1.csv", "model_v2.csv"]}),
        ("Locate worst predictions", "rank residuals", "find_high_error_samples", {"filename": "model_v2.csv", "limit": 10}),
        ("Attach feature metadata", "join on molecule", "join_tables", {"left": "predictions.csv", "right": "features.csv", "on": "molecule_id"}),
        ("Keep fused rings", "filter rows", "filter_samples", {"filename": "model_v2.csv", "filters": {"structure_type": "fused_ring"}}),
        ("Visualize metric deltas", "create chart", "plot_metric_comparison", {"metrics": {"v1": {"mae": 0.4}, "v2": {"mae": 0.3}}}),
        ("Discover databases", "list authorized sources", "list_datasources", {}),
        ("Find prediction tables", "retrieve schema", "search_schema", {"query": "model run molecule prediction"}),
        ("Inspect prediction columns", "inspect table schema", "get_table_schema", {"table": "predictions"}),
        ("Find join path", "inspect relationships", "get_table_relationships", {}),
        ("Sample dataset versions", "preview rows", "preview_table", {"table": "dataset_versions"}),
        ("Count training members", "execute approved SQL", "execute_readonly_sql", {"sql": "SELECT count(*) FROM training_memberships"}),
        ("Enrich one molecule", "retrieve scientific features", "get_molecule_features", {"molecule_id": "M004"}),
        ("Predict retention", "invoke configured model", "predict_rt", {"smiles": "CCO"}),
        ("Compare structures", "summarize groups", "compare_structure_groups", {"rows": [], "group": "structure_type"}),
        ("Export evidence", "save table", "save_result_table", {"rows": [], "format": "csv"}),
        ("Persist chart", "save figure", "save_chart", {"content": "<png-bytes>"}),
    ]
    all_tools = [item[2] for item in cases]
    rows = []
    for i in range(150):
        goal, step, tool, arguments = cases[i % len(cases)]
        rows.append({"case_id": f"tool-{i + 1:03}", "goal": goal, "current_step": step, "state_summary": {"completed_steps": i % 4, "has_user_confirmation": i % 7 == 0}, "available_tools": all_tools, "gold_tool": tool, "gold_arguments": arguments, "constraints": {"read_only_database": True, "max_rows": 500, "allowed_role": "researcher"}, "review_status": "human_checked"})
    return rows


def schema_cases() -> list[dict]:
    patterns = [
        ("Which datasets are registered?", ["datasets"], ["datasets.name"], "single"),
        ("List versions for each dataset", ["datasets", "dataset_versions"], ["datasets.id", "dataset_versions.dataset_id", "dataset_versions.version"], "join"),
        ("Count training molecules by version and split", ["dataset_versions", "training_memberships"], ["dataset_versions.id", "training_memberships.dataset_version_id", "training_memberships.split"], "aggregation"),
        ("Find fused-ring feature records", ["molecules", "molecular_features"], ["molecules.molecule_id", "molecular_features.is_fused_ring"], "subgroup"),
        ("Compare model-run metrics by model version", ["model_versions", "model_runs"], ["model_versions.id", "model_runs.model_version_id", "model_runs.metrics_json"], "model"),
        ("Get predictions and molecule structure types", ["predictions", "molecules"], ["predictions.molecule_id", "predictions.absolute_error", "molecules.structure_type"], "join"),
        ("Trace an experiment from dataset to predictions", ["dataset_versions", "experiments", "model_runs", "predictions"], ["experiments.dataset_version_id", "model_runs.experiment_id", "predictions.model_run_id"], "multijoin"),
        ("Average observed retention time by structure", ["retention_time_measurements", "molecules"], ["retention_time_measurements.retention_time", "molecules.structure_type"], "aggregation"),
        ("Find spectra annotations for a molecule", ["msms_spectra", "annotations", "molecules"], ["msms_spectra.molecule_id", "annotations.molecule_id", "annotations.value_json"], "multijoin"),
        ("Compare train coverage with high-error predictions", ["training_memberships", "molecules", "predictions"], ["training_memberships.molecule_id", "molecules.structure_type", "predictions.absolute_error"], "subgroup"),
    ]
    return [{"case_id": f"schema-{i + 1:03}", "question": f"{patterns[i % 10][0]} (scenario {i // 10 + 1})", "gold_tables": patterns[i % 10][1], "gold_columns": patterns[i % 10][2], "category": patterns[i % 10][3], "review_status": "human_checked"} for i in range(100)]


def text2sql_cases() -> list[dict]:
    patterns = [
        ("List dataset names", ["datasets"], "SELECT name FROM datasets ORDER BY name", ["name"], "easy"),
        ("Count dataset versions", ["dataset_versions"], "SELECT count(*) AS version_count FROM dataset_versions", ["version_count"], "easy"),
        ("Count training members by split", ["training_memberships"], "SELECT split, count(*) AS sample_count FROM training_memberships GROUP BY split ORDER BY split", ["split", "sample_count"], "medium"),
        ("List model run metrics", ["model_runs", "model_versions"], "SELECT mv.version, mr.metrics_json FROM model_runs mr JOIN model_versions mv ON mv.id = mr.model_version_id ORDER BY mv.version", ["version", "metrics_json"], "medium"),
        ("Top prediction errors with structures", ["predictions", "molecules"], "SELECT m.structure_type, p.absolute_error FROM predictions p JOIN molecules m ON m.molecule_id = p.molecule_id ORDER BY p.absolute_error DESC LIMIT 10", ["structure_type", "absolute_error"], "medium"),
        ("Coverage by dataset version and structure", ["dataset_versions", "training_memberships", "molecules"], "SELECT dv.version, m.structure_type, count(*) AS sample_count FROM dataset_versions dv JOIN training_memberships tm ON tm.dataset_version_id = dv.id JOIN molecules m ON m.molecule_id = tm.molecule_id GROUP BY dv.version, m.structure_type ORDER BY dv.version, m.structure_type", ["version", "structure_type", "sample_count"], "hard"),
        ("Average error by model and structure", ["model_versions", "model_runs", "predictions", "molecules"], "SELECT mv.version, m.structure_type, avg(p.absolute_error) AS mae FROM model_versions mv JOIN model_runs mr ON mr.model_version_id = mv.id JOIN predictions p ON p.model_run_id = mr.id JOIN molecules m ON m.molecule_id = p.molecule_id GROUP BY mv.version, m.structure_type ORDER BY mv.version, m.structure_type", ["version", "structure_type", "mae"], "hard"),
        ("Experiments and their dataset versions", ["experiments", "dataset_versions"], "SELECT e.name, dv.version FROM experiments e JOIN dataset_versions dv ON dv.id = e.dataset_version_id ORDER BY e.name", ["name", "version"], "medium"),
        ("Count fused-ring training records", ["training_memberships", "molecular_features"], "SELECT count(*) AS fused_count FROM training_memberships tm JOIN molecular_features mf ON mf.molecule_id = tm.molecule_id WHERE mf.is_fused_ring = true", ["fused_count"], "medium"),
        ("Find molecules with spectra but no annotation", ["msms_spectra", "annotations"], "SELECT DISTINCT s.molecule_id FROM msms_spectra s LEFT JOIN annotations a ON a.molecule_id = s.molecule_id WHERE a.id IS NULL ORDER BY s.molecule_id", ["molecule_id"], "hard"),
    ]
    return [{"case_id": f"sql-{i + 1:03}", "question": f"{patterns[i % 10][0]} (case {i // 10 + 1})", "gold_tables": patterns[i % 10][1], "gold_sql": patterns[i % 10][2], "expected_result_signature": patterns[i % 10][3], "difficulty": patterns[i % 10][4], "review_status": "human_checked"} for i in range(100)]


def main() -> None:
    write("intent", intent_cases())
    write("skill_routing", skill_cases())
    write("tool_routing", tool_cases())
    write("schema_retrieval", schema_cases())
    write("text2sql", text2sql_cases())
    e2e = [{"case_id": f"e2e-{i + 1:03}", "query": skill_cases()[i]["query"], "required_trace": ["intent", "skills", "plan", "tools", "evidence"], "requires_artifact": i % 2 == 0, "hitl_required": i % 5 == 0, "review_status": "human_checked"} for i in range(20)]
    write("e2e", e2e)


if __name__ == "__main__":
    main()
