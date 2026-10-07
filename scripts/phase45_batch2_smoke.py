"""One real Qwen structured follow-up classification; no SQL or benchmark run."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import httpx
from langchain_openai import ChatOpenAI

from app.models.schemas import FollowUpClassification
from app.services.followup import ConversationContextResolver, conversation_context_summary
from app.services.llm_config import llm_settings
from scripts.smoke_real_llm import actual_model, usage


async def main() -> None:
    settings = llm_settings()
    if not settings.configured or settings.model != "qwen3.7-flash":
        raise RuntimeError("qwen3.7-flash configuration unavailable; API key is never printed")
    context = {
        "task": {"id": str(uuid.uuid4()), "conversation_id": str(uuid.uuid4()),
                 "status": "completed", "intent_json": {"goal": "统计结构覆盖"}},
        "messages": [{"role": "user", "content": "统计训练集结构覆盖"}],
        "assistant_message": {"content": "fused_ring 样本数为 2；不能据此推断因果关系。"},
        "evidence": [{"id": str(uuid.uuid4()), "claim": "fused_ring 样本数", "source": "training_db",
                      "dataset_version": "train_v3"}],
    }
    query = "它靠谱吗？"
    summary = conversation_context_summary(query, [context])
    prompt = (
        "Classify the current turn relative to the previous completed scientific task. "
        "Choose exactly one follow_up_type. Explanation/evidence/error questions reuse persisted history; "
        "refine/rerun/continue may enter the workflow; unrelated work is NEW_TASK.\n"
        f"Bounded conversation context: {summary.model_dump_json(exclude_none=True)}"
    )
    statuses: list[int] = []

    async def capture(response: httpx.Response) -> None:
        statuses.append(response.status_code)

    async with httpx.AsyncClient(event_hooks={"response": [capture]}, timeout=90) as client:
        llm = ChatOpenAI(
            model=settings.model, api_key=settings.api_key, base_url=settings.api_base,
            temperature=0, max_tokens=256, extra_body={"enable_thinking": False},
            http_async_client=client, max_retries=0,
        )
        started = time.perf_counter()
        response = await llm.with_structured_output(FollowUpClassification, include_raw=True).ainvoke(prompt)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
    parsed = response.get("parsed")
    if response.get("parsing_error") or parsed is None:
        raise RuntimeError("structured follow-up classification failed")
    parsed.source = "llm_structured"
    resolved = ConversationContextResolver._attach_target(parsed, query, [context])
    result = {
        "real_llm": True,
        "fallback": False,
        "model": actual_model(response["raw"], settings.model),
        "http_status": statuses[-1] if statuses else None,
        "token_usage": usage(response["raw"]),
        "latency_ms": latency_ms,
        "structured_output": resolved.model_dump(),
        "new_tool_calls": 0,
    }
    if result["http_status"] != 200 or result["model"] != "qwen3.7-flash":
        raise RuntimeError("unexpected Qwen response status/model")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
