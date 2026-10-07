from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
from statistics import mean
from time import perf_counter
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI

from evaluation.metrics import percentile
from evaluation.phase4_common import (
    MODEL,
    PROMPT_VERSION,
    ResultCache,
    cost_cny,
    create_run,
    empty_record,
    split_cases,
    telemetry_summary,
    validate_record,
)


ENABLE_THINKING = False
MAX_TOKENS = 2048
DATASET_VERSION = "train_v3"


class UsageCallback(BaseCallbackHandler):
    def __init__(self):
        self.lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.calls = 0

    def on_llm_end(self, response, **kwargs: Any) -> None:
        usage = (getattr(response, "llm_output", None) or {}).get("token_usage", {}) or {}
        if not usage and getattr(response, "generations", None):
            try:
                message = response.generations[0][0].message
                usage = getattr(message, "usage_metadata", None) or {}
            except Exception:
                usage = {}
        input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
        total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
        with self.lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.total_tokens += total_tokens
            self.calls += 1

    def snapshot(self) -> tuple[int, int, int, int]:
        with self.lock:
            return self.input_tokens, self.output_tokens, self.total_tokens, self.calls


def delta(before: tuple[int, int, int, int], after: tuple[int, int, int, int]) -> dict[str, int]:
    values = [max(0, b - a) for a, b in zip(before, after, strict=True)]
    return dict(zip(("input_tokens", "output_tokens", "total_tokens", "api_calls"), values, strict=True))


