from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Any

import httpx
from langchain_openai import ChatOpenAI

from evaluation.phase4_common import MODEL, cost_cny


def token_usage(raw: Any) -> dict[str, int]:
    direct = getattr(raw, "usage_metadata", None) or {}
    if direct:
        return {
            "input_tokens": int(direct.get("input_tokens", 0)),
            "output_tokens": int(direct.get("output_tokens", 0)),
            "total_tokens": int(direct.get("total_tokens", 0)),
        }
    metadata = getattr(raw, "response_metadata", {}) or {}
    usage = metadata.get("token_usage", {}) or {}
    return {
        "input_tokens": int(usage.get("prompt_tokens", 0)),
        "output_tokens": int(usage.get("completion_tokens", 0)),
        "total_tokens": int(usage.get("total_tokens", 0)),
    }


class RealLLMClient:
    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        concurrency: int = 8,
        enable_thinking: bool | None = None,
        max_tokens: int | None = None,
    ):
        if model != MODEL:
            raise ValueError(f"Phase 4 model must be {MODEL}, got {model}")
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(150),
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
            trust_env=False,
        )
        extra_body = {"enable_thinking": enable_thinking} if enable_thinking is not None else None
        self.llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=api_base,
            temperature=0,
            max_tokens=max_tokens,
            extra_body=extra_body,
            max_retries=0,
            http_async_client=self.http,
        )
        self.semaphore = asyncio.Semaphore(concurrency)

    async def close(self) -> None:
        await self.http.aclose()

    async def structured(self, schema: Any, prompt: str) -> tuple[Any, dict[str, Any]]:
        async with self.semaphore:
            started = perf_counter()
            result = await self.llm.with_structured_output(schema, include_raw=True).ainvoke(prompt)
            latency = round((perf_counter() - started) * 1000, 2)
        if result.get("parsing_error") is not None or result.get("parsed") is None:
            error = result.get("parsing_error")
            raise RuntimeError(f"structured_output_error:{type(error).__name__}")
        raw = result["raw"]
        usage = token_usage(raw)
        metadata = getattr(raw, "response_metadata", {}) or {}
        actual_model = str(metadata.get("model_name") or metadata.get("model") or self.llm.model_name)
        return result["parsed"], {
            **usage,
            "llm_latency_ms": latency,
            "actual_model": actual_model,
            "fallback": False,
            "api_calls": 1,
            "cost_estimate": cost_cny(usage["input_tokens"], usage["output_tokens"]),
        }

