from __future__ import annotations

import asyncio
import json
import os
from statistics import mean
from time import perf_counter

from langchain_openai import ChatOpenAI

from evaluation.phase4_common import (
    MODEL, PROMPT_VERSION, ResultCache, cost_cny, create_run, empty_record,
    telemetry_summary, validate_record,
)
from evaluation.run_e2e import ENABLE_THINKING, MAX_TOKENS, UsageCallback, collect, delta


CASES = [
    {"case_id": "hitl-001", "query": "按数据集版本和 split 核对训练样本覆盖"},
    {"case_id": "hitl-002", "query": "检查 training_db 的训练覆盖情况"},
    {"case_id": "hitl-003", "query": "查询训练数据中各结构类型的样本覆盖"},
    {"case_id": "hitl-004", "query": "统计训练数据覆盖并按结构类型分组"},
    {"case_id": "hitl-005", "query": "核对训练样本覆盖情况与 fused_ring 数量"},
]


async def run_case(agent, usage: UsageCallback, cache: ResultCache, case: dict) -> dict:
    config = {
        "stage": "hitl_resume", "model": MODEL, "prompt_version": PROMPT_VERSION,
        "enable_thinking": ENABLE_THINKING, "max_tokens": MAX_TOKENS,
    }
    key = cache.key(case_id=case["case_id"], stage="hitl_resume", method="postgres_checkpoint", config=config, prompt=case["query"])
    cached = cache.get(key)
    if cached is not None:
        return cached
    row = empty_record(case["case_id"], "dev", "hitl_resume", "postgres_checkpoint", {
        "waiting": True, "resume_answer": "train_v3", "final": True,
    })
    thread_id = f"phase4-resume-{case['case_id']}"
    before = usage.snapshot()
    started = perf_counter()
    initial: list[dict] = []
    resumed: list[dict] = []
    runtime_error = None
    try:
        await collect(agent.stream(case["query"], "phase4-eval", thread_id, datasource_id="training_db"), initial)
        if any(item["event"] == "WAITING_FOR_USER" for item in initial):
            await collect(agent.resume(thread_id, "train_v3", "phase4-eval"), resumed)
    except Exception as exc:
        runtime_error = exc
    elapsed = (perf_counter() - started) * 1000
    tokens = delta(before, usage.snapshot())
    waited = any(item["event"] == "WAITING_FOR_USER" for item in initial)
    final = any(item["event"] == "FINAL_ANSWER" for item in resumed)
    error = next((item for item in reversed(resumed + initial) if item["event"] == "ERROR"), None)
    checkpoint_backend = next(
        (item["data"].get("checkpoint") for item in initial if item["event"] == "WAITING_FOR_USER"), None
    )
    success = bool(waited and final and not error and runtime_error is None and checkpoint_backend == "postgres")
    row.update(
        success=success,
        prediction={"initial_events": initial, "resume_events": resumed},
        candidate_count=0,
        input_tokens=tokens["input_tokens"], output_tokens=tokens["output_tokens"],
        total_tokens=tokens["total_tokens"], api_calls=tokens["api_calls"],
        total_latency_ms=round(elapsed, 3),
        fallback="fallback" in json.dumps(initial + resumed, ensure_ascii=False, default=str).lower(),
        error_type=(type(runtime_error).__name__ if runtime_error else "resume_error" if error else None),
        cost_estimate=cost_cny(tokens["input_tokens"], tokens["output_tokens"]),
        waited=waited, resumed=bool(resumed), final_answer_present=final,
        checkpoint_backend=checkpoint_backend, thread_id=thread_id,
        checkpoint_exists_after_wait=agent.checkpointing.checkpoint_exists(thread_id) if waited else False,
        actual_model=MODEL, real_llm=tokens["api_calls"] > 0,
    )
    if runtime_error:
        row["error"] = str(runtime_error)
    if error:
        row["error_event"] = error
    validate_record(row)
    cache.put(key, row)
    return row


async def main_async() -> None:
    api_base = os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model_name = os.getenv("LLM_MODEL") or os.getenv("LLM_MODEL_NAME")
    if not (api_base and api_key and model_name == MODEL):
        raise RuntimeError("real qwen3.7-flash environment is required")
    usage = UsageCallback()
    llm = ChatOpenAI(
        model=MODEL, api_key=api_key, base_url=api_base, temperature=0,
        max_tokens=MAX_TOKENS, extra_body={"enable_thinking": ENABLE_THINKING},
        max_retries=0, callbacks=[usage],
    )
    from app.agents.scientific_agent import ScientificAgent

    agent = ScientificAgent()
    agent.router.llm = llm
    agent.tool_registry.llm = llm
    agent.text2sql.llm = llm
    agent.deep_runtime._model = lambda: llm
    cache = ResultCache()
    rows = []
    for case in CASES:
        row = await run_case(agent, usage, cache, case)
        rows.append(row)
        print(case["case_id"], row["success"], row["error_type"], flush=True)
    result = telemetry_summary(rows)
    result.update({
        "wait_rate": mean(bool(row["waited"]) for row in rows),
        "checkpoint_persist_rate": mean(bool(row["checkpoint_exists_after_wait"]) for row in rows),
        "resume_success_rate": mean(bool(row["success"]) for row in rows),
        "final_answer_rate": mean(bool(row["final_answer_present"]) for row in rows),
    })
    config = {
        "stage": "hitl_resume", "split": "dev", "method": "postgres_checkpoint",
        "model": MODEL, "prompt_version": PROMPT_VERSION,
        "enable_thinking": ENABLE_THINKING, "max_tokens": MAX_TOKENS,
        "checkpoint_backend": "postgres" if agent.checkpointing.persistent else "memory",
    }
    create_run("hitl_resume_dev_postgres", config, rows, result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main_async())
