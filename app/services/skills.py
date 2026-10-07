from __future__ import annotations

from pathlib import Path
import re
import yaml

from app.config import SKILLS_ROOT


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

    def __init__(self, root: Path = SKILLS_ROOT):
        self.root = root

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
        return [name for _, name in sorted(scored, reverse=True)[:2]]

    def by_name(self) -> dict[str, dict]:
        return {skill["name"]: skill for skill in self.load()}

