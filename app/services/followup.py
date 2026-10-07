from __future__ import annotations

import json
import re
from typing import Any

from app.models.schemas import ConversationContextSummary, FollowUpClassification, ProvenanceRecord, TaskRefinementPatch
from app.services.llm_config import llm_settings


REUSE_TYPES = {"EVIDENCE_EXPLANATION", "RESULT_EXPLANATION", "ERROR_QUESTION"}
CONTEXT_TYPES = REUSE_TYPES | {"CONTINUE_ANALYSIS", "REFINE_PREVIOUS_TASK", "RERUN_PREVIOUS_TASK"}


def conversation_context_summary(
    query: str, contexts: list[dict[str, Any]]
) -> ConversationContextSummary:
    recent = contexts[:3]
    previous = recent[0] if recent else {}
    task = previous.get("task") or {}
    user_message = next(
        (item.get("content", "") for item in previous.get("messages", []) if item.get("role") == "user"),
        "",
    )
    evidence = previous.get("evidence") or []
    return ConversationContextSummary(
        conversation_id=task.get("conversation_id"),
        current_user_query=query[:1000],
        previous_user_query=user_message[:1000] or None,
        previous_assistant_answer=str((previous.get("assistant_message") or {}).get("content") or "")[:1200] or None,
        previous_task_id=task.get("id"),
        previous_task_status=task.get("status"),
        previous_task_goal=(task.get("intent_json") or {}).get("goal") or user_message[:1000] or None,
        available_evidence_summary=[
            {
                "id": item.get("id"),
                "claim": str(item.get("claim") or "")[:160],
                "source": item.get("source"),
                "dataset_version": item.get("dataset_version"),
                "model_version": item.get("model_version"),
            }
            for item in evidence[:5]
        ],
        dataset_versions=sorted({item["dataset_version"] for item in evidence if item.get("dataset_version")}),
        model_versions=sorted({item["model_version"] for item in evidence if item.get("model_version")}),
        recent_tasks=[
            {
                "task_id": item["task"].get("id"),
                "status": item["task"].get("status"),
                "goal": str((item["task"].get("intent_json") or {}).get("goal") or next(
                    (message.get("content", "") for message in item.get("messages", []) if message.get("role") == "user"),
                    "",
                ))[:240],
                "evidence_count": len(item.get("evidence") or []),
            }
            for item in recent
        ],
    )


def parse_refinement_patch(query: str) -> TaskRefinementPatch:
    patch = TaskRefinementPatch()
    dataset = re.search(r"train[_-]?v\d+", query, flags=re.I)
    model = re.search(r"model[_-]?v\d+", query, flags=re.I)
    subgroup = re.search(r"(?:只看|仅看|限定为|筛选)(?:\s*|为)(fused[-_ ]?ring|[\w-]+结构)", query, flags=re.I)
    if dataset:
        patch.dataset_version = dataset.group(0).replace("-", "_")
        patch.changed_fields.append("dataset_version")
    if model:
        patch.model_version = model.group(0).replace("-", "_")
        patch.changed_fields.append("model_version")
    if subgroup:
        patch.subgroup = subgroup.group(1).replace("-", "_").replace(" ", "_")
        patch.changed_fields.append("subgroup")
    if re.search(r"只输出表格|不要画图|改成表格", query):
        patch.output_format = "table"
        patch.changed_fields.append("output_format")
    elif re.search(r"画图|生成图表|改成图", query):
        patch.output_format = "chart"
        patch.changed_fields.append("output_format")
    return patch


def render_persisted_table(provenance: ProvenanceRecord) -> str | None:
    rows = provenance.sql_raw_result
    if not provenance.evidence or not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        return None
    columns = sorted(
        {key for row in rows for key in row},
        key=lambda key: (0 if key in {"structure_type", "is_fused_ring"} else 1, key),
    )
    if not columns:
        return None
    lines = ["基于上一任务已持久化的查询结果（未重新执行 SQL）：", "",
             "| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows[:20]:
        lines.append("| " + " | ".join(str(row.get(column, "")).replace("|", "\\|") for column in columns) + " |")
    lines.append(f"\n来源 task：`{provenance.previous_task_id}`；dataset：`{provenance.dataset_version or '未记录'}`。")
    return "\n".join(lines)


def workflow_query(query: str, follow_up_type: str, context: dict[str, Any] | None) -> tuple[str, str | None]:
    """Restore the prior goal for refinement/rerun without changing user message history."""
    patch = parse_refinement_patch(query)
    requested_version = patch.dataset_version
    if follow_up_type not in {"REFINE_PREVIOUS_TASK", "RERUN_PREVIOUS_TASK"} or not context:
        return query, requested_version
    previous_query = next(
        (message.get("content", "") for message in context.get("messages", []) if message.get("role") == "user"),
        "",
    ).strip()
    if not previous_query:
        return query, requested_version
    if follow_up_type == "RERUN_PREVIOUS_TASK":
        return previous_query, requested_version or _version_in_text(previous_query)
    if requested_version:
        previous_query = re.sub(r"train[_-]?v\d+", requested_version, previous_query, flags=re.I)
    if patch.model_version:
        previous_query = re.sub(r"model[_-]?v\d+", patch.model_version, previous_query, flags=re.I)
    return f"{previous_query}\n用户修改要求：{query}", requested_version or _version_in_text(previous_query)


def _version_in_text(text: str) -> str | None:
    match = re.search(r"train[_-]?v\d+", text, flags=re.I)
    return match.group(0).replace("-", "_") if match else None


class ConversationContextResolver:
    """Classify a turn before the scientific intent/tool workflow is entered."""

    EVIDENCE_PATTERNS = (
        r"证据", r"依据", r"从哪(?:里)?来", r"怎么(?:得出|得到|算出)",
        r"原始(?:数据|返回|行)", r"查询(?:过程|逻辑)", r"数据源", r"数据来源",
        r"\bsql\b", r"\bevidence(?:\s+id)?\b", r"参数", r"how did you get", r"source",
    )
    RESULT_PATTERNS = (
        r"为什么(?:这么|这样)(?:判断|说|认为)", r"解释(?:一下)?(?:这个|这些|刚才)?(?:结果|结论|数字)",
        r"(?:这个|这些)?(?:结果|数字)(?:是什么意思|怎么解读|为什么)", r"(?:刚才|上一轮|前面|上一个)(?:的)?(?:结论|判断)",
        r"(?:这个|那个|上面|前面).{0,3}(?:结论|判断)", r"为什么(?:说|认为)",
        r"怎么(?:理解|解读)", r"靠谱吗", r"可信(?:吗|么)", r"可靠(?:吗|么)", r"why (?:this|that)",
    )
    RERUN_PATTERNS = (
        r"重新(?:运行|执行|查询|验证|分析)", r"重跑", r"再跑(?:一遍|一次)?", r"再执行一次",
        r"重做一遍", r"rerun", r"run again",
    )
    REFINE_PATTERNS = (
        r"换成", r"改成", r"改为", r"只看", r"限定为", r"筛选", r"调整为",
        r"只输出表格", r"不要画图", r"改成表格",
        r"用\s*train[_-]?v\d+", r"instead", r"change to",
    )
    CONTINUE_PATTERNS = (r"继续(?:分析|做|查)", r"接着", r"下一步", r"再深入", r"continue")
    ERROR_PATTERNS = (
        r"报错", r"错误", r"为什么.*失败", r"失败(?:在|信息|原因)", r"哪里错",
        r"(?:执行|系统|接口|查询)异常", r"异常(?:信息|原因|报错|是怎么)", r"error", r"failed",
    )
    CONTEXT_MARKERS = ("这个", "这些", "刚才", "上次", "上一", "前面", "它", "该结果", "that", "previous")

    def __init__(self, llm: Any | None = None):
        self.llm = llm

    @staticmethod
    def _matches(text: str, patterns: tuple[str, ...]) -> bool:
        return any(re.search(pattern, text, flags=re.I) for pattern in patterns)

    def deterministic(self, query: str, context: dict[str, Any] | None) -> FollowUpClassification | None:
        text = " ".join(query.strip().split()).lower()
        has_reference = any(marker in text for marker in self.CONTEXT_MARKERS) or "之前" in text
        rerun_negated = bool(re.search(r"(?:不要|无需|禁止|不必).{0,8}(?:重跑|重新|再跑|再执行)", text))
        if self._matches(text, self.RERUN_PATTERNS) and not rerun_negated:
            return FollowUpClassification(follow_up_type="RERUN_PREVIOUS_TASK", reason="explicit rerun wording")
        if self._matches(text, self.REFINE_PATTERNS):
            return FollowUpClassification(follow_up_type="REFINE_PREVIOUS_TASK", reason="explicit refinement wording")
        if re.match(r"^(?:请)?(?:统计|比较|分析|查询|检查|计算)", text) and not has_reference:
            return FollowUpClassification(follow_up_type="NEW_TASK", reason="self-contained new analysis goal")
        if self._matches(text, self.CONTINUE_PATTERNS):
            return FollowUpClassification(follow_up_type="CONTINUE_ANALYSIS", reason="explicit continuation wording")
        if self._matches(text, self.ERROR_PATTERNS):
            return FollowUpClassification(follow_up_type="ERROR_QUESTION", reason="explicit error question")
        if self._matches(text, self.EVIDENCE_PATTERNS):
            return FollowUpClassification(follow_up_type="EVIDENCE_EXPLANATION", reason="explicit evidence/provenance question")
        if self._matches(text, self.RESULT_PATTERNS):
            return FollowUpClassification(follow_up_type="RESULT_EXPLANATION", reason="explicit result explanation question")
        if not context or not has_reference:
            return FollowUpClassification(follow_up_type="NEW_TASK", reason="no follow-up reference detected")
        return None

    @staticmethod
    def _attach_target(
        decision: FollowUpClassification,
        query: str,
        contexts: list[dict[str, Any]],
    ) -> FollowUpClassification:
        if decision.follow_up_type not in CONTEXT_TYPES:
            decision.target_task_id = None
            return decision
        if not contexts:
            decision.clarification_question = "当前会话没有可引用的上一项分析任务。请说明要追问的任务，或直接提出新的分析问题。"
            return decision
        task_ids = [item["task"]["id"] for item in contexts]
        mentioned_ids = re.findall(r"\b[0-9a-f]{8}-[0-9a-f-]{27,36}\b", query, flags=re.I)
        if mentioned_ids:
            if mentioned_ids[0] not in task_ids:
                decision.clarification_question = "未在当前会话最近的任务中找到指定 task_id；请确认要引用哪项任务。"
                return decision
            decision.target_task_id = mentioned_ids[0]
            return decision
        if re.search(r"上上轮|倒数第二|前一个任务", query):
            if len(contexts) < 2:
                decision.clarification_question = "当前会话没有可确认的上上轮分析任务；请提供 task_id。"
                return decision
            decision.target_task_id = task_ids[1]
            return decision
        if len(contexts) > 1 and re.search(r"之前那个|前面那个|某个任务|那次分析", query):
            decision.clarification_question = "当前会话有多项历史分析；请说明要引用哪一项，或提供 task_id。"
            return decision
        decision.target_task_id = task_ids[0]
        return decision

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
            max_tokens=256,
            extra_body={"enable_thinking": False},
        )

    async def resolve_async(
        self,
        query: str,
        context: dict[str, Any] | None,
        recent_contexts: list[dict[str, Any]] | None = None,
    ) -> FollowUpClassification:
        contexts = recent_contexts if recent_contexts is not None else ([context] if context else [])
        decision = self.deterministic(query, context)
        if decision is not None:
            return self._attach_target(decision, query, contexts)
        llm = self._configured_llm()
        if llm is None:
            return self._attach_target(FollowUpClassification(
                follow_up_type="RESULT_EXPLANATION",
                reason="context reference present; safe no-tool interpretation",
                source="safe_default",
            ), query, contexts)
        summary = conversation_context_summary(query, contexts)
        prompt = (
            "Classify the current turn relative to the previous completed scientific task. "
            "Choose exactly one follow_up_type. Explanation/evidence/error questions reuse persisted history; "
            "refine/rerun/continue may enter the workflow; unrelated work is NEW_TASK.\n"
            f"Bounded conversation context: {summary.model_dump_json(exclude_none=True)}"
        )
        try:
            result = await llm.with_structured_output(FollowUpClassification).ainvoke(prompt)
            result.source = "llm_structured"
            return self._attach_target(result, query, contexts)
        except Exception:
            return self._attach_target(FollowUpClassification(
                follow_up_type="RESULT_EXPLANATION",
                reason="ambiguous contextual turn; safe no-tool fallback",
                source="safe_default",
            ), query, contexts)


