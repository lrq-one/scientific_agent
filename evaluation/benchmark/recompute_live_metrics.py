"""Recompute outcome metrics from recorded live traces without new model calls."""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dirs", nargs="+")
    args = parser.parse_args()
    for raw in args.run_dirs:
        run_dir = Path(raw)
        rows = [json.loads(line) for line in (run_dir / "case_results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        for row in rows:
            db_case = row.get("case_id") in {"B", "C", "D08"}
            sql_ok = any(stage.get("stage") == "text2sql" and stage.get("success") for stage in row.get("stages", []))
            row["task_success"] = bool(row.get("response_success") and (sql_ok or not db_case))
        (run_dir / "case_results.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        old = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        latencies = [row["elapsed_ms"] for row in rows if isinstance(row.get("elapsed_ms"), (int, float))]
        old["task_success_rate"] = round(sum(bool(row.get("task_success")) for row in rows) / len(rows), 4) if rows else None
        old["p95_latency_ms"] = round(sorted(latencies)[min(len(latencies)-1, int((len(latencies)-1)*0.95))], 2) if latencies else None
        old["provider_call_count"] = sum((stage.get("measured_llm_calls") or 1)
                                          for row in rows for stage in row.get("stages", []))
        old["outcome_rule"] = "Database cases require a successful scope-verified Text2SQL stage and a grounded response; response-only prose is not scientific task success."
        (run_dir / "metrics.json").write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
