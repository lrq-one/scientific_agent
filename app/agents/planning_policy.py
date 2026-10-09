from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any

from pydantic import BaseModel, Field

from app.agents.planning_graph import canonical_plan, mandatory_stage_ids
from app.services.llm_config import llm_settings
from app.services.llm_telemetry import UsageCollector


class StageSelection(BaseModel):
    stage_ids: list[str] = Field(default_factory=list, max_length=8)
    reason: str = ""


class PlanningPolicy:
    """Validate free LLM plans. Legacy stage selection is compatibility-only."""

    @staticmethod
    def validate_plan(plan, allowed_tools: set[str], capabilities: set[str], tool_capabilities=None):
        if not plan or len(plan) > 16:
            raise ValueError("plan must contain 1..16 steps")
        ids = [step.step_id for step in plan]
        if len(set(ids)) != len(ids) or any(not item for item in ids):
            raise ValueError("plan step IDs must be nonempty and unique")
        by_id = {step.step_id: step for step in plan}
        visiting, visited = set(), set()
        def visit(step_id):
            if step_id in visiting:
                raise ValueError("cyclic plan dependencies")
            if step_id in visited:
                return
            visiting.add(step_id)
            step = by_id[step_id]
            step_tools = set(step.allowed_tools or step.selected_tools or step.preferred_tools)
            if step.allowed_tools and step.selected_tools and not set(step.selected_tools) <= set(step.allowed_tools):
                invalid = set(step.selected_tools) - set(step.allowed_tools)
                raise ValueError(f"invalid tool identifiers {sorted(invalid)}; exact allowed names={sorted(allowed_tools)}")
            if not step_tools <= allowed_tools:
                invalid = step_tools - allowed_tools
                raise ValueError(f"invalid tool identifiers {sorted(invalid)}; exact allowed names={sorted(allowed_tools)}")
            if not set(step.optional_tools) <= step_tools:
                raise ValueError(f"step {step_id}: optional_tools must be a subset of allowed_tools")
            required_inputs = step.required_inputs.keys() if isinstance(step.required_inputs, dict) else step.required_inputs
            if any(not item or not isinstance(item, str) for item in required_inputs):
                raise ValueError(f"step {step_id}: required_inputs must contain nonempty field names")
            if not {cap.value for cap in step.required_capabilities} <= capabilities:
                raise ValueError("plan references unavailable capabilities")
            if tool_capabilities is not None:
                required = {tool_capabilities[t] for t in step_tools}
                declared = {c.value for c in step.required_capabilities}
                if not required <= declared:
                    raise ValueError(f"step {step_id}: tool capability mismatch; required={sorted(required)}, declared={sorted(declared)}")
            condition = step.completion_predicate or step.completion_condition
            if condition and not set(condition.required_tools) <= step_tools:
                raise ValueError("completion condition references tools outside step scope")
            if condition and set(condition.required_tools) & set(step.optional_tools):
                raise ValueError(f"step {step_id}: completion predicate cannot require optional tools")
            for dependency in step.depends_on:
                if dependency not in by_id:
                    raise ValueError(f"unknown dependency {dependency!r}; exact step IDs={list(by_id)}")
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)
        for step_id in ids:
            visit(step_id)
        return plan

    def __init__(self, llm: Any | None = None):
        self.llm = llm

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

    @staticmethod
    def deterministic(intent: dict[str, Any]) -> list[str]:
        goal = str(intent.get("goal") or "").lower()
        stages = mandatory_stage_ids(intent)
        task_type = intent.get("task_type")
        if task_type in {"file_analysis", "mixed_analysis"} and any(
            token in goal for token in ("结构", "子群", "fused", "cyclic", "aromatic", "高误差", "subgroup")
        ):
            stages.add("subgroup-analysis")
        if task_type == "mixed_analysis" and any(
            token in goal for token in ("molecule_id", "关联", "核对", "一致", "对应", "join", "reconcile")
        ):
            stages.add("cross-resource-reconcile")
        if task_type == "mixed_analysis" and re.search(r"train[_-]?v\d+", goal):
            stages.add("cross-resource-reconcile")
        return sorted(stages)

    async def select_async(
        self,
        intent: dict[str, Any],
        selected_skills: list[str],
    ) -> tuple[list[str], str, dict[str, Any]]:
        catalog = canonical_plan(intent)
        available = {step["step_id"] for step in catalog}
        if len(catalog) <= 1:
            return [step["step_id"] for step in catalog], "single_stage", {"llm_called": False}

        llm = self._configured_llm()
        fallback = self.deterministic(intent)
        if llm is None:
            return fallback, "deterministic_fallback", {"llm_called": False, "fallback": True}

        compact = [
            {
                "stage_id": step["step_id"],
                "goal": step["goal"],
                "depends_on": step.get("depends_on", []),
                "required_capabilities": step.get("required_capabilities", []),
            }
            for step in catalog
        ]
        prompt = (
            "Choose the smallest set of executable plan stages needed for this scientific task. "
            "You may only use supplied stage_ids. Mandatory dependencies are added by the trusted runtime. "
            "Do not add a stage merely because it exists.\n"
            f"Intent: {json.dumps(intent, ensure_ascii=False)}\n"
            f"Selected skills: {selected_skills}\n"
            f"Stage catalog: {json.dumps(compact, ensure_ascii=False)}"
        )
        collector = UsageCollector()
        started = perf_counter()
        try:
            result = await llm.with_structured_output(StageSelection).ainvoke(
                prompt,
                config={"callbacks": [collector]},
            )
            selected = [stage for stage in result.stage_ids if stage in available]
            selected = list(dict.fromkeys(selected))
            selected.extend(stage for stage in mandatory_stage_ids(intent) if stage not in selected)
            return selected, "llm_bounded_stage_selection", {
                "llm_called": True,
                "fallback": False,
                "latency_ms": round((perf_counter() - started) * 1000, 2),
                "model_configured": llm_settings().model,
                **collector.snapshot(),
            }
        except Exception as exc:
            return fallback, "deterministic_fallback", {
                "llm_called": True,
                "fallback": True,
                "error_type": type(exc).__name__,
                "latency_ms": round((perf_counter() - started) * 1000, 2),
                **collector.snapshot(),
            }


planning_policy = PlanningPolicy()
