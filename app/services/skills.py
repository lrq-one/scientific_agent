from __future__ import annotations

from pathlib import Path
import json
import re
from time import perf_counter
from typing import Any

from pydantic import BaseModel, Field
import yaml

from app.config import SKILLS_ROOT
from app.services.llm_config import llm_settings
from app.services.llm_telemetry import UsageCollector


class SkillSelection(BaseModel):
    selected_skills: list[str] = Field(default_factory=list, max_length=4)
    reason: str = ""


class SkillService:
    REQUIRED_METADATA = {
        "name",
        "description",
        "domains",
        "intents",
        "required_capabilities",
        "optional_capabilities",
        "risk_level",
    }

    def __init__(self, root: Path = SKILLS_ROOT, llm: Any | None = None):
        self.root = root
        self.llm = llm

    def load(self) -> list[dict]:
        skills = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            text = path.read_text(encoding="utf-8")
            match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
            if not match:
                continue
            metadata = yaml.safe_load(match.group(1)) or {}
            missing = self.REQUIRED_METADATA - set(metadata)
            if missing:
                raise ValueError(f"{path}: missing skill metadata {sorted(missing)}")
            skills.append({**metadata, "instructions": match.group(2).strip(), "path": str(path)})
        return skills

    def select(self, goal: str, task_type: str) -> list[str]:
        """Deterministic fallback used only when semantic routing is unavailable."""
        goal_lower = goal.lower()
        scored = []
        for skill in self.load():
            if skill.get("requires_two_dataset_versions"):
                versions = set(re.findall(r"train[_-]?v\d+", goal_lower))
                if len(versions) != 2 or not any(word in goal_lower for word in ("比较", "对比", "compare")):
                    continue
            terms = [str(x).lower() for x in skill.get("tags", [])]
            score = sum(term in goal_lower for term in terms)
            intents = [str(x) for x in skill.get("intents", [])]
            if task_type in intents:
                score += 2
            if "general" in intents:
                score += 0.25
            if score:
                scored.append((score, skill["name"]))
        return [name for _, name in sorted(scored, reverse=True)[:3]]

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
    def _compact(skill: dict) -> dict[str, Any]:
        return {
            "name": skill["name"],
            "description": skill.get("description", ""),
            "domains": skill.get("domains", []),
            "intents": skill.get("intents", []),
            "required_capabilities": skill.get("required_capabilities", []),
            "optional_capabilities": skill.get("optional_capabilities", []),
            "tags": skill.get("tags", []),
            "risk_level": skill.get("risk_level", "low"),
        }

    async def select_async(
        self,
        goal: str,
        task_type: str,
        available_capabilities: set[str] | None = None,
    ) -> tuple[list[str], str, dict[str, Any]]:
        """Select workflow-level skills from compact metadata.

        The catalog is intentionally small enough to show all valid skills to the
        LLM. Retrieval is not inserted unless the catalog grows enough to justify
        it; Phase 4 v1 already showed that BM25 pre-filtering harmed recall at the
        current scale.
        """

        catalog = self.load()
        available = available_capabilities or set()
        candidates = []
        for skill in catalog:
            required = {str(item) for item in skill.get("required_capabilities", [])}
            if required and available and not required.issubset(available):
                continue
            candidates.append(skill)
        if not candidates:
            candidates = catalog

        llm = self._configured_llm()
        if llm is None:
            selected = self.select(goal, task_type)
            return selected, "deterministic_fallback", {"llm_called": False, "fallback": True}

        settings = llm_settings()
        collector = UsageCollector()
        prompt = (
            "Select the smallest set of reusable scientific workflow skills that together cover the task. "
            "Skills are workflow-level capabilities, not individual tools. Select at most 3. "
            "Use only names from the supplied catalog; do not invent names.\n"
            f"Task type: {task_type}\n"
            f"Goal: {goal}\n"
            f"Available capabilities: {sorted(available)}\n"
            f"Skill catalog: {json.dumps([self._compact(item) for item in candidates], ensure_ascii=False)}"
        )
        started = perf_counter()
        try:
            result = await llm.with_structured_output(SkillSelection).ainvoke(
                prompt,
                config={"callbacks": [collector]},
            )
            allowed = {item["name"] for item in candidates}
            selected = []
            for name in result.selected_skills:
                if name in allowed and name not in selected:
                    selected.append(name)
            telemetry = {
                "llm_called": True,
                "fallback": False,
                "latency_ms": round((perf_counter() - started) * 1000, 2),
                "model_configured": settings.model,
                **collector.snapshot(),
            }
            if selected:
                return selected[:3], "llm_compact_catalog", telemetry
        except Exception:
            pass

        selected = self.select(goal, task_type)
        return selected, "deterministic_fallback", {
            "llm_called": True,
            "fallback": True,
            "latency_ms": round((perf_counter() - started) * 1000, 2),
            **collector.snapshot(),
        }

    def by_name(self) -> dict[str, dict]:
        return {skill["name"]: skill for skill in self.load()}
