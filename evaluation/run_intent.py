from __future__ import annotations

import argparse
import asyncio
import os
from time import perf_counter
from typing import Any

from app.agents.request_router import RequestRouter
from app.models.schemas import RequestIntent, ResourceSummary
from evaluation.metrics import accuracy, intent_metrics
from evaluation.phase4_common import (
    MODEL,
    PROMPT_VERSION,
    ResultCache,
    create_run,
    empty_record,
    split_cases,
    telemetry_summary,
    validate_record,
)
from evaluation.phase4_llm import RealLLMClient


def resources(case: dict[str, Any]) -> ResourceSummary:
    available = case["available_resources"]
    return ResourceSummary(
        available_files=available.get("files", []),
        authorized_datasources=available.get("datasources", []),
        available_scientific_models=available.get("models", []),
        available_mcp_tools=available.get("mcp_tools", []),
    )


def gold(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_type": case["gold_task_type"],
        "complexity": case["gold_complexity"],
        "capabilities": case["gold_capabilities"],
    }


def prediction(intent: RequestIntent) -> dict[str, Any]:
    return {
        "task_type": intent.task_type,
        "complexity": intent.complexity,
        "capabilities": [item.value for item in intent.required_capabilities],
    }


def correct(predicted: dict[str, Any], expected: dict[str, Any]) -> bool:
    return (
        predicted["task_type"] == expected["task_type"]
        and predicted["complexity"] == expected["complexity"]
        and set(predicted["capabilities"]) == set(expected["capabilities"])
    )


def i0(case: dict[str, Any], split: str) -> dict[str, Any]:
    started = perf_counter()
    intent = RequestRouter().route(case["query"], resources(case))
    expected = gold(case)
    predicted = prediction(intent)
    row = empty_record(case["case_id"], split, "intent", "I0_deterministic", expected)
    row.update(
        success=correct(predicted, expected),
        prediction=predicted,
        candidate_count=5,
        total_latency_ms=round((perf_counter() - started) * 1000, 2),
        error_type=None if correct(predicted, expected) else "wrong_prediction",
        api_calls=0,
        cache_hit=False,
    )
    validate_record(row)
    return row


def prompt(case: dict[str, Any], method: str) -> str:
    base = (
        "Classify the user request as one RequestIntent. Allowed task_type values: "
        "file_analysis, database_analysis, mixed_analysis, scientific_model, general. "
        "Allowed capabilities: file, database, scientific_model, mcp. "
        "Return only structured output and do not execute anything.\n"
        f"User query: {case['query']}\n"
    )
    if method.startswith("I2_hybrid"):
        available = case["available_resources"]
        base += (
            f"Available files: {available.get('files', [])}\n"
            f"Authorized datasources: {available.get('datasources', [])}\n"
            f"Available scientific models: {available.get('models', [])}\n"
            f"Available MCP tools: {available.get('mcp_tools', [])}\n"
            "Only select capabilities backed by these resources."
        )
        if method.startswith("I2_hybrid_v2"):
            base += (
                "\nComplexity rules: use complex for a request with multiple dependent objectives, "
                "comparison plus checking/analysis, or an underspecified request that needs clarification. "
                "Do not infer file_analysis merely because a file is available: when the user does not name "
                "a file operation, database operation, or model invocation, classify an exploratory ambiguous "
                "request as general with no capabilities."
            )
    return base


