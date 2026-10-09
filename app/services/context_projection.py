"""Bounded, node-specific views of ScientificAgentState.

Projection is a presentation optimization only: the persisted state, tool
results, evidence and audit events are never mutated or discarded. Every view
retains the user contract, resource binding, population/query scope and SQL
lineage needed to make a safe decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CRITICAL_FACT_KEYS = (
    "goal_contract", "resource_binding", "query_scope", "population_requirements",
    "sql_candidate_lineage", "evidence_ids", "current_step", "allowed_actions",
    "remaining_tool_budget", "remaining_replan_budget",
)


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _bound(value: Any, *, rows: int = 8, chars: int = 1200, depth: int = 3) -> Any:
    if depth <= 0:
        return "[compacted]"
    if isinstance(value, str):
        return value[:chars]
    if isinstance(value, list):
        return [_bound(item, rows=rows, chars=chars, depth=depth - 1) for item in value[:rows]]
    if isinstance(value, dict):
        return {str(key): _bound(item, rows=rows, chars=chars, depth=depth - 1) for key, item in list(value.items())[:40]}
    return value


@dataclass(frozen=True)
class Projection:
    payload: dict[str, Any]
    ledger: dict[str, Any]


def _ledger(payload: dict[str, Any], *, projection: str) -> dict[str, Any]:
    chars = len(str(payload))
    return {"projection": projection, "field_count": len(payload), "approx_chars": chars,
            "approx_tokens": max(1, (chars + 3) // 4), "compacted": True,
            "critical_facts_valid": bool(payload.get("critical_facts_valid", False)),
            "critical_fact_fields": list(CRITICAL_FACT_KEYS)}


def _unique_recent(items: list[Any], key) -> list[Any]:
    seen = set()
    output = []
    for item in reversed(items):
        marker = key(item)
        if marker in seen:
            continue
        seen.add(marker)
        output.append(item)
    return list(reversed(output))


def _sql_lineage(state: Any) -> list[dict[str, Any]]:
    lineage = []
    for call, result in zip(state.tool_calls, state.observations):
        metadata = getattr(result, "metadata", {}) or {}
        candidate = metadata.get("sql_candidate")
        if candidate:
            lineage.append({"tool_call_id": call.get("tool_call_id"),
                            "status": metadata.get("sql_candidate_status", "diagnostic_only"),
                            "candidate": _bound(candidate, chars=1800),
                            "scope_validation": _bound(metadata.get("scope_validation", {}), chars=1400)})
        elif isinstance(getattr(result, "data", None), dict) and result.data.get("sql"):
            lineage.append({"tool_call_id": call.get("tool_call_id"),
                            "status": metadata.get("sql_candidate_status", "unknown"),
                            "candidate": _bound({"sql": result.data.get("sql"), "params": result.data.get("params", {})}, chars=1800)})
    previous = (state.conversation_context.get("previous_provenance") or {}).get("sql_candidate")
    if previous:
        lineage.append({"tool_call_id": "previous_provenance", "status": previous.get("candidate_status", previous.get("status", "unknown")),
                        "candidate": _bound(previous, chars=1800)})
    return _unique_recent(lineage, lambda item: (item.get("tool_call_id"), item.get("status"), str(item.get("candidate"))))


def validate_critical_facts(payload: dict[str, Any]) -> bool:
    """Validate the explicit projection contract before an LLM call.

    Empty values are valid for optional facts, but the keys themselves must be
    present so compaction cannot silently drop an authorization boundary.
    """
    required = {"goal_contract", "resource_binding", "query_scope", "current_step",
                "allowed_actions", "remaining_tool_budget", "remaining_replan_budget"}
    return required <= set(payload) and "projection_contract" in payload


def decision_context(state: Any, *, tools: list[dict[str, Any]], skill_context: str,
                    callable_tools: list[str], pending_scope: list[Any], actions: list[str]) -> Projection:
    """Return the minimal safe decision view while preserving audit references."""
    observations = []
    from app.config import MAX_REPLANS, MAX_TOOL_CALLS
    recent = _unique_recent(list(zip(state.tool_calls, state.observations)),
                            lambda pair: pair[0].get("tool_call_id", str(pair[0])))
    for call, result in recent[-4:]:
        result_data = _dump(result)
        # Keep identifiers and validation metadata intact; bound bulky rows.
        if isinstance(result_data, dict):
            result_data = {key: _bound(value, rows=4, chars=900) for key, value in result_data.items()}
        observations.append({"tool_call": _bound(call, chars=900), "result": result_data})
    evidence_items = _unique_recent(state.evidence, lambda item: getattr(item, "evidence_id", str(item)))
    evidence = [_bound(_dump(item), rows=4, chars=900) for item in evidence_items[-6:]]
    plan_scopes = []
    for step in state.plan:
        predicate = getattr(step, "completion_predicate", None) or getattr(step, "completion_condition", None)
        plan_scopes.append({"step_id": step.step_id, "status": step.status,
                            "allowed_tools": step.allowed_tools or step.selected_tools or step.preferred_tools,
                            "optional_tools": step.optional_tools, "required_inputs": step.required_inputs,
                            "completion_predicate": _dump(predicate) if predicate else None,
                            "depends_on": step.depends_on})
    from app.services.prompt_contract import PROMPT_VERSIONS
    from app.services.user_profile import task_profile_context
    profile = None
    raw_profile = state.conversation_context.get("user_profile") if isinstance(state.conversation_context, dict) else None
    if isinstance(raw_profile, dict) and raw_profile.get("user_id") == state.user_id:
        try:
            from app.models.schemas import UserProfile
            profile = task_profile_context(UserProfile.model_validate(raw_profile))
        except Exception:
            profile = None
    payload = {
        "prompt_contract_version": PROMPT_VERSIONS["planning_decision"],
        "user_question": state.user_request, "goal": state.goal,
        "original_goal_requirements": {"requested_dimensions": state.requested_dimensions,
                                       "required_deliverables": state.required_deliverables},
        "goal_contract": _dump(state.goal_contract) if state.goal_contract else None,
        "conversation": _bound(state.conversation_context, rows=4),
        "user_profile": profile,
        "resources": _dump(state.resource_summary), "resource_grounding": _bound(state.resource_hint),
        "selected_skills": state.selected_skills, "skill_instructions": skill_context[:3600],
        "plan": [_bound(_dump(step), chars=900) for step in state.plan], "current_step": state.current_step,
        "allowed_tool_schemas": tools, "currently_callable_tools": callable_tools,
        "recorded_schema_tables": sorted(state.schema_cache),
        "currently_callable_step_ids": [step.step_id for step in state.plan if step.status in {"pending", "running", "blocked", "failed"}],
        "recorded_row_tables": [], "plan_step_tool_scopes": plan_scopes,
        "observations": observations, "evidence": evidence,
        "errors": state.errors[-3:], "uncertainties": state.uncertainties[-4:],
        "hitl_answers": state.hitl_answers[-2:], "dataset_version": state.dataset_version,
        "resource_binding": _dump(state.resource_binding), "query_scope": _dump(state.query_scope),
        "population_requirements": [_dump(item) for item in state.populations],
        "requires_population_binding": state.requires_population_binding,
        "pending_executed_scope": [_dump(item) for item in pending_scope], "allowed_actions": actions,
        "remaining_tool_budget": max(0, MAX_TOOL_CALLS - state.tool_call_count), "replan_count": state.replan_count,
        "remaining_replan_budget": max(0, MAX_REPLANS - state.replan_count),
        "no_progress_replan_count": state.no_progress_replan_count,
        "last_decision": _dump(state.decision) if state.decision else None,
        "control_observations": state.control_observations[-3:],
        "sql_candidate_lineage": _sql_lineage(state),
        "evidence_ids": [item.evidence_id for item in evidence_items],
        "critical_facts_valid": True,
        "projection_contract": {"preserved": ["user_goal", "goal_contract", "resource_binding", "query_scope", "populations", "sql_candidate_lineage", "evidence_ids"],
                                "source_state_unchanged": True},
    }
    payload["critical_facts_valid"] = validate_critical_facts(payload)
    return Projection(payload, _ledger(payload, projection="decision"))


def text2sql_context(*, goal: str, query_scope: Any, resource_binding: Any, schema: dict[str, Any],
                     relationships: list[dict[str, Any]], repair_feedback: str | None = None) -> Projection:
    payload = {"goal": goal, "query_scope": _dump(query_scope), "resource_binding": _dump(resource_binding),
               "schema": _bound(schema, rows=20, chars=1000), "relationships": _bound(relationships),
               "repair_feedback": (repair_feedback or "")[:2600],
               "critical_facts_valid": query_scope is not None and resource_binding is not None,
               "projection_contract": {"preserved": ["goal", "query_scope", "resource_binding", "repair_feedback"], "source_state_unchanged": True}}
    return Projection(payload, _ledger(payload, projection="text2sql"))


def final_answer_context(facts: dict[str, Any]) -> Projection:
    payload = {"question": facts.get("question", ""), "goal_coverage": _dump(facts.get("goal_coverage")),
               "quality_status": facts.get("quality_status"), "evidence": _bound(facts.get("evidence", []), rows=12, chars=1400),
               "claims": _bound(facts.get("claims", [])), "artifacts": _bound(facts.get("artifacts", [])),
               "uncertainties": _bound(facts.get("uncertainties", [])),
               "critical_facts_valid": "goal_coverage" in facts and "quality_status" in facts,
               "projection_contract": {"preserved": ["evidence", "claims", "goal_coverage", "quality_status", "artifacts"], "source_state_unchanged": True}}
    return Projection(payload, _ledger(payload, projection="final_answer"))
