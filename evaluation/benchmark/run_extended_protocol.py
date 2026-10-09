"""Validate the 34-case offline protocol inventory without model calls.

This is intentionally a contract/replay inventory, not a fabricated quality
score: every case is marked ``not_measured`` until an actual trace exists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="evaluation/benchmark/results/extended_protocol.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    source = root / "cases_extended.jsonl"
    cases = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    required = {"case_id", "family", "request", "source", "required_evidence", "expected_outcome"}
    invalid = [case.get("case_id") for case in cases if not required <= set(case)]
    payload = {
        "schema_version": "scientific-agent-offline-protocol-v1",
        "case_count": len(cases), "source": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "measured": False, "metric_policy": "not_measured; no model/database calls",
        "validation": {"invalid_cases": invalid, "valid": not invalid and len(cases) >= 30},
        "cases": [{"case_id": case["case_id"], "family": case["family"],
                   "expected_outcome": case["expected_outcome"], "measured": False,
                   "required_evidence": case["required_evidence"], "source": case["source"]} for case in cases],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["validation"] | {"case_count": len(cases)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
