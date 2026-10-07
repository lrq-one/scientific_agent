from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from app.services.llm_config import llm_settings


InteractionType = Literal[
    "DELEGATE",
    "DIRECT_ANSWER",
    "CAPABILITY_QUESTION",
    "CLARIFY",
    "UNSUPPORTED",
    "CANCEL_ACTIVE",
]


class InteractionDecision(BaseModel):
    interaction_type: InteractionType
    reason: str = ""
    direct_answer: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str = "deterministic"


class ConversationPolicy:
    """Decide whether a user turn should execute the scientific workflow at all.

    The existing Follow-up Resolver and RequestRouter remain responsible for turns
    that actually need task/follow-up execution. This layer prevents greetings,
    capability questions, general explanations, unsupported live-data requests,
    and cancellation from being forced through database/file tools.
    """

    GREETING = re.compile(r"^(你好|您好|嗨|hello|hi|hey|谢谢|感谢|thanks)[！!。.]?$", re.I)
    CAPABILITY = re.compile(
        r"(你能(?:做|分析|处理|支持)|你会(?:做|分析|处理)|支持(?:什么|哪些|csv|excel|数据库|sql|质谱|mcp)|"
        r"能不能(?:分析|处理)|有哪些功能|可以(?:分析|处理).{0,20}(?:吗|么))",
        re.I,
    )
    CANCEL = re.compile(r"(取消|停止|别分析了|不要分析了|算了(?:吧)?|终止|stop|cancel)", re.I)
    LIVE_EXTERNAL = re.compile(
        r"(今天天气|明天天气|实时天气|现在股价|实时股价|最新新闻|今天新闻|实时航班|实时汇率|current weather|live price)",
        re.I,
    )
    TASK_SIGNAL = re.compile(
        r"(分析|统计|比较|查询|检查|计算|生成|读取|筛选|排序|预测|训练|数据库|training_db|"
        r"\.csv\b|\.xlsx\b|\.xls\b|sql|dataset|model[_-]?v\d+|train[_-]?v\d+|mcp|"
        r"fused[-_ ]?ring|质谱|谱图|保留时间|rt\b)",
        re.I,
    )
    CONTEXT_SIGNAL = re.compile(
        r"(刚才|上一轮|上次|前面|这个结果|这些结果|这个结论|这些证据|它|那换|继续|重跑|重新|证据|依据)",
        re.I,
    )
    VAGUE_ANALYSIS = re.compile(r"^(帮我|请)?(?:分析|看看|查一下|处理一下)(?:这个|一下)?[？?。.]?$", re.I)

    def __init__(self, llm=None):
        self.llm = llm

    @staticmethod
    def capability_answer(resources) -> str:
        capabilities: list[str] = []
        if getattr(resources, "available_files", []):
            capabilities.append("CSV/Excel 文件分析")
        else:
            capabilities.append("CSV/Excel 文件上传与分析")
        if getattr(resources, "authorized_datasources", []):
            capabilities.append("PostgreSQL 科研数据只读查询与 Text-to-SQL")
        if getattr(resources, "available_mcp_tools", []):
            capabilities.append("MCP 科研工具调用")
        if getattr(resources, "available_scientific_models", []):
            capabilities.append("已配置科研模型推理")
        capabilities.extend(["多轮证据追溯", "HITL/Checkpoint 恢复", "结果表格/图表产物"])
        model_note = (
            "当前检测到可用科研模型。"
            if getattr(resources, "available_scientific_models", [])
            else "当前没有可验证的真实科研模型权重，因此不会伪造模型推理结果。"
        )
        return "目前可处理：" + "、".join(capabilities) + "。\n\n" + model_note

    def deterministic(self, query: str, *, has_context: bool, active_task: bool, resources) -> InteractionDecision | None:
        text = " ".join(query.strip().split())
        if not text:
            return InteractionDecision(
                interaction_type="CLARIFY",
                reason="empty user turn",
                direct_answer="请告诉我你希望分析什么数据、结果或科研问题。",
            )
        if active_task and self.CANCEL.search(text):
            return InteractionDecision(
                interaction_type="CANCEL_ACTIVE",
                reason="explicit cancellation while task is active",
            )
        if self.GREETING.match(text):
            return InteractionDecision(
                interaction_type="DIRECT_ANSWER",
                reason="greeting",
                direct_answer="你好。你可以直接描述科研问题，或上传 CSV/Excel、选择数据库后让我分析。",
            )
        if self.CAPABILITY.search(text):
            return InteractionDecision(
                interaction_type="CAPABILITY_QUESTION",
                reason="explicit capability question",
                direct_answer=self.capability_answer(resources),
            )
        if self.LIVE_EXTERNAL.search(text):
            return InteractionDecision(
                interaction_type="UNSUPPORTED",
                reason="requires live external data not exposed as an authorized capability",
                direct_answer="当前 Scientific Agent 没有对应的实时外部数据源，因此不能可靠获取这类实时信息。",
            )
        if self.VAGUE_ANALYSIS.match(text):
            return InteractionDecision(
                interaction_type="CLARIFY",
                reason="analysis request lacks target/resource",
                direct_answer="可以。请说明要分析的对象，例如文件名、数据库/数据集版本、模型结果，或你想回答的具体科研问题。",
            )
        if self.CONTEXT_SIGNAL.search(text) and has_context:
            return InteractionDecision(interaction_type="DELEGATE", reason="conversation-context turn")
        if self.TASK_SIGNAL.search(text):
            return InteractionDecision(interaction_type="DELEGATE", reason="scientific execution signal")
        return None

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

    async def resolve_async(self, query: str, *, has_context: bool, active_task: bool, resources) -> InteractionDecision:
        deterministic = self.deterministic(
            query,
            has_context=has_context,
            active_task=active_task,
            resources=resources,
        )
        if deterministic is not None:
            return deterministic

        llm = self._configured_llm()
        if llm is None:
            return InteractionDecision(
                interaction_type="DIRECT_ANSWER",
                reason="LLM unavailable; safe non-tool response",
                direct_answer=(
                    "我理解这是一个不需要当前科研工具链直接执行的问题，但目前没有可用 LLM 来生成可靠回答。"
                    "如果你希望执行文件、数据库或科研分析，请把目标和数据来源说具体一些。"
                ),
                source="deterministic_fallback",
                confidence=0.5,
            )

        prompt = f"""
You are the conversation policy layer of a Scientific Agent.
Decide whether this user turn should enter the scientific execution workflow.

Allowed interaction_type:
- DELEGATE: a scientific task or context-dependent follow-up that should go to the existing follow-up/task router.
- DIRECT_ANSWER: ordinary conversation or stable conceptual explanation that needs no tools.
- CAPABILITY_QUESTION: asks what this system can do.
- CLARIFY: cannot safely know what object/resource/task the user means.
- UNSUPPORTED: requires a live/external capability the system does not have.
- CANCEL_ACTIVE: only when the user explicitly asks to stop an active task.

Important rules:
1. Do NOT choose DELEGATE merely because database/file capabilities exist.
2. Questions such as "what is overfitting?" are DIRECT_ANSWER.
3. Questions such as "why did you say that just now?" with conversation context are DELEGATE.
4. If a direct answer would require current live external data, choose UNSUPPORTED.
5. For DIRECT_ANSWER/UNSUPPORTED/CLARIFY, provide a concise Chinese direct_answer.
6. Do not invent results from files, databases, tools, or previous tasks.

User query: {query}
Has previous analysis context: {has_context}
Has active task: {active_task}
Available files: {getattr(resources, "available_files", [])}
Authorized datasources: {getattr(resources, "authorized_datasources", [])}
Available scientific models: {getattr(resources, "available_scientific_models", [])}
Available MCP tools: {getattr(resources, "available_mcp_tools", [])}
""".strip()
        try:
            decision = await llm.with_structured_output(InteractionDecision).ainvoke(prompt)
            decision.source = "llm_structured_policy"
            return decision
        except Exception as exc:
            return InteractionDecision(
                interaction_type="DIRECT_ANSWER",
                reason=f"policy LLM failed: {type(exc).__name__}",
                direct_answer="这个问题不需要我调用科研工具。当前语义判断服务暂时不可用，请稍后重试或把问题描述得更具体。",
                source="safe_fallback",
                confidence=0.0,
            )


conversation_policy = ConversationPolicy()
