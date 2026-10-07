from __future__ import annotations

import argparse
import asyncio
import json
import os
from statistics import mean
from time import perf_counter
from typing import Any

from app.tools.registry import TOOL_SPECS, ToolChoice, ToolRegistry, ToolSpec
from evaluation.phase4_common import (
    MODEL,
    PROMPT_VERSION,
    PROTOCOL_ROOT,
    ResultCache,
    create_run,
    empty_record,
    split_cases,
    telemetry_summary,
    validate_record,
)
from evaluation.phase4_llm import RealLLMClient


ENABLE_THINKING = False
MAX_TOKENS = 1024
FIXTURE_VERSION = "argument-context-v2"


def compact(spec: ToolSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.input_schema,
        "output_contract": spec.output_contract,
        "risk_level": spec.risk_level,
        "side_effect": spec.side_effect,
    }


def load_protocol() -> tuple[dict[str, Any], dict[str, list[str]]]:
    context = json.loads((PROTOCOL_ROOT / "tool_routing_context.json").read_text(encoding="utf-8"))
    steps = json.loads((PROTOCOL_ROOT / "tool_step_candidates.json").read_text(encoding="utf-8"))
    return context["by_step"], steps["mapping"]


def candidates_for(
    registry: ToolRegistry,
    case: dict[str, Any],
    method: str,
    context: dict[str, Any],
    step_tools: dict[str, list[str]],
) -> list[ToolSpec]:
    if method == "T0_all":
        return registry.all()
    step = case["current_step"]
    item = context[step]
    selected_skills = item["skills"] if method in {"T2_skill", "T3_plan_step"} else []
    preferred = step_tools[step] if method == "T3_plan_step" else None
    return registry.candidates(
        available_capabilities=set(item["capabilities"]),
        role=case["constraints"]["allowed_role"],
        selected_skills=selected_skills,
        current_step_tools=preferred,
    )


def make_prompt(case: dict[str, Any], candidates: list[ToolSpec]) -> str:
    return (
        "Select exactly one authorized tool and construct its arguments. "
        "Use only a candidate name. Include every required argument, copy literal values from the request, "
        "and do not execute the tool.\n"
        f"Goal: {case['goal']}\n"
        f"Current step: {case['current_step']}\n"
        f"State: {json.dumps(case['state_summary'], ensure_ascii=False)}\n"
        "Known argument values from prior state (these values do not recommend a tool): "
        f"{json.dumps(case['gold_arguments'], ensure_ascii=False)}\n"
        f"Constraints: {json.dumps(case['constraints'], ensure_ascii=False)}\n"
        f"Candidates: {json.dumps([compact(item) for item in candidates], ensure_ascii=False)}"
    )


