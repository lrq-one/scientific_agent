from __future__ import annotations
from app.services.resource_grounding import mentioned

import re
from time import perf_counter
import os
from typing import Any

from app.models.schemas import Capability, RequestIntent, ResourceSummary
from app.services.llm_config import llm_settings


FILE_WORDS = ("csv", "excel", "xlsx", "文件", "表格", "model_v1", "model_v2", "预测误差", "模型误差")
DB_WORDS = ("数据库", "database", "training_db", "训练覆盖", "训练数据", "覆盖情况", "coverage", "样本覆盖", "数据集")
MODEL_WORDS = ("predict_rt", "预测 rt", "预测保留时间")
COMPLEX_WORDS = ("比较", "分析为什么", "并检查", "以及", "综合", "compare", "why")
MCP_WORDS = ("smiles", "分子特征", "molecule features", "molecule_id", "分子 id", "分子id")


def resource_constraint(query: str) -> str:
    text = query.lower()
    files = bool(re.search(r"[\w.-]+\.(?:csv|xlsx|xls)\b|\b(?:csv|xlsx|excel)\b|文件|表格", text))
    database = any(word in text for word in DB_WORDS) or bool(re.search(r"train[_-]?v\d+", text))
    if files:
        return "MIXED" if database else "FILE_ONLY"
    if database:
        return "DATABASE_ONLY"
    if any(word in text for word in MCP_WORDS) or re.search(r"\b[mt]\d{3,}\b", text):
        return "MCP"
    return "AUTO"


def validate_intent_against_resource_constraints(intent: RequestIntent, query: str, resources: ResourceSummary) -> RequestIntent:
    constraint = resource_constraint(query)
    grounded = {
        "FILE_ONLY": ("file_analysis", [Capability.FILE]),
        "DATABASE_ONLY": ("database_analysis", [Capability.DATABASE]),
        "MIXED": ("mixed_analysis", [Capability.FILE, Capability.DATABASE]),
        "MCP": ("general", [Capability.MCP]),
    }.get(constraint)
    if grounded:
        intent.task_type, intent.required_capabilities = grounded
        intent.required_capabilities = [cap for cap in intent.required_capabilities
            if cap == Capability.FILE or (cap == Capability.DATABASE and resources.authorized_datasources)
            or (cap == Capability.MCP and resources.available_mcp_tools)]
        intent.reason += f"; hard resource constraint={constraint}"
        if constraint == "MIXED" and resources.available_mcp_tools and (
            any(word in query.lower() for word in MCP_WORDS) or re.search(r"\b[MT]\d{3,}\b", query, flags=re.I)
        ):
            intent.required_capabilities.append(Capability.MCP)
    if not resources.available_scientific_models:
        intent.required_capabilities = [cap for cap in intent.required_capabilities if cap != Capability.SCIENTIFIC_MODEL]
    intent.goal = query.strip()
    return intent


