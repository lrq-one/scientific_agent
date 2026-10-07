"""Static split-hygiene checks only; this does not execute or score Test cases."""

from __future__ import annotations

import json
from pathlib import Path
import re


EXPECTED_FAMILIES = {
    "file_only", "db_only", "file_db_mixed", "scientific_mcp", "planning",
    "replanning", "tool_recovery", "hitl_checkpoint", "security",
    "evidence_quality", "artifact", "followup", "refinement", "provenance",
    "scientific_model",
}


def validate(path: Path) -> dict:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [case["case_id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case_id")
    splits = {name: [case for case in cases if case["split"] == name] for name in ("dev", "test_draft")}
    if sum(map(len, splits.values())) != len(cases):
        raise ValueError("unexpected split")
    for name, items in splits.items():
        if {case["scenario_family"] for case in items} != EXPECTED_FAMILIES:
            raise ValueError(f"{name} scenario family coverage mismatch")
        if any(case["review_status"] != "agent_drafted" for case in items):
            raise ValueError("draft cases must not claim human review")
        if any(not case.get("expected_behavior") for case in items):
            raise ValueError("missing expected behavior")
    templates = [{case["template_family"] for case in splits[name]} for name in splits]
    if templates[0] & templates[1]:
        raise ValueError("template family leakage")
    normalize = lambda query: re.sub(r"\s+", "", query).lower()
    prompts = [{normalize(case["query"]) for case in splits[name]} for name in splits]
    if prompts[0] & prompts[1]:
        raise ValueError("exact normalized prompt leakage")
    return {
        "dev_cases": len(splits["dev"]),
        "test_draft_cases": len(splits["test_draft"]),
        "families_per_split": len(EXPECTED_FAMILIES),
        "template_overlap": 0,
        "exact_prompt_overlap": 0,
        "review_status": "agent_drafted",
        "independent_test_frozen": False,
    }


if __name__ == "__main__":
    print(json.dumps(validate(Path(__file__).with_name("scenario_cases.jsonl")), ensure_ascii=False))
