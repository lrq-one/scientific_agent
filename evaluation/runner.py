from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from evaluation.metrics import (
    e2e_metrics,
    intent_metrics,
    schema_retrieval_metrics,
    skill_routing_metrics,
    text2sql_metrics,
    tool_routing_metrics,
)


class EvaluationRunner:
    """Adapter-driven runner; it never invents results or calls a model implicitly."""

    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).parent

    def load(self, suite: str) -> list[dict[str, Any]]:
        path = self.root / suite / "cases.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def run(self, suite: str, adapter: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        rows = self.load(suite)
        outputs = []
        for case in rows:
            start = perf_counter()
            output = adapter(case)
            output.setdefault("latency_ms", (perf_counter() - start) * 1000)
            outputs.append(output)
        if suite == "intent":
            gold = [{"task_type": row["gold_task_type"], "capabilities": row["gold_capabilities"]} for row in rows]
            return intent_metrics(gold, outputs)
        if suite == "skill_routing":
            return skill_routing_metrics([row["gold_skills"] for row in rows], [row["ranked_skills"] for row in outputs])
        if suite == "tool_routing":
            return tool_routing_metrics([{**row, **output} for row, output in zip(rows, outputs, strict=True)])
        if suite == "schema_retrieval":
            return schema_retrieval_metrics(rows, [row["ranked_schema"] for row in outputs])
        if suite == "text2sql":
            return text2sql_metrics(outputs)
        if suite == "e2e":
            return e2e_metrics(outputs)
        raise ValueError(f"unknown evaluation suite: {suite}")
