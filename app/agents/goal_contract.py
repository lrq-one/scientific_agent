"""GoalContract lifecycle helpers.

This module owns the narrow bridge from the existing state fields to the
persisted GoalContract.  It does not create a second planner or execution
state: the contract lives on ``ScientificAgentState`` and is checkpointed with
the rest of the run.
"""

from __future__ import annotations

import re
from typing import Iterable

from app.models.schemas import AgentDecision, GoalContract, PopulationRequirement, ScientificAgentState


_DIMENSION_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*(?:_type|_category|_class|split)\b", re.IGNORECASE)
_VERSION_RE = re.compile(r"\btrain[_-]?v\d+\b", re.IGNORECASE)
_FILE_RE = re.compile(r"[\w.-]+\.(?:csv|xlsx|xls)\b", re.IGNORECASE)
_DB_RE = re.compile(r"(?:\bSQL\b|\bdatabase\b|\btraining_db\b|数据库|数据表|预测误差|训练覆盖|model_run|prediction)", re.IGNORECASE)
_CSV_ARTIFACT_RE = re.compile(
    r"(?:export|download|save|output|write|导出|下载|保存|输出|写入)[^\n。！？]{0,50}csv|"
    r"csv[^\n。！？]{0,30}(?:export|download|save|output|write|导出|下载|保存|输出|写入)",
    re.IGNORECASE,
)


def _request(state: ScientificAgentState) -> str:
    return str(state.user_request or state.goal or "").strip()


def _contains_token(text: str, token: str) -> bool:
    return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])", text, re.IGNORECASE))


def _explicit_sources(state: ScientificAgentState, text: str) -> list[str]:
    sources: list[str] = []
    for datasource in state.resource_summary.authorized_datasources:
        if datasource and _contains_token(text, datasource):
            sources.append(datasource)
    if _DB_RE.search(text):
        sources.append("database")
    files = list(dict.fromkeys(match.group(0) for match in _FILE_RE.finditer(text)))
    if files:
        sources.append("file")
        # Preserve the user-named files as data-source requirements as well as
        # the coarse file capability used by GoalCoverage.
        sources.extend(files)
    return list(dict.fromkeys(sources))


def _explicit_deliverables(state: ScientificAgentState, text: str, sources: list[str]) -> list[str]:
    result: list[str] = []
    if "database" in sources or any(item != "file" and item in sources for item in state.resource_summary.authorized_datasources):
        result.append("database_analysis")
    if "file" in sources:
        result.append("file_analysis")
    if _CSV_ARTIFACT_RE.search(text):
        result.append("artifact")
    return result


def _explicit_dimensions(state: ScientificAgentState, text: str) -> list[str]:
    dimensions = list(state.query_scope.grouping)
    dimensions.extend(match.group(0) for match in _DIMENSION_RE.finditer(text))
    for item in state.requested_dimensions:
        if _contains_token(text, item):
            dimensions.append(item)
    return list(dict.fromkeys(dimensions))


def _explicit_version(text: str, supplied: str | None) -> str | None:
    matches = list(dict.fromkeys(match.group(0).lower().replace("-", "_") for match in _VERSION_RE.finditer(text)))
    if len(matches) == 1:
        return matches[0]
    return supplied if supplied and _contains_token(text, supplied) else None


def _sync_legacy_fields(state: ScientificAgentState) -> None:
    contract = state.goal_contract
    if contract is None:
        return
    state.requested_dimensions = list(contract.required_dimensions)
    state.required_deliverables = list(contract.required_deliverables)
    if contract.population_requirements:
        state.populations = [item.model_copy(deep=True) for item in contract.population_requirements]


def ensure_goal_contract(state: ScientificAgentState) -> GoalContract:
    """Create the initial frozen contract, or preserve an existing one."""
    if state.goal_contract is None:
        text = _request(state)
        sources = _explicit_sources(state, text)
        state.goal_contract = GoalContract(
            original_request=text,
            required_goals=[text] if text else [],
            required_dimensions=_explicit_dimensions(state, text),
            required_data_sources=sources,
            dataset_version=_explicit_version(text, state.dataset_version),
            population_requirements=[item.model_copy(deep=True) for item in state.populations],
            required_deliverables=_explicit_deliverables(state, text, sources),
        )
        state.goal_contract_history = []
    elif not state.goal_contract.frozen:
        raise ValueError("GoalContract is immutable outside an explicit HITL revision")
    _sync_legacy_fields(state)
    return state.goal_contract


