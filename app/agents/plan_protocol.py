"""Verified step lifecycle and atomic replacement; no scientific task routing."""
from __future__ import annotations
import hashlib
import json
import re
import uuid
from app.models.schemas import CompletionCondition

SCHEMA_TOOLS = {"search_schema", "get_table_schema", "get_table_relationships", "inspect_table", "list_workspace_files", "list_datasources"}
METADATA_TOOLS = SCHEMA_TOOLS | {"text_to_sql", "query_checker"}
ARTIFACT_TOOLS = {"save_result_table", "save_chart", "plot_metric_comparison"}


def condition_for(step):
    tools = step.selected_tools or step.preferred_tools
    if step.completion_condition:
        return step.completion_condition
    if "execute_readonly_sql" in tools:
        return CompletionCondition(kind="EXECUTED_ROWS", required_tools=["execute_readonly_sql"], require_scope_match=True)
    if set(tools) & ARTIFACT_TOOLS:
        return CompletionCondition(kind="ARTIFACT", required_tools=sorted(set(tools) & ARTIFACT_TOOLS))
    empirical = [t for t in tools if t not in METADATA_TOOLS]
    if empirical:
        return CompletionCondition(required_tools=empirical)
    if tools and set(tools) <= SCHEMA_TOOLS:
        return CompletionCondition(kind="SCHEMA", required_tools=[])
    return CompletionCondition(required_tools=[t for t in tools if t not in SCHEMA_TOOLS])


def satisfied(step):
    condition = condition_for(step)
    successful = [o for o in step.observations if o.get("success") and o.get("goal_satisfied", True) and
                  (not condition.require_scope_match or o.get("scope_verified")) and
                  (not step.population_id or o.get("tool") != "execute_readonly_sql" or
                   o.get("population_id") == step.population_id and step.query_scope is not None and
                   o.get("effective_query_scope") == step.query_scope.model_dump(mode="json"))]
    names = {o.get("tool") for o in successful}
    if condition.kind == "SCHEMA":
        return bool(names & SCHEMA_TOOLS) and set(condition.required_tools) <= names
    return bool(condition.required_tools) and set(condition.required_tools) <= names


def step_signature(step):
    return (re.sub(r"\s+", " ", step.goal).strip(), tuple(sorted(step.selected_tools or step.preferred_tools)),
            condition_for(step).model_dump_json(), step.query_scope.model_dump_json() if step.query_scope else None, step.population_id)


def step_action_signature(step):
    """Executable contract, deliberately independent of prose and generated IDs."""
    return (tuple(sorted(step.selected_tools or step.preferred_tools)), condition_for(step).model_dump_json(),
            step.query_scope.model_dump_json() if step.query_scope else None,
            tuple(sorted(c.value for c in step.required_capabilities)), step.population_id)


def plan_signature(plan):
    return hashlib.sha256(json.dumps([(s.step_id, step_signature(s), sorted(s.depends_on),
        sorted(c.value for c in s.required_capabilities)) for s in plan], ensure_ascii=False).encode()).hexdigest()


def plan_action_signature(plan):
    """Hash the action path, ignoring only cosmetic step IDs and goal rewording."""
    by_id = {step.step_id: step for step in plan}
    memo = {}
    def contract(step):
        if step.step_id not in memo:
            dependencies = sorted(contract(by_id[dep]) if dep in by_id else f"external:{dep}"
                                  for dep in step.depends_on)
            memo[step.step_id] = hashlib.sha256(json.dumps(
                [step_action_signature(step), dependencies], ensure_ascii=False).encode()).hexdigest()
        return memo[step.step_id]
    return hashlib.sha256(json.dumps(sorted(contract(step) for step in plan)).encode()).hexdigest()


def prepare_replacement(state, plan):
    """Prepare copies only. Never trust LLM-supplied statuses or observations."""
    new_id = str(uuid.uuid4())
    prepared = []
    for proposal in plan:
        step = proposal.model_copy(deep=True)
        from app.services.resource_grounding import effective_scope
        if step.population_id or set(step.selected_tools or step.preferred_tools) & {"text_to_sql", "query_checker", "execute_readonly_sql"}:
            step.query_scope = effective_scope(state, step.population_id)
        else:
            step.query_scope = state.query_scope.model_copy(deep=True)
        step.completion_condition = condition_for(step)
        step.status, step.observations, step.evidence_ids, step.completion_evidence = "pending", [], [], []
        step.error, step.result_summary, step.plan_id = None, None, new_id
        # Stable semantic contract may map to a different step ID. All verified
        # facts also remain plan-independent in state.tool_calls/observations.
        # Matching tools alone cannot prove equal inputs/outcomes (e.g. two
        # files both use calculate_metrics). Ambiguous facts stay in global
        # state, not attached to an unrelated step as completion evidence.
        matches = [o for o in state.plan if step_signature(o) == step_signature(step)]
        old = matches[0] if len(matches) == 1 else None
        if old:
            # Failed attempts remain in global observations/control feedback,
            # but are not copied into a replacement as if they were progress.
            step.observations = [dict(item) for item in old.observations if item.get("success")]
            step.evidence_ids = list(old.evidence_ids)
            step.completion_evidence = list(old.completion_evidence)
            step.result_summary = old.result_summary
            step.status = "completed" if satisfied(step) else "running" if step.observations else "pending"
        prepared.append(step)
    return new_id, prepared


def retain_verified_prerequisites(state, proposed):
    """A new plan may explicitly depend on a completed old fact.

    Retain that verified node, including its verified predecessors, BEFORE DAG
    validation. Unknown/unverified nodes are never invented or marked complete.
    """
    result = [s.model_copy(deep=True) for s in proposed]
    by_id = {s.step_id: s for s in result}
    old = {s.step_id: s for s in state.plan}
    cursor = 0
    while cursor < len(result):
        for dep in result[cursor].depends_on:
            fact = old.get(dep)
            if dep not in by_id and fact and fact.status == "completed" and satisfied(fact):
                copy = fact.model_copy(deep=True)
                by_id[dep] = copy
                result.append(copy)
        cursor += 1
    return result


def refresh_steps(state):
    completed = {s.step_id for s in state.plan if s.status == "completed"}
    for step in state.plan:
        if set(step.depends_on) - completed and step.status not in {"completed", "failed"}:
            step.status = "blocked"
        elif step.status == "blocked":
            step.status = "running" if step.observations else "pending"
        if not (set(step.depends_on) - completed) and satisfied(step):
            step.status = "completed"
            step.completion_evidence = [o["tool_call_id"] for o in step.observations if o.get("success") and o.get("tool_call_id")]
            completed.add(step.step_id)
