from __future__ import annotations

import asyncio
import json
from time import perf_counter
from typing import Any

import httpx
from langchain_openai import ChatOpenAI

from app.agents.request_router import RequestRouter
from app.models.schemas import RequestIntent, ResourceSummary, SQLCandidate
from app.services.llm_config import llm_settings
from app.services.skills import SkillService
from app.services.text2sql import TextToSQLService
from app.tools.registry import ToolChoice, ToolRegistry
from app.tools.sql_guard import SQLGuard


QUERY = "比较 model_v1.csv 和 model_v2.csv 的 RT 预测表现，并分析误差主要集中在哪些结构类型。"


def usage(raw: Any) -> dict[str, int]:
    direct = getattr(raw, "usage_metadata", None)
    if direct:
        return {
            "input_tokens": int(direct.get("input_tokens", 0)),
            "output_tokens": int(direct.get("output_tokens", 0)),
            "total_tokens": int(direct.get("total_tokens", 0)),
        }
    metadata = getattr(raw, "response_metadata", {}) or {}
    token_usage = metadata.get("token_usage", {}) or {}
    return {
        "input_tokens": int(token_usage.get("prompt_tokens", 0)),
        "output_tokens": int(token_usage.get("completion_tokens", 0)),
        "total_tokens": int(token_usage.get("total_tokens", 0)),
    }


def actual_model(raw: Any, requested: str) -> str:
    metadata = getattr(raw, "response_metadata", {}) or {}
    return str(metadata.get("model_name") or metadata.get("model") or requested)


async def structured_call(llm: ChatOpenAI, schema, prompt: str, statuses: list[int]) -> tuple[Any, dict]:
    before = len(statuses)
    start = perf_counter()
    result = await llm.with_structured_output(schema, include_raw=True).ainvoke(prompt)
    latency_ms = round((perf_counter() - start) * 1000, 2)
    if result.get("parsing_error") is not None or result.get("parsed") is None:
        raise RuntimeError(f"structured output parsing failed: {type(result.get('parsing_error')).__name__}")
    raw = result["raw"]
    return result["parsed"], {
        "model": actual_model(raw, llm.model_name),
        "http_status": statuses[-1] if len(statuses) > before else None,
        "token_usage": usage(raw),
        "latency_ms": latency_ms,
        "tool_call_present": bool(getattr(raw, "tool_calls", None)),
    }


async def tool_call_smoke(llm: ChatOpenAI, prompt: str, statuses: list[int]) -> tuple[ToolChoice, dict]:
    before = len(statuses)
    start = perf_counter()
    raw = await llm.bind_tools([ToolChoice], tool_choice="ToolChoice").ainvoke(prompt)
    latency_ms = round((perf_counter() - start) * 1000, 2)
    if not raw.tool_calls:
        raise RuntimeError("model returned no ToolCall")
    call = raw.tool_calls[0]
    choice = ToolChoice.model_validate(call["args"])
    return choice, {
        "model": actual_model(raw, llm.model_name),
        "http_status": statuses[-1] if len(statuses) > before else None,
        "token_usage": usage(raw),
        "latency_ms": latency_ms,
        "tool_call_present": True,
        "tool_call_name": call["name"],
        "tool_call_id_present": bool(call.get("id")),
    }


