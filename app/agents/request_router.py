from __future__ import annotations

import re
import os
from typing import Any

from app.models.schemas import Capability, RequestIntent, ResourceSummary
from app.services.llm_config import llm_settings


FILE_WORDS = ("csv", "excel", "xlsx", "文件", "表格", "model_v1", "model_v2", "预测误差", "模型误差")
DB_WORDS = ("数据库", "database", "training_db", "训练覆盖", "训练数据", "覆盖情况", "coverage", "样本覆盖", "数据集")
MODEL_WORDS = ("predict_rt", "预测 rt", "预测保留时间")
COMPLEX_WORDS = ("比较", "分析为什么", "并检查", "以及", "综合", "compare", "why")
MCP_WORDS = ("smiles", "分子特征", "molecule features", "molecule_id", "分子 id", "分子id")


class RequestRouter:
    """Deterministic-first router with a stable structured result.

    An optional LLM adapter can refine ambiguous semantics later, but can only select
    capabilities present in ResourceSummary.
    """

    def __init__(self, llm: Any | None = None):
        self.llm = llm

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
        return RequestIntent(
            goal=query.strip(),
            domain=domain,
            task_type=task_type,
            complexity="complex" if complex_task else "simple",
            required_capabilities=required,
            need_planning=complex_task,
            reason="deterministic resource-aware routing",
        )

    def _configured_llm(self):
        if self.llm is not None:
            return self.llm
        settings = llm_settings()
        if not settings.configured:
            return None
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.api_base,
            temperature=0,
            extra_body={"enable_thinking": False} if (settings.model or "").startswith("qwen") else None,
        )

    async def route_async(self, query: str, resources: ResourceSummary) -> RequestIntent:
        deterministic = self.route(query, resources)
        # Explicit filenames plus an authorized datasource are a structural
        # resource constraint, not an ambiguous semantic classification.
        if deterministic.task_type == "mixed_analysis" and re.search(r"[\w.-]+\.(?:csv|xlsx|xls)", query, re.I):
            deterministic.reason += "; explicit file+database resource constraint"
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
        try:
            candidate = await llm.with_structured_output(RequestIntent).ainvoke(prompt)
        except Exception as exc:
            deterministic.reason += f"; LLM fallback after {type(exc).__name__}"
            return deterministic
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
        return candidate

