from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parent
EXPECTED = {
    "intent/cases.jsonl": (120, {"case_id", "query", "available_resources", "gold_task_type", "gold_complexity", "gold_capabilities"}),
    "skill_routing/cases.jsonl": (150, {"case_id", "query", "context", "gold_skills"}),
    "tool_routing/cases.jsonl": (150, {"case_id", "goal", "current_step", "state_summary", "available_tools", "gold_tool", "gold_arguments", "constraints"}),
    "schema_retrieval/cases.jsonl": (100, {"case_id", "question", "gold_tables", "gold_columns", "category"}),
    "text2sql/cases.jsonl": (100, {"case_id", "question", "gold_tables", "gold_sql", "expected_result_signature", "difficulty"}),
}


def validate() -> dict[str, int]:
    counts = {}
    for relative, (minimum, fields) in EXPECTED.items():
        path = ROOT / relative
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(rows) < minimum:
            raise ValueError(f"{relative}: expected at least {minimum}, got {len(rows)}")
        for index, row in enumerate(rows, 1):
            missing = fields - set(row)
            if missing:
                raise ValueError(f"{relative}:{index}: missing {sorted(missing)}")
        counts[relative] = len(rows)
    return counts


if __name__ == "__main__":
    print(json.dumps(validate(), ensure_ascii=False, indent=2))
