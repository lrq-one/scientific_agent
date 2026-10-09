"""Small, shared protocol for SQLCandidate provenance.

Failed Text-to-SQL results intentionally keep their generated SQL in
``ToolResult.metadata`` so recovery can explain and repair it.  That SQL is
diagnostic data, not an authorised candidate for Query Checker or execution.
This module keeps the distinction in one place for both the decision boundary
and the runtime argument validator.
"""

from __future__ import annotations

import re
from typing import Any


DIAGNOSTIC_ONLY = "diagnostic_only"


def normalize_sql(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().rstrip(";")


def _successful_candidate(result) -> bool:
    if not result.success or result.metadata.get("sql_candidate_status") == DIAGNOSTIC_ONLY:
        return False
    scope_validation = result.metadata.get("scope_validation") or {}
    return scope_validation.get("verified") is not False


def _previous_scope_matches(state, previous: dict[str, Any], params: dict[str, Any]) -> bool:
    provenance = state.conversation_context.get("previous_provenance") or {}
    scope_version = getattr(state.query_scope, "dataset_version", None)
    requested_version = scope_version or state.dataset_version
    if not requested_version:
        return True
    if provenance.get("dataset_version") == requested_version:
        return True
    sql = str(previous.get("sql") or "")
    return "%(dataset_version)s" in sql and params.get("dataset_version") == requested_version


def trusted_sql_candidates(state, *, population_id: str | None = None,
                           params: dict[str, Any] | None = None) -> list[str]:
    """Return only SQL strings that may be checked/executed.

    A successful Text2SQL observation is trusted as the generated candidate
    (scope validation happens in that tool).  A failed observation carrying a
    diagnostic ``sql_candidate`` is deliberately excluded.
    """
    expected_params = params or {}
    candidates: list[str] = []
    for call, result in zip(state.tool_calls, state.observations):
        if call.get("tool") != "text_to_sql" or not _successful_candidate(result):
            continue
        if result.metadata.get("sql_candidate_status") == DIAGNOSTIC_ONLY:
            continue
        data = result.data if isinstance(result.data, dict) else {}
        sql = data.get("sql")
        if not sql:
            continue
        if state.populations and result.metadata.get("population_id") != population_id:
            continue
        if (data.get("params") or {}) != expected_params:
            continue
        candidates.append(str(sql))

    provenance = state.conversation_context.get("previous_provenance") or {}
    previous = provenance.get("sql_candidate") or {}
    if previous.get("candidate_status") == DIAGNOSTIC_ONLY or previous.get("status") == DIAGNOSTIC_ONLY:
        previous = {}
    if previous.get("sql") and _previous_scope_matches(state, previous, expected_params):
        candidates.append(str(previous["sql"]))
    return candidates


def user_supplied_sql(state, sql: str) -> bool:
    text = normalize_sql(state.user_request or state.goal)
    value = normalize_sql(sql)
    return bool(value and value in text)


def has_trusted_sql_source(state, *, population_id: str | None = None) -> bool:
    """Whether Query Checker can be exposed before a new decision is made."""
    for call, result in zip(state.tool_calls, state.observations):
        if call.get("tool") != "text_to_sql" or not _successful_candidate(result):
            continue
        if result.metadata.get("sql_candidate_status") == DIAGNOSTIC_ONLY:
            continue
        data = result.data if isinstance(result.data, dict) else {}
        if not data.get("sql"):
            continue
        if state.populations and result.metadata.get("population_id") != population_id:
            continue
        return True

    provenance = state.conversation_context.get("previous_provenance") or {}
    previous = provenance.get("sql_candidate") or {}
    if (previous.get("sql") and previous.get("candidate_status") != DIAGNOSTIC_ONLY
            and previous.get("status") != DIAGNOSTIC_ONLY
            and _previous_scope_matches(state, previous, previous.get("params") or provenance.get("sql_params") or {})):
        return True

    text = normalize_sql(state.user_request or state.goal)
    # A bare mention of the word SQL is not a user-supplied statement.
    return bool(re.search(r"\b(?:SELECT|WITH)\b.+\bFROM\b", text, re.IGNORECASE))