async def main() -> int:
    settings = llm_settings()
    configuration = {
        "api_base_set": bool(settings.api_base),
        "api_key_set": bool(settings.api_key),
        "api_key_length": len(settings.api_key or ""),
        "model": settings.model,
        "source": settings.source,
    }
    if not settings.configured or not settings.api_base:
        print(json.dumps({
            "configuration": configuration,
            "real_llm": False,
            "fallback": True,
            "error": "LLM configuration is not visible to this process",
        }, ensure_ascii=False, indent=2))
        return 2
    if settings.model != "qwen3.7-flash":
        print(json.dumps({
            "configuration": configuration,
            "real_llm": False,
            "fallback": True,
            "error": f"unexpected model: {settings.model}",
        }, ensure_ascii=False, indent=2))
        return 2

    statuses: list[int] = []

    async def capture_status(response: httpx.Response) -> None:
        statuses.append(response.status_code)

    async with httpx.AsyncClient(event_hooks={"response": [capture_status]}, timeout=90) as http_client:
        llm = ChatOpenAI(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.api_base,
            temperature=0,
            http_async_client=http_client,
            max_retries=0,
        )

        resources = ResourceSummary(
            available_files=["model_v1.csv", "model_v2.csv"],
            authorized_datasources=["training_db"],
            available_mcp_tools=["get_molecule_features"],
        )
        router_prompt = (
            "Return a structured RequestIntent. Interpret semantics only; never invent resources.\n"
            f"User query: {QUERY}\n"
            f"Available files: {resources.available_files}\n"
            f"Authorized datasources: {resources.authorized_datasources}\n"
            f"Available scientific models: {resources.available_scientific_models}\n"
            f"Available MCP tools: {resources.available_mcp_tools}"
        )
        intent, intent_telemetry = await structured_call(llm, RequestIntent, router_prompt, statuses)

        skill_service = SkillService()
        skill_start = perf_counter()
        selected_skills = skill_service.select(QUERY, intent.task_type)
        skill_latency = round((perf_counter() - skill_start) * 1000, 2)

        registry = ToolRegistry(skills=skill_service)
        candidates = registry.candidates(
            available_capabilities={cap.value for cap in intent.required_capabilities} | {"artifact"},
            role="researcher",
            selected_skills=selected_skills,
            current_step_tools=None,
        )
        tool_prompt = (
            "Choose exactly one tool from the already authorized candidates for the current step. "
            "Return only a valid ToolChoice and do not execute the tool.\n"
            f"Goal: {QUERY}\nCurrent step: compare overall prediction metrics\n"
            f"Candidates: {json.dumps([item.model_dump() for item in candidates], ensure_ascii=False)}"
        )
        tool_choice, tool_telemetry = await tool_call_smoke(llm, tool_prompt, statuses)
        if tool_choice.tool not in {item.name for item in candidates}:
            raise RuntimeError(f"model selected unauthorized tool: {tool_choice.tool}")

        schema = {
            "training_molecules": [
                {"name": "molecule_id", "type": "text"},
                {"name": "dataset_version", "type": "text"},
                {"name": "is_cyclic", "type": "boolean"},
            ],
            "molecules": [
                {"name": "molecule_id", "type": "text"},
                {"name": "structure_type", "type": "text"},
            ],
        }
        relationships = [{
            "source_table": "training_molecules",
            "source_column": "molecule_id",
            "target_table": "molecules",
            "target_column": "molecule_id",
        }]
        sql_service = TextToSQLService(llm=llm)
        sql_prompt = sql_service.prompt(
            "统计 train_v3 中各 structure_type 的训练样本数量",
            "training-coverage",
            "training_db",
            schema,
            relationships,
        ) + "\nUse the dataset_version parameter value exactly 'train_v3'."
        candidate, sql_telemetry = await structured_call(llm, SQLCandidate, sql_prompt, statuses)
        guarded_sql = SQLGuard().validate(candidate.sql, set(schema), dialect="postgres")

    total_usage = {
        key: sum(item["token_usage"][key] for item in (intent_telemetry, tool_telemetry, sql_telemetry))
        for key in ("input_tokens", "output_tokens", "total_tokens")
    }
    report = {
        "configuration": configuration,
        "actual_runtime": {"real_llm": True, "fallback": False},
        "request_intent": {
            **intent_telemetry,
            "structured_output": intent.model_dump(mode="json", include={
                "goal", "domain", "task_type", "complexity", "required_capabilities", "need_planning"
            }),
            "fallback": False,
        },
        "skill_routing": {
            "selection_source": "deterministic_skill_contract_routing",
            "selected_skills": selected_skills,
            "latency_ms": skill_latency,
            "fallback": False,
        },
        "tool_selection": {
            **tool_telemetry,
            "candidate_tools": [item.name for item in candidates],
            "structured_output": tool_choice.model_dump(),
            "tool_executed": False,
            "fallback": False,
        },
        "text_to_sql": {
            **sql_telemetry,
            "structured_output": candidate.model_dump(),
            "guarded_sql": guarded_sql,
            "sqlglot_guard_passed": True,
            "sql_executed": False,
            "fallback": False,
        },
        "total_token_usage": total_usage,
        "all_http_statuses": statuses,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