def _revision(state: ScientificAgentState, **changes) -> GoalContract:
    current = ensure_goal_contract(state)
    state.goal_contract_history.append(current.model_copy(deep=True))
    state.goal_contract = current.model_copy(update={
        **changes,
        "contract_version": current.contract_version + 1,
        "frozen": True,
    })
    _sync_legacy_fields(state)
    return state.goal_contract


def _valid_original_span(contract: GoalContract, text: str, state: ScientificAgentState) -> bool:
    original = re.sub(r"\s+", " ", contract.original_request).strip().lower()
    candidate = re.sub(r"\s+", " ", text).strip().lower()
    if candidate and candidate in original:
        return True
    return any(candidate and candidate in str(item.get("answer", "")).lower() for item in state.hitl_answers)


def apply_decision_proposal(state: ScientificAgentState, decision: AgentDecision) -> GoalContract:
    """Accept only LLM fields that are provably grounded in the contract."""
    contract = ensure_goal_contract(state)
    proposed_dimensions = list(dict.fromkeys(decision.requested_dimensions))
    unknown_dimensions = [item for item in proposed_dimensions
                          if not _contains_token(contract.original_request, item)
                          and item not in contract.required_dimensions]
    if unknown_dimensions:
        raise ValueError(f"GoalContract rejects ungrounded requested dimensions: {unknown_dimensions}")
    additions = [item for item in proposed_dimensions if item not in contract.required_dimensions]
    proposed_deliverables = list(dict.fromkeys(decision.required_deliverables))
    unknown_deliverables = [item for item in proposed_deliverables if item not in contract.required_deliverables]
    # Validate the complete proposal before recording any revision. A model
    # proposal containing one valid and one invalid field must be rejected
    # atomically; otherwise the valid dimension would become a phantom
    # contract revision even though the decision itself was not accepted.
    if unknown_deliverables:
        raise ValueError(f"GoalContract rejects ungrounded deliverables: {unknown_deliverables}")
    if additions:
        contract = _revision(
            state,
            required_dimensions=list(dict.fromkeys([*contract.required_dimensions, *additions])),
            change_source="validated_proposal",
            change_reason="LLM proposal matched explicit original-request dimension",
        )
    _sync_legacy_fields(state)
    return contract


def accept_population_requirements(state: ScientificAgentState, populations: Iterable[PopulationRequirement]) -> GoalContract:
    """Persist only populations already checked against the original intent."""
    contract = ensure_goal_contract(state)
    proposed = [item.model_copy(deep=True) for item in populations]
    for item in proposed:
        if not _valid_original_span(contract, item.source_text, state):
            raise ValueError("GoalContract rejects a population not present in the original request or HITL answer")
    current = [item.model_dump(mode="json") for item in contract.population_requirements]
    incoming = [item.model_dump(mode="json") for item in proposed]
    if current and current != incoming:
        raise ValueError("ordinary Replan cannot change frozen GoalContract populations")
    if not current and incoming:
        contract = _revision(
            state,
            population_requirements=proposed,
            dataset_version=proposed[0].query_scope.dataset_version if proposed else contract.dataset_version,
            change_source="validated_proposal",
            change_reason="population spans validated against the original request",
        )
    else:
        _sync_legacy_fields(state)
    return contract


def revise_from_hitl(state: ScientificAgentState, answer: str) -> GoalContract:
    """Create a new contract version only when HITL changes a scope fact."""
    contract = ensure_goal_contract(state)
    new_version = _explicit_version(answer, state.query_scope.dataset_version)
    if new_version == contract.dataset_version and not re.search(r"\b(?:train|validation|test)\b|训练|版本", answer, re.I):
        return contract
    state.populations = []
    return _revision(
        state,
        dataset_version=new_version,
        population_requirements=[],
        change_source="hitl",
        change_reason=answer[:500],
        user_confirmed=True,
    )
