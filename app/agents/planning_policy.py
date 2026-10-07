from __future__ import annotations

import json
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
    """Select only from validated executable stages; never invent a new executor."""

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
