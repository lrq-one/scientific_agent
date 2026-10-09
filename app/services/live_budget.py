"""Process-local budget gate for explicitly authorized live Qwen runs.

The gate is inert unless ``SCIENTIFIC_AGENT_LIVE_LLM=1`` and
``SCIENTIFIC_AGENT_LIVE_RUN_DIR`` are set. A live run then fails closed on
request, token, cost or wall-clock limits. The ledger contains no API key or
prompt contents.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from threading import Lock
from time import monotonic, time
from typing import Any


class LiveBudgetExceeded(RuntimeError):
    """Raised before a provider call that would exceed the authorized budget."""


@dataclass
class LiveBudget:
    run_dir: Path
    max_requests: int = 250
    max_tokens: int = 1_500_000
    max_cost_usd: float = 30.0
    max_minutes: float = 120.0
    usd_to_cny: float = 7.0
    # Conservative upper-tier Qwen3.7-Flash rates (CNY / million tokens).
    input_cny_per_million: float = 1.44
    output_cny_per_million: float = 5.76
    started_at: float = field(default_factory=time)
    monotonic_started: float = field(default_factory=monotonic)
    requests: int = 0
    reserved_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)
    pending_reservations: list[int] = field(default_factory=list, repr=False)
    exceeded_reason: str | None = None
    lock: Lock = field(default_factory=Lock, repr=False)

    @property
    def path(self) -> Path:
        return self.run_dir / "budget_ledger.json"

    @classmethod
    def from_env(cls) -> "LiveBudget | None":
        if os.getenv("SCIENTIFIC_AGENT_LIVE_LLM") != "1":
            return None
        run_dir = os.getenv("SCIENTIFIC_AGENT_LIVE_RUN_DIR")
        if not run_dir:
            raise LiveBudgetExceeded("live model calls require SCIENTIFIC_AGENT_LIVE_RUN_DIR")
        budget = cls(
            Path(run_dir),
            max_requests=int(os.getenv("SCIENTIFIC_AGENT_MAX_REQUESTS", "250")),
            max_tokens=int(os.getenv("SCIENTIFIC_AGENT_MAX_TOKENS", "1500000")),
            max_cost_usd=float(os.getenv("SCIENTIFIC_AGENT_MAX_COST_USD", "30")),
            max_minutes=float(os.getenv("SCIENTIFIC_AGENT_MAX_MINUTES", "120")),
        )
        budget.run_dir.mkdir(parents=True, exist_ok=True)
        budget._persist("run_started", {"model": os.getenv("LLM_MODEL") or os.getenv("LLM_MODEL_NAME"),
                                         "api_base_host": (os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE") or "").split("/", 3)[2] if "/" in (os.getenv("LLM_API_BASE") or os.getenv("OPENAI_API_BASE") or "") else None})
        return budget

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        cny = (input_tokens / 1_000_000) * self.input_cny_per_million + (output_tokens / 1_000_000) * self.output_cny_per_million
        return cny / self.usd_to_cny

    def reserve(self, stage: str, estimated_tokens: int) -> None:
        with self.lock:
            elapsed = (monotonic() - self.monotonic_started) / 60
            if self.exceeded_reason:
                raise LiveBudgetExceeded(self.exceeded_reason)
            if elapsed >= self.max_minutes:
                raise LiveBudgetExceeded(f"live budget wall-clock limit reached at {elapsed:.2f} minutes")
            if self.requests >= self.max_requests:
                raise LiveBudgetExceeded("live budget request limit reached")
            estimate = max(1, int(estimated_tokens))
            if self.total_tokens + self.reserved_tokens + estimate > self.max_tokens:
                raise LiveBudgetExceeded("live budget token limit reached")
            estimated_cost = self._cost(estimate, 0)
            if self.estimated_cost_usd + estimated_cost > self.max_cost_usd:
                raise LiveBudgetExceeded("live budget cost limit reached")
            self.requests += 1
            self.reserved_tokens += estimate
            self.pending_reservations.append(estimate)
            self._persist("request_reserved", {"stage": stage, "estimated_tokens": estimate})

    def record(self, stage: str, usage: dict[str, Any] | None, *, latency_ms: float, error: str | None = None) -> None:
        usage = usage or {}
        input_tokens = usage.get("input_tokens") if isinstance(usage.get("input_tokens"), int) else None
        output_tokens = usage.get("output_tokens") if isinstance(usage.get("output_tokens"), int) else None
        total = usage.get("total_tokens") if isinstance(usage.get("total_tokens"), int) else None
        with self.lock:
            # Unknown provider usage is conservatively charged against the
            # reservation; no unmeasured request is treated as free.
            actual_total = total if total is not None else (self.pending_reservations[0] if self.pending_reservations else 0)
            reservation = self.pending_reservations.pop(0) if self.pending_reservations else actual_total
            self.reserved_tokens = max(0, self.reserved_tokens - reservation)
            self.input_tokens += input_tokens or 0; self.output_tokens += output_tokens or 0; self.total_tokens += actual_total
            charged_cost = self._cost(input_tokens, output_tokens) if input_tokens is not None and output_tokens is not None else self._cost(0, reservation)
            self.estimated_cost_usd += charged_cost
            if self.total_tokens > self.max_tokens:
                self.exceeded_reason = "live budget token limit reached after provider usage"
            elif self.estimated_cost_usd > self.max_cost_usd:
                self.exceeded_reason = "live budget cost limit reached after provider usage"
            self._persist("request_finished", {"stage": stage, "input_tokens": input_tokens,
                                                "output_tokens": output_tokens, "total_tokens": total,
                                                "latency_ms": round(latency_ms, 2), "error_type": error})
            if self.exceeded_reason:
                self._persist("budget_exceeded", {"stage": stage, "reason": self.exceeded_reason})

    def _persist(self, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "time": time(), **data}
        self.events.append(payload)
        snapshot = {"limits": {"max_requests": self.max_requests, "max_tokens": self.max_tokens,
                               "max_cost_usd": self.max_cost_usd, "max_minutes": self.max_minutes},
                    "usage": {"requests": self.requests, "input_tokens": self.input_tokens,
                              "output_tokens": self.output_tokens, "total_tokens": self.total_tokens,
                              "reserved_tokens": self.reserved_tokens,
                              "estimated_cost_usd": round(self.estimated_cost_usd, 8)},
                    "events": self.events[-500:]}
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_ACTIVE: LiveBudget | None = None


def active_budget() -> LiveBudget | None:
    global _ACTIVE
    if _ACTIVE is None and os.getenv("SCIENTIFIC_AGENT_LIVE_LLM") == "1":
        _ACTIVE = LiveBudget.from_env()
    return _ACTIVE


def reserve_live_call(stage: str, estimated_tokens: int) -> None:
    budget = active_budget()
    if budget is not None:
        budget.reserve(stage, estimated_tokens)


def record_live_call(stage: str, usage: dict[str, Any] | None, *, latency_ms: float, error: str | None = None) -> None:
    budget = active_budget()
    if budget is not None:
        budget.record(stage, usage, latency_ms=latency_ms, error=error)


def live_max_retries(default: int = 1) -> int:
    return 0 if os.getenv("SCIENTIFIC_AGENT_LIVE_LLM") == "1" else default