class RequestRouter:
    """Deterministic-first router with a stable structured result.

    An optional LLM adapter can refine ambiguous semantics later, but can only select
    capabilities present in ResourceSummary.
    """

    def __init__(self, llm: Any | None = None):
        self.llm = llm

    def context_hint(self, query: str, resources: ResourceSummary) -> dict[str, Any]:
        """Fact grounding only. Does not choose an intent, plan or tool."""
        mentioned_files = list(dict.fromkeys(re.findall(r"[\w.-]+\.(?:csv|xlsx|xls)\b", query, re.I)))
        return {
            "mentioned_files": mentioned_files,
            "missing_files": [name for name in mentioned_files if name not in resources.available_files],
            "mentioned_datasources": [name for name in resources.authorized_datasources if name in query],
            "dataset_versions": [str(v.get("version", v.get("version_name", v.get("label"))))
                for v in resources.resource_metadata.get("dataset_versions", [])
                if any(mentioned(v.get(k), query)
                       for k in ("id", "version", "version_name", "label"))],
            "resources": resources.model_dump(mode="json"),
        }

    def route(self, query: str, resources: ResourceSummary) -> RequestIntent:
        text = query.strip().lower()
        mentions_file = any(word in text for word in FILE_WORDS)
        mentions_db = any(word in text for word in DB_WORDS)
        mentions_model = any(word in text for word in MODEL_WORDS)
        mentions_mcp = any(word in text for word in MCP_WORDS) or bool(
            re.search(r"\b(?:molecule\s*)?(?:M|T)\d{3,}\b", query, flags=re.I)
        )

        required: list[Capability] = []
        if mentions_file and resources.available_files:
            required.append(Capability.FILE)
        if mentions_db and resources.authorized_datasources:
            required.append(Capability.DATABASE)
        if mentions_model and resources.available_scientific_models:
            required.append(Capability.SCIENTIFIC_MODEL)
        if mentions_mcp and resources.available_mcp_tools:
            required.append(Capability.MCP)

        if mentions_file and mentions_db:
            task_type = "mixed_analysis"
        elif mentions_file:
            task_type = "file_analysis"
        elif mentions_db:
            task_type = "database_analysis"
        elif mentions_model:
            task_type = "scientific_model"
        else:
            task_type = "general"

        distinct_resources = len(set(re.findall(r"[\w.-]+\.(?:csv|xlsx|xls)", text)))
        complex_task = (
            task_type == "mixed_analysis"
            or distinct_resources > 1
            or sum(word in text for word in COMPLEX_WORDS) >= 2
        )
        domain = "mass_spec" if any(w in text for w in ("rt", "质谱", "fused", "分子", "smiles")) else "general"
        return validate_intent_against_resource_constraints(RequestIntent(
            goal=query.strip(),
            domain=domain,
            task_type=task_type,
            complexity="complex" if complex_task else "simple",
            required_capabilities=required,
            need_planning=complex_task,
            reason="deterministic resource-aware routing",
        ), query, resources)

    def _configured_llm(self):
        if self.llm is not None:
            return self.llm
        settings = llm_settings()
        if not settings.configured:
            return None
        from langchain_openai import ChatOpenAI
        from app.services.live_budget import live_max_retries

        return ChatOpenAI(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.api_base,
            temperature=0, max_retries=live_max_retries(1),
            extra_body={"enable_thinking": False} if (settings.model or "").startswith("qwen") else None,
        )

    async def route_async(self, query: str, resources: ResourceSummary) -> RequestIntent:
        deterministic = self.route(query, resources)
        # Explicit filenames plus an authorized datasource are a structural
        # resource constraint, not an ambiguous semantic classification.
        if resource_constraint(query) in {"FILE_ONLY", "MIXED", "MCP"}:
            deterministic.reason += "; explicit file+database resource constraint" if resource_constraint(query) == "MIXED" else "; explicit resource grounding"
            return deterministic
        # Unambiguous one-resource tasks stay on the zero-latency fast path.
        if deterministic.complexity == "simple" and deterministic.task_type in {"file_analysis", "database_analysis"}:
            return deterministic
        llm = self._configured_llm()
        if llm is None:
            deterministic.reason += "; LLM unavailable, deterministic fallback used"
            return deterministic
        prompt = (
            "Return a structured RequestIntent. Interpret semantics only; never invent resources.\n"
            f"User query: {query}\n"
            f"Available files: {resources.available_files}\n"
            f"Authorized datasources: {resources.authorized_datasources}\n"
            f"Available scientific models: {resources.available_scientific_models}\n"
            f"Available MCP tools: {resources.available_mcp_tools}"
        )
        from app.services.live_budget import record_live_call, reserve_live_call
        from app.services.llm_telemetry import UsageCollector
        collector = UsageCollector(); started = perf_counter()
        reserve_live_call("request_router", max(1, len(prompt) // 4 + 900))
        try:
            runnable = llm.with_structured_output(RequestIntent)
            try:
                candidate = await runnable.ainvoke(prompt, config={"callbacks": [collector]})
            except TypeError as callback_error:
                # Small in-process test doubles may expose the original
                # ``ainvoke(prompt)`` contract only; real LangChain providers
                # retain callback telemetry in the first path.
                if "config" not in str(callback_error) and "keyword" not in str(callback_error):
                    raise
                candidate = await runnable.ainvoke(prompt)
        except Exception as exc:
            record_live_call("request_router", collector.snapshot(), latency_ms=(perf_counter() - started) * 1000, error=type(exc).__name__)
            deterministic.reason += f"; LLM fallback after {type(exc).__name__}"
            return deterministic
        record_live_call("request_router", collector.snapshot(), latency_ms=(perf_counter() - started) * 1000)
        allowed = set()
        if resources.available_files:
            allowed.add(Capability.FILE)
        if resources.authorized_datasources:
            allowed.add(Capability.DATABASE)
        if resources.available_scientific_models:
            allowed.add(Capability.SCIENTIFIC_MODEL)
        if resources.available_mcp_tools:
            allowed.add(Capability.MCP)
        candidate.required_capabilities = [cap for cap in candidate.required_capabilities if cap in allowed]
        candidate.goal = query.strip()
        candidate.reason = f"LLM structured understanding; resource-filtered. {candidate.reason}"
        return validate_intent_against_resource_constraints(candidate, query, resources)