async def llm_case(
    client: RealLLMClient,
    cache: ResultCache,
    case: dict[str, Any],
    split: str,
    method: str,
) -> dict[str, Any]:
    text = prompt(case, method)
    prompt_version = "intent-v2" if method.startswith("I2_hybrid_v2") else PROMPT_VERSION
    no_thinking = method.endswith("_nothinking")
    config = {
        "stage": "intent", "method": method, "model": MODEL, "prompt_version": prompt_version,
        "enable_thinking": not no_thinking, "max_tokens": 1024 if no_thinking else None,
    }
    key = cache.key(case_id=case["case_id"], stage="intent", method=method, config=config, prompt=text)
    cached = cache.get(key)
    if cached is not None:
        if cached.get("llm_latency_ms"):
            cached["batch_elapsed_ms"] = cached.get("total_latency_ms", 0.0)
            cached["total_latency_ms"] = cached["llm_latency_ms"]
        return cached
    expected = gold(case)
    row = empty_record(case["case_id"], split, "intent", method, expected)
    row["prompt_version"] = prompt_version
    started = perf_counter()
    try:
        intent, telemetry = await client.structured(RequestIntent, text)
        if method.startswith("I2_hybrid"):
            available = resources(case)
            allowed = set()
            if available.available_files:
                allowed.add("file")
            if available.authorized_datasources:
                allowed.add("database")
            if available.available_scientific_models:
                allowed.add("scientific_model")
            if available.available_mcp_tools:
                allowed.add("mcp")
            intent.required_capabilities = [item for item in intent.required_capabilities if item.value in allowed]
        predicted = prediction(intent)
        is_correct = correct(predicted, expected)
        row.update(
            success=is_correct,
            prediction=predicted,
            candidate_count=5,
            error_type=None if is_correct else "wrong_prediction",
            **telemetry,
        )
    except Exception as exc:
        row.update(error_type=type(exc).__name__, error=str(exc), api_calls=1)
    row["batch_elapsed_ms"] = round((perf_counter() - started) * 1000, 2)
    row["total_latency_ms"] = row["llm_latency_ms"] or row["batch_elapsed_ms"]
    validate_record(row)
    cache.put(key, row)
    return row


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row["prediction"] is not None]
    base = telemetry_summary(rows)
    if not valid:
        return base
    expected = [{"task_type": row["gold"]["task_type"], "capabilities": row["gold"]["capabilities"]} for row in valid]
    predicted = [{"task_type": row["prediction"]["task_type"], "capabilities": row["prediction"]["capabilities"]} for row in valid]
    base.update(intent_metrics(expected, predicted))
    base["complexity_accuracy"] = accuracy(
        [row["gold"]["complexity"] for row in valid],
        [row["prediction"]["complexity"] for row in valid],
    )
    return base


async def main_async(split: str, methods: list[str], concurrency: int) -> None:
    cases = split_cases("intent", split)
    cache = ResultCache()
    api_base = os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("LLM_MODEL") or os.getenv("LLM_MODEL_NAME")
    if any(method != "I0_deterministic" for method in methods) and not (api_base and api_key and model):
        raise RuntimeError("real LLM environment is not configured")
    try:
        for method in methods:
            if method == "I0_deterministic":
                rows = [i0(case, split) for case in cases]
            else:
                no_thinking = method.endswith("_nothinking")
                client = RealLLMClient(
                    api_base=api_base, api_key=api_key, model=model, concurrency=concurrency,
                    enable_thinking=False if no_thinking else None,
                    max_tokens=1024 if no_thinking else None,
                )
                rows = await asyncio.gather(*(llm_case(client, cache, case, split, method) for case in cases))
                await client.close()
            config = {
                "stage": "intent", "split": split, "method": method, "model": MODEL,
                "prompt_version": "intent-v2" if method.startswith("I2_hybrid_v2") else PROMPT_VERSION,
                "concurrency": concurrency,
                "enable_thinking": not method.endswith("_nothinking"),
                "max_tokens": 1024 if method.endswith("_nothinking") else None,
            }
            result = metrics(rows)
            create_run(f"intent_{split}_{method.lower()}", config, rows, result)
            print(method, result, flush=True)
    finally:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--methods", nargs="+", default=["I0_deterministic", "I1_llm_only", "I2_hybrid"])
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    asyncio.run(main_async(args.split, args.methods, args.concurrency))


if __name__ == "__main__":
    main()
