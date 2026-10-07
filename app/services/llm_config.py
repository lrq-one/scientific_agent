from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class LLMSettings:
    api_base: str | None
    api_key: str | None
    model: str | None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model)

    @property
    def source(self) -> dict[str, str | None]:
        return {
            "api_base": "LLM_API_BASE" if os.getenv("LLM_API_BASE") else "OPENAI_API_BASE" if os.getenv("OPENAI_API_BASE") else None,
            "api_key": "LLM_API_KEY" if os.getenv("LLM_API_KEY") else "OPENAI_API_KEY" if os.getenv("OPENAI_API_KEY") else None,
            "model": "LLM_MODEL" if os.getenv("LLM_MODEL") else "LLM_MODEL_NAME" if os.getenv("LLM_MODEL_NAME") else None,
        }


def llm_settings() -> LLMSettings:
    """Read preferred LLM_* names while preserving backwards compatibility."""
    return LLMSettings(
        api_base=os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE"),
        api_key=os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY"),
        model=os.getenv("LLM_MODEL") or os.getenv("LLM_MODEL_NAME"),
    )
