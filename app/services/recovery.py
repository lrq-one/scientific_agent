"""Bounded, side-effect-aware decisions after tool observations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel
from app.models.schemas import RecoveryPolicy
from app.tools.sql_guard import SQLGuardError


FailureKind = Literal[
    "invalid_arguments", "schema_mismatch", "timeout", "rate_limit",
    "permission_denied", "tool_unavailable", "empty_result", "non_retryable_error", "resource_not_found", "artifact_failure",
]
Action = Literal["retry", "alternative_tool", "replan", "hitl", "fail_safely"]


class RecoveryDecision(BaseModel):
    failure_kind: FailureKind
    action: Action
    reason: str
    max_attempts: int = 1


def recovery_policy_for(
    failure_code: str | None,
    *,
    failed_stage: str = "tool_execution",
    recoverable: bool | None = None,
    context: dict[str, Any] | None = None,
    previous_attempt: dict[str, Any] | None = None,
) -> RecoveryPolicy:
    """Map every runtime failure to one bounded, auditable action.

    This is deliberately deterministic. It complements (rather than replaces)
    the existing ``classify_failure`` API used by the legacy executor.
    """
    code = str(failure_code or "EXECUTION_FAILED").upper()
    mapping: dict[str, tuple[str, int, bool]] = {
        "INVALID_ARGUMENT": ("repair_arguments", 1, True),
        "INVALID_ARGUMENTS": ("repair_arguments", 1, True),
        "MISSING_INPUT": ("repair_arguments", 1, True),
        "SCHEMA_MISMATCH": ("retrieve_schema", 1, True),
        "SCHEMA_NOT_FOUND": ("retrieve_schema", 1, True),
        "SQL_CANDIDATE_LINEAGE": ("establish_sql_candidate", 1, True),
        "NO_TRUSTED_SQL_CANDIDATE": ("establish_sql_candidate", 1, True),
        "UNVERIFIED_SCOPE": ("targeted_sql_repair", 1, True),
        "SCOPE_VIOLATION": ("safe_reject", 0, False),
        "TIMEOUT": ("bounded_retry", 1, True),
        "RATE_LIMIT": ("bounded_retry", 1, True),
        "TRANSIENT_IO": ("bounded_retry", 1, True),
        "TOOL_UNAVAILABLE": ("bounded_retry", 1, True),
        "PLAN_DEPENDENCY": ("repair_plan_dependency", 1, True),
        "PLAN_REJECTED": ("repair_plan_dependency", 1, True),
        "NO_PROGRESS_REPLAN": ("no_progress_stop", 0, False),
        "DATABASE_STORAGE_CORRUPTION": ("stop", 0, False),
        "USER_INPUT_REQUIRED": ("ask_user", 0, True),
        "RESOURCE_NOT_FOUND": ("ask_user", 0, True),
        "ARTIFACT_FAILURE": ("safe_reject", 0, False),
    }
    action, budget, default_recoverable = mapping.get(code, ("safe_reject", 0, False))
    effective_recoverable = default_recoverable if recoverable is None else bool(recoverable)
    if action in {"safe_reject", "stop", "no_progress_stop"}:
        effective_recoverable = False
    # A policy may only carry a retry budget when the failure is explicitly
    # recoverable; this prevents an upstream boolean from enabling unsafe
    # retries for scope violations or storage corruption.
    if not effective_recoverable:
        budget = 0
    return RecoveryPolicy(
        failure_code=code,
        failed_stage=failed_stage,
        recoverable=effective_recoverable,
        recovery_action=action,
        retry_budget=budget,
        context=dict(context or {}),
        previous_attempt=dict(previous_attempt or {}),
        strategy_changed=action not in {"bounded_retry", "safe_reject", "stop", "no_progress_stop"},
    )


def classify_failure(error: Exception | str, *, tool: str) -> RecoveryDecision:
    text = str(error).lower()
    if isinstance(error, FileNotFoundError) or "resource not found" in text:
        return RecoveryDecision(failure_kind="resource_not_found", action="hitl", reason="requested resource is missing")
    if tool in {"save_chart", "save_result_table", "plot_metric_comparison"}:
        return RecoveryDecision(failure_kind="artifact_failure", action="fail_safely", reason="artifact generation failed; retain verified evidence")
    sqlstate = getattr(error, "sqlstate", None)
    if isinstance(error, SQLGuardError) and "table not authorized" in text and tool == "query_checker":
        return RecoveryDecision(failure_kind="schema_mismatch", action="replan", reason=str(error))
    if any(token in text for token in ("missing columns", "unknown group column", "join key not found")):
        return RecoveryDecision(
            failure_kind="schema_mismatch",
            action="alternative_tool",
            reason=str(error),
        )
    if sqlstate in {"42501"} or any(token in text for token in ("permission denied", "not authorized", "forbidden")):
        return RecoveryDecision(failure_kind="permission_denied", action="fail_safely", reason=str(error))
    if sqlstate in {"42703", "42P01"} or any(token in text for token in ("column", "alias", "does not exist", "unknown table", "join relationship")):
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
