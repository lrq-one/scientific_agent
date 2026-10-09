"""Derive paired live-Qwen comparison artifacts from immutable run dirs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read(run_dir: Path) -> dict[str, Any]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    cases = [json.loads(line) for line in (run_dir / "case_results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    ledger = json.loads((run_dir / "budget_ledger.json").read_text(encoding="utf-8"))
    return {"manifest": manifest, "metrics": metrics, "cases": cases, "ledger": ledger}


def provider_usage(cases: list[dict[str, Any]]) -> dict[str, Any]:
    stages = [stage for case in cases for stage in case.get("stages", [])]
    def value(stage: dict[str, Any], key: str):
        for candidate in (stage.get(key), stage.get(f"provider_{key}"), (stage.get("llm_telemetry") or {}).get(key)):
            if isinstance(candidate, int):
                return candidate
        return None
    measured = [stage for stage in stages if all(value(stage, key) is not None for key in ("input_tokens", "output_tokens", "total_tokens"))]
    return {"stage_calls": len(stages), "provider_calls": sum((stage.get("measured_llm_calls") or 1) for stage in stages),
            "measured_stage_calls": len(measured),
            "coverage": round(len(measured) / len(stages), 4) if stages else None,
            "input_tokens": sum(value(stage, "input_tokens") or 0 for stage in measured),
            "output_tokens": sum(value(stage, "output_tokens") or 0 for stage in measured),
            "total_tokens": sum(value(stage, "total_tokens") or 0 for stage in measured) if len(measured) == len(stages) else None}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--optimized", required=True)
    parser.add_argument("--ablation", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    runs = {"baseline": read(Path(args.baseline)), "optimized": read(Path(args.optimized)), "ablation_no_skill": read(Path(args.ablation))}
    metric_names = ("task_success_rate", "avg_decision_calls", "avg_replans", "avg_invalid_tool_calls", "p95_latency_ms")
    comparison = {"schema_version": "scientific-agent-live-qwen-comparison-v1", "runs": {}, "paired_cases": []}
    for name, data in runs.items():
        comparison["runs"][name] = {"label": data["manifest"]["label"], "git_sha": data["manifest"]["git_sha"],
            "ablation": data["manifest"].get("ablation", "none"), "case_count": data["metrics"]["case_count"],
            "metrics": {key: data["metrics"].get(key) for key in metric_names},
            "provider_usage": data["metrics"].get("provider_usage") or provider_usage(data["cases"]), "budget": data["metrics"].get("budget"),
            "execution_scope": data["metrics"].get("execution_scope")}
    by_run = {name: {case["case_id"]: case for case in data["cases"]} for name, data in runs.items()}
    ids = sorted(set.intersection(*(set(items) for items in by_run.values())))
    for case_id in ids:
        item = {"case_id": case_id}
        for name, rows in by_run.items():
            row = rows[case_id]
            item[name] = {"task_success": row.get("task_success"),
                          "elapsed_ms": row.get("elapsed_ms"),
                          "stage_failures": [stage.get("error_type") for stage in row.get("stages", []) if not stage.get("success")],
                          "stage_count": len(row.get("stages", [])), "decision_calls": row.get("decision_calls", 0)}
        comparison["paired_cases"].append(item)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "live_comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "cost_ledger.json").write_text(json.dumps({name: data["ledger"].get("usage", {}) for name, data in runs.items()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failures = {name: [{"case_id": case["case_id"], "stage": stage.get("stage"), "error_type": stage.get("error_type"), "error": stage.get("error")}
                       for case in data["cases"] for stage in case.get("stages", []) if not stage.get("success")] for name, data in runs.items()}
    (output / "failure_analysis.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(comparison, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
