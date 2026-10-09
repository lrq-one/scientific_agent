"""Versioned, production-loaded prompt policy fragments.

Long node prompts remain close to their schemas for review.  This catalog adds
the stable policy and node contracts without creating a second planner or
changing runtime authorization.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.config import ROOT


PROMPT_ROOT = ROOT / "prompts"


class PromptCatalog:
    def __init__(self, root: Path = PROMPT_ROOT):
        self.root = root
        self.manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self._cache: dict[str, str] = {}

    def _read(self, relative: str) -> str:
        if relative not in self._cache:
            self._cache[relative] = (self.root / relative).read_text(encoding="utf-8").strip()
        return self._cache[relative]

    def fragment(self, node: str) -> str:
        relative = self.manifest["nodes"][node]
        return self._read(relative)

    def core(self) -> str:
        return self._read(self.manifest["core"])

    def compose(self, node: str, prompt: str) -> str:
        return f"{self.core()}\n\n{self.fragment(node)}\n\n{prompt}"

    def manifest_hash(self) -> str:
        payload: dict[str, Any] = {"manifest": self.manifest}
        for relative in [self.manifest["core"], *self.manifest["nodes"].values()]:
            payload[relative] = self._read(relative)
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


prompt_catalog = PromptCatalog()