async def collect(stream, events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    events = events if events is not None else []
    async for item in stream:
        events.append({"event": item.event, "message": item.message, "data": item.data})
    return events


def trace_flags(events: list[dict[str, Any]]) -> dict[str, bool]:
    names = {item["event"] for item in events}
    intent = "INTENT_RESOLVED" in names
    skills = any(item["event"] == "INTENT_RESOLVED" and "selected_skills" in item["data"] for item in events)
    plan = "PLAN_CREATED" in names
    tools = "TOOL_FINISHED" in names
    evidence = "EVIDENCE_ADDED" in names
    return {"intent": intent, "skills": skills, "plan": plan, "tools": tools, "evidence": evidence}


async def run_case(agent, usage: UsageCallback, cache: ResultCache, case: dict[str, Any], split: str) -> dict[str, Any]:
    config = {
        "stage": "e2e", "model": MODEL, "prompt_version": PROMPT_VERSION,
        "enable_thinking": ENABLE_THINKING, "max_tokens": MAX_TOKENS,
        "dataset_version_on_resume": DATASET_VERSION,
    }
    key = cache.key(case_id=case["case_id"], stage="e2e", method="E2E_agent", config=config, prompt=case["query"])
    cached = cache.get(key)
    if cached is not None:
        return cached
    row = empty_record(case["case_id"], split, "e2e", "E2E_agent", case)
    row.update(candidate_count=0)
    user_id = "phase4-eval"
    thread_id = f"phase4-{case['case_id']}"
    before = usage.snapshot()
    started = perf_counter()
    events: list[dict[str, Any]] = []
    runtime_error: Exception | None = None
    initial_count = 0
    try:
        await collect(agent.stream(
            case["query"], user_id, thread_id,
            datasource_id="training_db",
            skip_hitl=not case["hitl_required"],
        ), events)
        initial_count = len(events)
        waited = any(item["event"] == "WAITING_FOR_USER" for item in events)
        if waited:
            await collect(agent.resume(thread_id, DATASET_VERSION, user_id), events)
    except Exception as exc:
        runtime_error = exc
        waited = any(item["event"] == "WAITING_FOR_USER" for item in events[:initial_count or len(events)])
    elapsed = (perf_counter() - started) * 1000
    usage_delta = delta(before, usage.snapshot())
    flags = trace_flags(events)
    required = case["required_trace"]
    evidence_coverage = sum(flags.get(name, False) for name in required) / len(required)
    error_events = [item for item in events if item["event"] == "ERROR"]
    final_events = [item for item in events if item["event"] == "FINAL_ANSWER"]
    artifacts = [item["data"].get("artifact") for item in events if item["event"] == "ARTIFACT_CREATED"]
    evidence = [item["data"].get("evidence") for item in events if item["event"] == "EVIDENCE_ADDED"]
    tool_finished = [item for item in events if item["event"] == "TOOL_FINISHED"]
    tool_successes = [
        bool((item["data"].get("result") or {}).get("success", True)) for item in tool_finished
    ]
    fallback = "fallback" in json.dumps(events, ensure_ascii=False, default=str).lower()
    hitl_resume_success = bool(waited and len(events) > initial_count and final_events and not error_events and runtime_error is None)
    artifact_correct = bool(artifacts) if case["requires_artifact"] else True
    task_success = bool(final_events and evidence and not error_events and runtime_error is None)
    prediction = {
        "answer": final_events[-1]["data"].get("answer") if final_events else None,
        "trace_flags": flags,
        "event_count": len(events),
        "events": events,
    }
    row.update(
        success=task_success,
        prediction=prediction,
        input_tokens=usage_delta["input_tokens"],
        output_tokens=usage_delta["output_tokens"],
        total_tokens=usage_delta["total_tokens"],
        api_calls=usage_delta["api_calls"],
        llm_latency_ms=0.0,
        total_latency_ms=round(elapsed, 3),
        fallback=fallback,
        error_type=(
            type(runtime_error).__name__ if runtime_error is not None
            else error_events[-1]["data"].get("error", "event_error") if error_events else None
        ),
        cost_estimate=cost_cny(usage_delta["input_tokens"], usage_delta["output_tokens"]),
        task_success=task_success,
        evidence_coverage=evidence_coverage,
        evidence_count=len(evidence),
        tool_calls=len(tool_finished),
        tool_success_rate=mean(tool_successes) if tool_successes else 0.0,
        artifact_correct=artifact_correct,
        artifact_count=len(artifacts),
        hitl_required=case["hitl_required"],
        hitl_waited=waited,
        hitl_resume_success=hitl_resume_success,
        final_answer_present=bool(final_events),
        actual_model=MODEL,
        real_llm=usage_delta["api_calls"] > 0,
        thread_id=thread_id,
    )
    if runtime_error is not None:
        row["error"] = str(runtime_error)
    validate_record(row)
    cache.put(key, row)
    return row


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = telemetry_summary(rows)
    calls = [row["tool_calls"] for row in rows]
    hitl = [row for row in rows if row["hitl_required"]]
    result.update({
        "task_success_rate": mean(bool(row["task_success"]) for row in rows),
        "evidence_coverage": mean(float(row["evidence_coverage"]) for row in rows),
        "tool_success_rate": mean(float(row["tool_success_rate"]) for row in rows),
        "hitl_resume_success_rate": mean(bool(row["hitl_resume_success"]) for row in hitl) if hitl else 0.0,
        "artifact_correctness": mean(bool(row["artifact_correct"]) for row in rows),
        "avg_tool_calls": mean(calls),
        "tool_calls_p50": percentile(calls, 0.5),
        "tool_calls_p95": percentile(calls, 0.95),
        "real_llm_rate": mean(bool(row["real_llm"]) for row in rows),
    })
    return result


async def main_async(split: str) -> None:
    api_base = os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model_name = os.getenv("LLM_MODEL") or os.getenv("LLM_MODEL_NAME")
    if not (api_base and api_key and model_name):
        raise RuntimeError("real LLM environment is not configured")
    if model_name != MODEL:
        raise RuntimeError(f"expected {MODEL}, got {model_name}")
    usage = UsageCallback()
    llm = ChatOpenAI(
        model=model_name, api_key=api_key, base_url=api_base, temperature=0,
        max_tokens=MAX_TOKENS, extra_body={"enable_thinking": ENABLE_THINKING},
        max_retries=0, callbacks=[usage],
    )
    from app.agents.scientific_agent import ScientificAgent

    agent = ScientificAgent()
    agent.router.llm = llm
    agent.tool_registry.llm = llm
    agent.text2sql.llm = llm
    agent.deep_runtime._model = lambda: llm
    cases = split_cases("e2e", split)
    cache = ResultCache()
    rows = []
    for index, case in enumerate(cases, 1):
        try:
            row = await run_case(agent, usage, cache, case, split)
        except Exception as exc:
            row = empty_record(case["case_id"], split, "e2e", "E2E_agent", case)
            row.update(error_type=type(exc).__name__, error=str(exc), api_calls=0, task_success=False,
                       evidence_coverage=0.0, tool_calls=0, tool_success_rate=0.0,
                       artifact_correct=not case["requires_artifact"], hitl_required=case["hitl_required"],
                       hitl_resume_success=False, real_llm=False)
            validate_record(row)
        rows.append(row)
        print(f"{index}/{len(cases)} {case['case_id']} success={row['success']} error={row['error_type']}", flush=True)
    result = metrics(rows)
    config = {
        "stage": "e2e", "split": split, "method": "E2E_agent", "model": MODEL,
        "prompt_version": PROMPT_VERSION, "enable_thinking": ENABLE_THINKING,
        "max_tokens": MAX_TOKENS, "database_backend": "postgres",
        "checkpoint_backend": "postgres" if agent.checkpointing.persistent else "memory",
        "object_storage": "minio" if agent.storage.configured else "local",
    }
    create_run(f"e2e_{split}_agent", config, rows, result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev"], default="dev")
    args = parser.parse_args()
    asyncio.run(main_async(args.split))


if __name__ == "__main__":
    main()
