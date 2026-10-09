from pathlib import Path

import pytest

from app.services.live_budget import LiveBudget, LiveBudgetExceeded


def test_unknown_provider_usage_consumes_reservation_and_stops_on_token_cap(tmp_path: Path):
    budget = LiveBudget(tmp_path, max_requests=3, max_tokens=10, max_cost_usd=30, max_minutes=10)
    budget.reserve("stage", 8)
    budget.record("stage", {}, latency_ms=1, error="TimeoutError")
    assert budget.total_tokens == 8
    with pytest.raises(LiveBudgetExceeded, match="token limit"):
        budget.reserve("next", 3)


def test_known_usage_is_persisted_without_prompt_or_secret(tmp_path: Path):
    budget = LiveBudget(tmp_path, max_requests=2, max_tokens=100, max_cost_usd=30, max_minutes=10)
    budget.reserve("stage", 20)
    budget.record("stage", {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}, latency_ms=2)
    payload = (tmp_path / "budget_ledger.json").read_text(encoding="utf-8")
    assert '"input_tokens": 7' in payload
    assert "prompt" not in payload.lower()
    assert "api_key" not in payload.lower()