def build_provenance(context: dict[str, Any] | None) -> ProvenanceRecord:
    if not context:
        return ProvenanceRecord(uncertainties=["当前会话没有可复用的已完成分析任务。"])
    task = context.get("task", {})
    events = context.get("events", [])
    evidence = context.get("evidence", [])
    tools: list[dict[str, Any]] = []
    sql_candidate = None
    sql_params: dict[str, Any] = {}
    raw_result: Any = None
    datasource = None
    recorded_errors: list[str] = []
    dataset_version = next((item.get("dataset_version") for item in evidence if item.get("dataset_version")), None)
    model_version = next((item.get("model_version") for item in evidence if item.get("model_version")), None)
    for item in events:
        if item.get("event_type") == "CANCELLED":
            recorded_errors.append("上一任务已由用户取消；中间结果不能作为已完成科研结论。")
        if item.get("event_type") == "ERROR":
            payload = item.get("payload_json") or {}
            detail = payload.get("error") or payload.get("message")
            if detail:
                recorded_errors.append(f"上一任务错误：{detail}")
        if item.get("event_type") != "TOOL_FINISHED":
            continue
        payload = item.get("payload_json") or {}
        tool = payload.get("tool")
        result = payload.get("result") or {}
        tools.append({
            "tool": tool,
            "success": result.get("success"),
            "source": result.get("source"),
        })
        if result.get("success") is False and result.get("error"):
            recorded_errors.append(f"工具 {tool} 失败：{result['error']}")
        if tool == "text_to_sql" and isinstance(result.get("data"), dict):
            sql_candidate = result["data"]
        if tool == "execute_readonly_sql":
            datasource = result.get("source") or datasource
            raw_result = result.get("data")
            metadata = result.get("metadata") or {}
            sql_params = metadata.get("params") or {}
            if sql_candidate is None and metadata.get("sql"):
                sql_candidate = {"sql": metadata["sql"], "params": sql_params}
            dataset_version = dataset_version or sql_params.get("dataset_version")
    assistant = context.get("assistant_message") or {}
    uncertainties = recorded_errors
    if not evidence:
        uncertainties.append("上一任务没有持久化 Evidence。")
    if raw_result == []:
        uncertainties.append("上一查询返回 0 rows；这不能证明科学对象不存在。")
    if sql_candidate is None and any(item.get("source_type") == "database" for item in evidence):
        uncertainties.append("数据库 Evidence 存在，但未找到持久化 SQLCandidate。")
    datasource = datasource or next((item.get("source") for item in evidence if item.get("source")), None)
    return ProvenanceRecord(
        previous_task_id=task.get("id"),
        conversation_id=task.get("conversation_id"),
        thread_id=task.get("thread_id"),
        datasource=datasource,
        dataset_version=dataset_version,
        model_version=model_version,
        selected_skills=list(task.get("selected_skills_json") or []),
        tool_calls=tools,
        sql_candidate=sql_candidate,
        sql_params=sql_params,
        sql_raw_result=raw_result,
        evidence=evidence,
        claims=context.get("claims", []),
        artifacts=context.get("artifacts", []),
        final_answer=assistant.get("content"),
        uncertainties=uncertainties,
    )