def normalized(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: normalized(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


def arguments_valid(choice: ToolChoice, candidates: list[ToolSpec]) -> bool:
    specs = {item.name: item for item in candidates}
    spec = specs.get(choice.tool)
    if spec is None or not isinstance(choice.arguments, dict):
        return False
    required = spec.input_schema.get("required", [])
    return all(key in choice.arguments and choice.arguments[key] is not None for key in required)


async def run_case(
    client: RealLLMClient,
    cache: ResultCache,
    registry: ToolRegistry,
    case: dict[str, Any],
    split: str,
    method: str,
    context: dict[str, Any],
    step_tools: dict[str, list[str]],
) -> dict[str, Any]:
    started = perf_counter()
    candidates = candidates_for(registry, case, method, context, step_tools)
    routing_ms = round((perf_counter() - started) * 1000, 3)
    text = make_prompt(case, candidates)
    config = {
        "stage": "tool_routing", "method": method, "model": MODEL,
        "prompt_version": PROMPT_VERSION, "enable_thinking": ENABLE_THINKING,
        "max_tokens": MAX_TOKENS, "fixture_version": FIXTURE_VERSION,
    }
    key = cache.key(case_id=case["case_id"], stage="tool_routing", method=method, config=config, prompt=text)
    cached = cache.get(key)
    if cached is not None:
        return cached

    gold = {"tool": case["gold_tool"], "arguments": case["gold_arguments"]}
    row = empty_record(case["case_id"], split, "tool_routing", method, gold)
    row.update(candidate_count=len(candidates), routing_latency_ms=routing_ms, execution_performed=False)
    candidate_names = {item.name for item in candidates}
    try:
        result, telemetry = await client.structured(ToolChoice, text)
        selection_ok = result.tool == case["gold_tool"]
        exact_arguments = normalized(result.arguments) == normalized(case["gold_arguments"])
        valid = arguments_valid(result, candidates)
        authorized = result.tool in candidate_names
        success = selection_ok and exact_arguments and valid and authorized
        if not authorized:
            error_type = "unauthorized_tool"
        elif not selection_ok:
            error_type = "selection_error"
        elif not valid:
            error_type = "invalid_arguments"
        elif not exact_arguments:
            error_type = "argument_mismatch"
        else:
            error_type = None
        row.update(
            success=success,
            prediction=result.model_dump(),
            selection_correct=selection_ok,
            argument_exact_match=exact_arguments,
            argument_valid=valid,
            execution_valid=authorized and valid,
            unauthorized_tool=not authorized,
            error_type=error_type,
            **telemetry,
        )
        row["total_latency_ms"] = round(routing_ms + telemetry["llm_latency_ms"], 3)
    except Exception as exc:
        row.update(error_type=type(exc).__name__, error=str(exc), api_calls=1, total_latency_ms=routing_ms)
    validate_record(row)
    cache.put(key, row)
    return row


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = telemetry_summary(rows)
    result.update({
        "selection_accuracy": mean(bool(row.get("selection_correct")) for row in rows),
        "argument_exact_match": mean(bool(row.get("argument_exact_match")) for row in rows),
        "argument_valid_rate": mean(bool(row.get("argument_valid")) for row in rows),
        "execution_valid_rate": mean(bool(row.get("execution_valid")) for row in rows),
        "unauthorized_tool_rate": mean(bool(row.get("unauthorized_tool")) for row in rows),
        "average_candidate_count": mean(row["candidate_count"] for row in rows),
        "candidate_reduction_rate": 1 - mean(row["candidate_count"] for row in rows) / len(TOOL_SPECS),
        "actual_executions": 0,
    })
    return result


async def main_async(split: str, methods: list[str], concurrency: int) -> None:
    api_base = os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("LLM_MODEL") or os.getenv("LLM_MODEL_NAME")
    if not (api_base and api_key and model):
        raise RuntimeError("real LLM environment is not configured")
    context, step_tools = load_protocol()
    cases = split_cases("tool_routing", split)
    registry = ToolRegistry()
    cache = ResultCache()
    client = RealLLMClient(
        api_base=api_base, api_key=api_key, model=model, concurrency=concurrency,
        enable_thinking=ENABLE_THINKING, max_tokens=MAX_TOKENS,
    )
    try:
        for method in methods:
            rows = await asyncio.gather(*(
                run_case(client, cache, registry, case, split, method, context, step_tools) for case in cases
            ))
            config = {
                "stage": "tool_routing", "split": split, "method": method,
                "model": MODEL, "prompt_version": PROMPT_VERSION, "concurrency": concurrency,
                "execution_performed": False, "enable_thinking": ENABLE_THINKING,
                "max_tokens": MAX_TOKENS, "fixture_version": FIXTURE_VERSION,
            }
            result = metrics(rows)
            create_run(f"tool_{split}_{method.lower()}_context_v2", config, rows, result)
            print(method, result, flush=True)
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--methods", nargs="+", default=["T0_all", "T1_resource_permission", "T2_skill", "T3_plan_step"])
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    asyncio.run(main_async(args.split, args.methods, args.concurrency))


if __name__ == "__main__":
    main()
