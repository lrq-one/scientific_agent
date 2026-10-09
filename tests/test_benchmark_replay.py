import json
from pathlib import Path

from evaluation.benchmark.run_benchmark import load_jsonl, metric, skill_metrics


ROOT = Path(__file__).parents[1] / "evaluation" / "benchmark"


def test_frozen_replay_contains_non_regression_cases_and_no_model_claim():
    cases = load_jsonl(ROOT / "cases.jsonl")
    traces = load_jsonl(ROOT / "traces.baseline.jsonl")
    assert {row["case_id"] for row in cases} >= {"A", "B", "C"}
    assert all(row["task_success"] for row in traces if row["case_id"] in {"A", "B", "C"})
    assert metric(traces, "avg_tokens") is None
    assert skill_metrics(cases, traces)["macro_f1"] == 1.0


def test_frozen_gold_provenance_is_separate_from_trace_values():
    gold = json.loads((ROOT / "gold.json").read_text(encoding="utf-8"))
    assert gold["source_head"] == "209fda8ecc15f130260b28af3e0dfdeb5fe6f9b4"
    assert "model output" in gold["gold_policy"]


def test_extended_offline_suite_has_meaningful_protocol_coverage():
    cases = load_jsonl(ROOT / "cases_extended.jsonl")
    assert len(cases) >= 30
    assert {row["family"] for row in cases} >= {
        "scope_recovery", "training_coverage", "multi_population", "security",
        "artifact_export", "query_checker_precondition", "budget_gate",
    }
    assert all(row.get("source") and row.get("required_evidence") for row in cases)
