"""Bounded, side-effect-aware decisions after tool observations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel
from app.tools.sql_guard import SQLGuardError


FailureKind = Literal[
    "invalid_arguments", "schema_mismatch", "timeout", "rate_limit",
    "permission_denied", "tool_unavailable", "empty_result", "non_retryable_error",
]
Action = Literal["retry", "alternative_tool", "replan", "hitl", "fail_safely"]


class RecoveryDecision(BaseModel):
    failure_kind: FailureKind
    action: Action
    reason: str
    max_attempts: int = 1


def classify_failure(error: Exception | str, *, tool: str) -> RecoveryDecision:
    text = str(error).lower()
    sqlstate = getattr(error, "sqlstate", None)
    if isinstance(error, SQLGuardError) and "table not authorized" in text and tool == "query_checker":
        return RecoveryDecision(failure_kind="schema_mismatch", action="replan", reason=str(error))
    if sqlstate in {"42501"} or any(token in text for token in ("permission denied", "not authorized", "forbidden")):
        return RecoveryDecision(failure_kind="permission_denied", action="fail_safely", reason=str(error))
    if sqlstate in {"42703", "42P01"} or any(token in text for token in ("column", "alias", "does not exist", "unknown table")):
        return RecoveryDecision(failure_kind="schema_mismatch", action="replan", reason=str(error))
    if sqlstate in {"57014"} or any(token in text for token in ("timed out", "timeout")):
        return RecoveryDecision(failure_kind="timeout", action="retry", reason=str(error), max_attempts=2)
    if sqlstate in {"53300"} or any(token in text for token in ("rate limit", "429", "too many requests")):
        return RecoveryDecision(failure_kind="rate_limit", action="retry", reason=str(error), max_attempts=2)
    if any(token in text for token in ("connection refused", "unavailable", "mcp server")):
        return RecoveryDecision(
            failure_kind="tool_unavailable",
            action="alternative_tool" if tool.startswith("mcp:") else "fail_safely",
            reason=str(error),
        )
    if any(token in text for token in ("missing argument", "invalid argument", "validation error")):
        return RecoveryDecision(failure_kind="invalid_arguments", action="fail_safely", reason=str(error))
    if "0 rows" in text or "empty result" in text:
        return RecoveryDecision(failure_kind="empty_result", action="fail_safely", reason=str(error))
    return RecoveryDecision(failure_kind="non_retryable_error", action="fail_safely", reason=f"{tool}: {error}")


async def bounded_transient_retry(
    operation: Callable[[], Awaitable[Any]], *, max_attempts: int = 2, base_delay: float = 0.1
) -> Any:
    """Retry only known transient failures; caller must use an idempotent operation."""
    if max_attempts < 1 or max_attempts > 3:
        raise ValueError("max_attempts must be in 1..3")
    for attempt in range(max_attempts):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            decision = classify_failure(exc, tool="idempotent_operation")
            if decision.action != "retry" or attempt + 1 >= max_attempts:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    raise AssertionError("unreachable")
