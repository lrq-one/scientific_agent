from __future__ import annotations

from collections import Counter
from statistics import mean
from typing import Any, Iterable


def accuracy(gold: list[str], predicted: list[str]) -> float:
    return sum(a == b for a, b in zip(gold, predicted, strict=True)) / len(gold) if gold else 0.0


def macro_f1(gold: list[Any], predicted: list[Any]) -> float:
    labels = sorted(set(gold) | set(predicted), key=str)
    scores = []
    for label in labels:
        tp = sum(a == label and b == label for a, b in zip(gold, predicted, strict=True))
        fp = sum(a != label and b == label for a, b in zip(gold, predicted, strict=True))
        fn = sum(a == label and b != label for a, b in zip(gold, predicted, strict=True))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return mean(scores) if scores else 0.0


def multilabel_macro_f1(gold: list[list[str]], predicted: list[list[str]]) -> float:
    labels = sorted({label for rows in gold + predicted for label in rows})
    scores = []
    for label in labels:
        tp = sum(label in a and label in b for a, b in zip(gold, predicted, strict=True))
        fp = sum(label not in a and label in b for a, b in zip(gold, predicted, strict=True))
        fn = sum(label in a and label not in b for a, b in zip(gold, predicted, strict=True))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return mean(scores) if scores else 0.0


def intent_metrics(gold: list[dict], predicted: list[dict]) -> dict[str, float]:
    return {
        "accuracy": accuracy([row["task_type"] for row in gold], [row["task_type"] for row in predicted]),
        "macro_f1": macro_f1([row["task_type"] for row in gold], [row["task_type"] for row in predicted]),
        "capability_f1": multilabel_macro_f1(
            [row["capabilities"] for row in gold], [row["capabilities"] for row in predicted]
        ),
    }


def ranking_metrics(gold: list[list[str]], ranked: list[list[str]], k: int = 3) -> dict[str, float]:
    if not gold:
        return {"top1_accuracy": 0.0, f"hit_at_{k}": 0.0, f"recall_at_{k}": 0.0, "mrr": 0.0}
    top1, hits, recalls, reciprocal = [], [], [], []
    for expected, candidates in zip(gold, ranked, strict=True):
        expected_set = set(expected); top = candidates[:k]
        top1.append(bool(candidates and candidates[0] in expected_set))
        hits.append(bool(expected_set & set(top)))
        recalls.append(len(expected_set & set(top)) / len(expected_set) if expected_set else 1.0)
        ranks = [index + 1 for index, item in enumerate(candidates) if item in expected_set]
        reciprocal.append(1 / min(ranks) if ranks else 0.0)
    return {
        "top1_accuracy": mean(top1), f"hit_at_{k}": mean(hits),
        f"recall_at_{k}": mean(recalls), "mrr": mean(reciprocal),
    }


def skill_routing_metrics(gold: list[list[str]], ranked: list[list[str]]) -> dict[str, float]:
    metrics = ranking_metrics(gold, ranked, 3)
    metrics["macro_f1"] = multilabel_macro_f1(gold, [items[:3] for items in ranked])
    return metrics


def tool_routing_metrics(rows: list[dict]) -> dict[str, float]:
    if not rows:
        return {name: 0.0 for name in ("selection_accuracy", "argument_valid_rate", "execution_success_rate", "unauthorized_tool_rate")}
    return {
        "selection_accuracy": mean(row["gold_tool"] == row["selected_tool"] for row in rows),
        "argument_valid_rate": mean(bool(row.get("arguments_valid")) for row in rows),
        "execution_success_rate": mean(bool(row.get("execution_success")) for row in rows),
        "unauthorized_tool_rate": mean(bool(row.get("unauthorized")) for row in rows),
    }


def schema_retrieval_metrics(gold: list[dict], ranked: list[list[dict]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for k in (1, 3, 5):
        result[f"table_recall_at_{k}"] = mean(
            len(set(item["gold_tables"]) & {hit["table"] for hit in hits[:k]}) / len(set(item["gold_tables"]))
            for item, hits in zip(gold, ranked, strict=True)
        )
        result[f"column_recall_at_{k}"] = mean(
            len(set(item["gold_columns"]) & {column for hit in hits[:k] for column in hit.get("columns", [])})
            / max(1, len(set(item["gold_columns"])))
            for item, hits in zip(gold, ranked, strict=True)
        )
    result["mrr"] = mean(
        next((1 / (index + 1) for index, hit in enumerate(hits) if hit["table"] in item["gold_tables"]), 0.0)
        for item, hits in zip(gold, ranked, strict=True)
    )
    return result


def text2sql_metrics(rows: list[dict]) -> dict[str, float]:
    bool_fields = {
        "valid_sql_rate": "valid_sql", "execution_accuracy": "execution_success",
        "result_accuracy": "result_match", "security_pass_rate": "security_pass",
        "repair_success_rate": "repair_success",
    }
    metrics = {name: mean(bool(row.get(field)) for row in rows) if rows else 0.0 for name, field in bool_fields.items()}
    metrics["latency_ms"] = mean(row.get("latency_ms", 0.0) for row in rows) if rows else 0.0
    return metrics


def percentile(values: Iterable[float], percent: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = (len(ordered) - 1) * percent
    lower = int(index); upper = min(lower + 1, len(ordered) - 1); fraction = index - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def e2e_metrics(rows: list[dict]) -> dict[str, float]:
    if not rows:
        return {}
    calls = [row.get("tool_calls", 0) for row in rows]
    return {
        "task_success_rate": mean(bool(row.get("task_success")) for row in rows),
        "evidence_coverage": mean(float(row.get("evidence_coverage", 0)) for row in rows),
        "tool_success_rate": mean(float(row.get("tool_success_rate", 0)) for row in rows),
        "hitl_resume_success_rate": mean(bool(row.get("hitl_resume_success")) for row in rows if row.get("hitl_required")) if any(row.get("hitl_required") for row in rows) else 0.0,
        "avg_tool_calls": mean(calls), "tool_calls_p50": percentile(calls, 0.5), "tool_calls_p95": percentile(calls, 0.95),
    }
