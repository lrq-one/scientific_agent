from __future__ import annotations

from pathlib import Path
import json
import re
import hashlib
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
        self._catalog_cache: list[dict] | None = None
        self._catalog_fingerprint: tuple[tuple[str, int], ...] | None = None

    def _catalog(self) -> list[dict]:
        """Cache immutable Skill metadata while still noticing local edits."""
        files = tuple((str(path), path.stat().st_mtime_ns) for path in sorted(self.root.glob("*/SKILL.md")))
        if self._catalog_cache is None or files != self._catalog_fingerprint:
            self._catalog_cache = self.load()
            self._catalog_fingerprint = files
        return self._catalog_cache

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
            body = match.group(2).strip()
            # Keep one registry: these fields are derived from the canonical
            # SKILL.md instead of maintained in a second catalog file.
            required = list(metadata.get("required_capabilities", []))
            inferred_inputs = (["csv", "xlsx"] if "file" in required else [])
            if "database" in required:
                inferred_inputs.append("authorized_schema")
            enriched = {
                **metadata,
                "id": path.parent.name,
                "version": str(metadata.get("version", "1.0.0")),
                "input_types": list(metadata.get("input_types", inferred_inputs)),
                "capabilities": list(metadata.get("capabilities", metadata.get("intents", []))),
                "required_resources": list(metadata.get("required_resources", required)),
                "negative_intents": list(metadata.get("negative_intents", [])),
                "tool_capabilities": list(metadata.get("tool_capabilities", metadata.get("allowed_tools", []))),
                "prerequisites": list(metadata.get("prerequisites", [])),
                "references": list(metadata.get("references", [])),
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "enabled": bool(metadata.get("enabled", True)),
                "deprecated": bool(metadata.get("deprecated", False)),
                "instructions": body,
                "path": str(path),
            }
            skills.append(enriched)
        return skills

    def select(self, goal: str, task_type: str) -> list[str]:
        """Deterministic fallback used only when semantic routing is unavailable."""
        goal_lower = goal.lower()
        scored = []
        for skill in self._catalog():
            if skill.get("name") == "scientific_result_summary":
                continue
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

    def catalog(self) -> list[dict[str, Any]]:
        """L0 metadata only; full instructions are intentionally excluded."""
        return [{key: value for key, value in skill.items() if key not in {"instructions", "path"}}
                for skill in self._catalog() if skill.get("enabled", True) and not skill.get("deprecated", False)]

    def detail(self, name: str, *, max_chars: int | None = None) -> dict[str, Any] | None:
        """L3 detail loading for an already selected skill."""
        skill = self.by_name().get(name)
        if not skill or not skill.get("enabled", True) or skill.get("deprecated", False):
            return None
        if max_chars is None:
            return dict(skill)
        return {**skill, "instructions": str(skill.get("instructions", ""))[:max_chars]}

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
            temperature=0,
            max_retries=live_max_retries(1),
            extra_body={"enable_thinking": False} if (settings.model or "").startswith("qwen") else None,
        )

    @staticmethod
    def _compact(skill: dict) -> dict[str, Any]:
        return {
            "id": skill.get("id", skill.get("name", "")),
            "name": skill["name"],
            "version": skill.get("version", "1.0.0"),
            "description": skill.get("description", ""),
            "domains": skill.get("domains", []),
            "intents": skill.get("intents", []),
            "required_capabilities": skill.get("required_capabilities", []),
            "optional_capabilities": skill.get("optional_capabilities", []),
            "tags": skill.get("tags", []),
            "input_types": skill.get("input_types", []),
            "capabilities": skill.get("capabilities", []),
            "required_resources": skill.get("required_resources", []),
            "negative_intents": skill.get("negative_intents", []),
            "enabled": skill.get("enabled", True),
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

        catalog = self._catalog()
        if task_type == "file_analysis" and re.search(r"多少行|几行|行数|列名|哪些列|row count|how many rows", goal, flags=re.I):
            return [], "direct_file_inspection", {"llm_called": False, "fallback": False}
        available = available_capabilities or set()
        candidates = []
        for skill in catalog:
            if skill.get("name") == "scientific_result_summary":
                continue
            required = {str(item) for item in skill.get("required_capabilities", [])}
            if available_capabilities is not None and not required.issubset(available):
                continue
            # The declared Skill input contract is an execution prerequisite,
            # not just an optional hint to the LLM. In particular, comparing
            # two model-run names is NOT comparing two dataset versions.
            if skill.get("requires_two_dataset_versions"):
                explicit_versions = set(re.findall(r"train[_-]?v\d+", goal, re.I))
                if len(explicit_versions) != 2 or not re.search(r"比较|对比|compare", goal, re.I):
                    continue
            candidates.append(skill)
        allowed = {item["name"] for item in candidates}
        if not candidates:
            return [], "no_authorized_skill", {"llm_called": False}

        llm = self._configured_llm()
        if llm is None:
            selected = [name for name in self.select(goal, task_type) if name in allowed]
            return selected, "deterministic_fallback", {"llm_called": False, "fallback": True}

        settings = llm_settings()
        collector = UsageCollector()
        prompt = (
            "Select the smallest set of reusable scientific workflow skills that together cover the task. "
            "Skills are workflow-level capabilities, not individual tools. Select at most 3. "
            "Use only names from the supplied catalog; do not invent names.\n"
            "For questions ABOUT system capabilities, greetings, or general conceptual explanations, return an empty selected_skills list: no scientific method is being executed.\n"
            f"Task type: {task_type}\n"
            f"Goal: {goal}\n"
            f"Available capabilities: {sorted(available)}\n"
            f"Skill catalog: {json.dumps([self._compact(item) for item in candidates], ensure_ascii=False)}"
        )
        from app.services.prompt_catalog import prompt_catalog
        prompt = prompt_catalog.compose("skill_router", prompt)
        started = perf_counter()
        from app.services.live_budget import record_live_call, reserve_live_call
        reserve_live_call("skill_routing", max(1, len(prompt) // 4 + 1200))
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
                "candidate_count": len(candidates),
                "catalog_chars": len(json.dumps([self._compact(item) for item in candidates], ensure_ascii=False)),
                "prompt_chars": len(prompt),
                "latency_ms": round((perf_counter() - started) * 1000, 2),
                "model_configured": settings.model,
                **collector.snapshot(),
            }
            record_live_call("skill_routing", collector.snapshot(), latency_ms=(perf_counter() - started) * 1000)
            # An empty semantic selection is valid (e.g. conversation or direct
            # inspection); do not replace it with a keyword-selected Skill.
            return selected[:3], "llm_compact_catalog", telemetry
        except Exception as exc:
            record_live_call("skill_routing", collector.snapshot(), latency_ms=(perf_counter() - started) * 1000, error=type(exc).__name__)

        selected = [name for name in self.select(goal, task_type) if name in allowed]
        return selected, "deterministic_fallback", {
            "llm_called": True,
            "fallback": True,
            "candidate_count": len(candidates),
            "prompt_chars": len(prompt),
            "latency_ms": round((perf_counter() - started) * 1000, 2),
            **collector.snapshot(),
        }

    def execution_context(self, selected_skills: list[str], max_chars_per_skill: int = 1200) -> str:
        """Return bounded executable Skill guidance for downstream planners/tools."""
        catalog = self.by_name()
        blocks: list[str] = []
        for name in selected_skills[:3]:
            if name == "scientific_result_summary":
                continue
            skill = catalog.get(name)
            if not skill:
                continue
            detail = self.detail(name, max_chars=max_chars_per_skill)
            if not detail:
                continue
            blocks.append(
                "\n".join(
                    [
                        f"Skill: {name}",
                        f"Description: {detail.get('description', '')}",
                        f"Allowed tools: {', '.join(detail.get('allowed_tools', []))}",
                        str(detail.get("instructions", "")),
                    ]
                )
            )
        return "\n\n".join(blocks)

    def by_name(self) -> dict[str, dict]:
        return {skill["name"]: skill for skill in self._catalog()}
