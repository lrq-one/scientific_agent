"""Paired comparison for two benchmark JSON outputs."""
from __future__ import annotations
import argparse, json
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path); parser.add_argument("candidate", type=Path); parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    left = json.loads(args.baseline.read_text(encoding="utf-8")); right = json.loads(args.candidate.read_text(encoding="utf-8"))
    if [x["case_id"] for x in left["case_results"]] != [x["case_id"] for x in right["case_results"]]: raise SystemExit("case IDs/order differ; paired comparison refused")
    rows = []
    for name in sorted(set(left["metrics"]) | set(right["metrics"])):
        before, after = left["metrics"].get(name), right["metrics"].get(name)
        rows.append({"metric": name, "baseline": before, "candidate": after,
                     "delta": None if before is None or after is None else round(after - before, 4),
                     "status": "not_measured" if before is None or after is None else "measured"})
    result = {"schema_version": "scientific-agent-benchmark-paired-v1", "baseline_run": left["run_id"], "candidate_run": right["run_id"], "same_cases": True, "rows": rows,
              "interpretation": "A replay comparison is not a real-model improvement claim; causal attribution requires an actual paired optimized run."}
    if args.output: args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
