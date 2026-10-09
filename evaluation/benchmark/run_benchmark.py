"""Deterministic replay benchmark for the Scientific Agent.

This runner consumes committed acceptance traces. It never instantiates an LLM,
opens a database connection, or mutates task history. Null fields are retained
as ``not_measured`` rather than treated as zero.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def mean_known(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [row[field] for row in rows if isinstance(row.get(field), (int, float))]
    return round(statistics.mean(values), 4) if values else None


def metric(rows: list[dict[str, Any]], name: str) -> Any:
    if name in {"task_success_rate", "evidence_coverage", "tool_success_rate", "recovery_success_rate"}:
        field = {"task_success_rate":"task_success", "evidence_coverage":"evidence_coverage", "tool_success_rate":"tool_success_rate", "recovery_success_rate":"recovery_success"}[name]
        return mean_known(rows, field)
    if name in {"avg_tool_calls", "avg_decision_calls", "avg_replans", "avg_invalid_tool_calls", "avg_tokens", "p95_latency_ms"}:
        field = {"avg_tool_calls":"tool_calls", "avg_decision_calls":"decision_calls", "avg_replans":"replans", "avg_invalid_tool_calls":"invalid_tool_calls", "avg_tokens":"tokens", "p95_latency_ms":"latency_ms"}[name]
        values = [row[field] for row in rows if isinstance(row.get(field), (int, float))]
        if not values:
            return None
        if name == "p95_latency_ms":
            values.sort(); index = (len(values) - 1) * 0.95; lo = int(index); hi = min(lo + 1, len(values) - 1); frac = index - lo
            return round(values[lo] * (1 - frac) + values[hi] * frac, 4)
        return round(statistics.mean(values), 4)
    return None


def skill_metrics(cases: list[dict[str, Any]], traces: list[dict[str, Any]]) -> dict[str, float]:
    labels = sorted({skill for case in cases for skill in case["gold_skills"]} | {skill for row in traces for skill in row["observed_skills"]})
    if not labels: return {"macro_f1": 0.0, "exact_set_match": 1.0}
    scores = []
    for label in labels:
        tp = fp = fn = 0
        for case, row in zip(cases, traces, strict=True):
            gold, pred = label in case["gold_skills"], label in row["observed_skills"]
            tp += gold and pred; fp += (not gold) and pred; fn += gold and (not pred)
        p = tp / (tp + fp) if tp + fp else 0.0; r = tp / (tp + fn) if tp + fn else 0.0
        scores.append(2 * p * r / (p + r) if p + r else 0.0)
    exact = sum(set(c["gold_skills"]) == set(t["observed_skills"]) for c, t in zip(cases, traces, strict=True)) / len(cases)
    return {"macro_f1": round(statistics.mean(scores), 4), "exact_set_match": round(exact, 4)}


def git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT.parent.parent, text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", default=str(ROOT / "traces.baseline.jsonl"))
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    cases = load_jsonl(ROOT / "cases.jsonl")
    traces = load_jsonl(Path(args.trace))
    by_id = {row["case_id"]: row for row in traces}
    ordered = [by_id[case["case_id"]] for case in cases]
    result = {
        "schema_version": "scientific-agent-benchmark-v1",
        "run_id": f"{args.label}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "label": args.label, "git_sha": git_sha(), "environment": "offline_replay",
        "model": "not_measured", "source_trace": str(Path(args.trace).resolve()),
        "case_count": len(cases),
        "metrics": {
            name: metric(ordered, name) for name in ("task_success_rate", "evidence_coverage", "tool_success_rate", "recovery_success_rate", "avg_tool_calls", "avg_decision_calls", "avg_replans", "avg_invalid_tool_calls", "avg_tokens", "p95_latency_ms")
        },
        "skill_routing": skill_metrics(cases, ordered),
        "case_results": [{"case_id": row["case_id"], "task_success": row["task_success"], "source": row["source"]} for row in ordered],
        "limitations": ["Replay contains only committed acceptance facts.", "Token and latency are not_measured where traces did not persist them.", "No real Qwen request was made."],
    }
    output = Path(args.output) if args.output else ROOT / "results" / f"{args.label}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