def provenance_answer(provenance: ProvenanceRecord) -> str:
    if not provenance.evidence or provenance.sql_raw_result == [] or any("已由用户取消" in item for item in provenance.uncertainties):
        missing = provenance.uncertainties or ["没有可复用的持久化 Evidence。"]
        return "\n".join([
            "INSUFFICIENT_EVIDENCE",
            "",
            "无法基于上一轮记录给出确定性证据说明：",
            *[f"- {item}" for item in missing],
            "- 未经用户明确要求，我没有重新执行 SQL 或其他工具。",
        ])
    lines = [
        "## 上一轮结论的证据来源",
        "",
        f"- Previous task：`{provenance.previous_task_id}`",
        f"- 数据源：`{provenance.datasource or '未记录'}`",
        f"- Dataset/version：`{provenance.dataset_version or '未记录'}`",
        f"- Selected skills：`{', '.join(provenance.selected_skills) or '未记录'}`",
        "- 本轮新增工具调用：`0`",
        "",
        "### 已持久化的工具路径",
    ]
    for item in provenance.tool_calls:
        lines.append(f"- `{item.get('tool')}`：success={item.get('success')}，source={item.get('source')} ")
    if provenance.sql_candidate:
        lines += [
            "",
            "### SQL / 查询逻辑",
            "```sql",
            str(provenance.sql_candidate.get("sql", "")),
            "```",
            f"参数：`{json.dumps(provenance.sql_params, ensure_ascii=False, default=str)}`",
        ]
    rows = provenance.sql_raw_result
    if rows is not None:
        display = rows[:20] if isinstance(rows, list) else rows
        lines += [
            "",
            "### 实际返回的关键数据",
            "```json",
            json.dumps(display, ensure_ascii=False, indent=2, default=str),
            "```",
        ]
        if isinstance(rows, list) and len(rows) > 20:
            lines.append(f"仅展示前 20 行；持久化结果共 {len(rows)} 行。")
    if provenance.final_answer:
        lines += ["", "### 上一轮最终回答", provenance.final_answer]
    lines += ["", "### Evidence 与结论映射"]
    if provenance.claims:
        for claim in provenance.claims:
            linked = claim.get("evidence_ids_json") or []
            lines.append(
                f"- Claim `{claim.get('id')}`（{claim.get('status')}）：{claim.get('claim_text')}；"
                f"Evidence IDs：{', '.join(str(item) for item in linked) or '无'}。"
            )
    else:
        lines.append("- 历史任务没有显式 Claim → Evidence 关联；以下仅列原始 Evidence，不推断其支持全部结论。")
    for item in provenance.evidence:
        lines.append(
            f"- Evidence `{item.get('id')}`：{item.get('claim')} = "
            f"`{json.dumps(item.get('value_json'), ensure_ascii=False, default=str)}`；"
            f"来源 `{item.get('source')}`，tool call `{item.get('tool_call_id')}`。"
        )
    lines += ["", "### 不确定性"]
    lines.extend(f"- {item}" for item in provenance.uncertainties)
    if not provenance.uncertainties:
        lines.append("- 这里只解释已持久化的执行记录，不把相关性扩大解释为因果关系。")
    return "\n".join(lines)
