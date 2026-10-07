from __future__ import annotations

from langchain_core.callbacks import BaseCallbackHandler


class UsageCollector(BaseCallbackHandler):
    """Capture response metadata from actual LLM completions, never credentials."""

    def __init__(self) -> None:
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.total_tokens: int | None = None
        self.model: str | None = None

    def on_llm_end(self, response, **kwargs) -> None:
        try:
            message = response.generations[0][0].message
            usage = getattr(message, "usage_metadata", None) or {}
            metadata = getattr(message, "response_metadata", None) or {}
            self.input_tokens = usage.get("input_tokens")
            self.output_tokens = usage.get("output_tokens")
            self.total_tokens = usage.get("total_tokens")
            self.model = metadata.get("model_name") or metadata.get("model")
        except (IndexError, AttributeError, TypeError):
            return

    def snapshot(self) -> dict:
        return {
            "actual_model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }
