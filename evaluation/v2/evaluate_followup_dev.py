"""Local deterministic constraint audit for the new Phase 4.5 Dev cases.

This is not an independent Frozen Test and does not call the LLM or execute Tools.
"""

from __future__ import annotations

import json
from pathlib import Path
import uuid

from app.services.followup import ConversationContextResolver


def main() -> None:
    rows = [json.loads(line) for line in Path(__file__).with_name("followup_dev.jsonl").read_text(encoding="utf-8").splitlines()]
    resolver = ConversationContextResolver()
    correct = 0
    failures = []
    unresolved = []
    for index, row in enumerate(rows, start=1):
        count = row.get("recent_count", 1) if row["has_context"] else 0
        contexts = [{"task": {"id": str(uuid.uuid4()), "conversation_id": "dev-conversation", "status": "completed"},
                     "messages": [{"role": "user", "content": "统计训练集结构覆盖"}],
                     "assistant_message": {"content": "已返回覆盖数据"}, "evidence": []}
                    for _ in range(count)]
        decision = resolver.deterministic(row["query"], contexts[0] if contexts else None)
        if decision is None:
            unresolved.append(index)
            continue
        decision = resolver._attach_target(decision, row["query"], contexts)
        passed = decision.follow_up_type == row["expected"] and bool(decision.clarification_question) == row.get("clarification", False)
        correct += int(passed)
        if not passed:
            failures.append({"case": index, "expected": row["expected"], "actual": decision.follow_up_type,
                             "clarification": bool(decision.clarification_question)})
    print(json.dumps({"dataset": "new Dev (not Frozen)", "cases": len(rows), "correct": correct,
                      "deterministic_accuracy": correct / len(rows), "llm_unresolved": unresolved,
                      "failures": failures, "llm_calls": 0, "tool_calls": 0}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
