from types import SimpleNamespace

from app.models.schemas import QueryScope, ResourceBinding, ResourceSummary, ScientificAgentState
from app.services.context_projection import decision_context, final_answer_context, text2sql_context


def _state():
    state = ScientificAgentState(user_id="u", thread_id="t", goal="compare models", user_request="compare models")
    state.resource_summary = ResourceSummary()
    state.resource_binding = ResourceBinding()
    state.query_scope = QueryScope()
    state.conversation_context = {"history": ["x" * 5000] * 20}
    state.errors = ["diagnostic"]
    return state


def test_decision_projection_is_bounded_and_preserves_contract():
    state = _state()
    original = state.model_dump(mode="json")
    projected = decision_context(state, tools=[], skill_context="y" * 5000,
                                 callable_tools=[], pending_scope=[], actions=["FINISH"])
    assert projected.ledger["compacted"] is True
    assert projected.ledger["approx_tokens"] < len(str(original)) // 2
    assert projected.payload["projection_contract"]["source_state_unchanged"] is True
    assert state.model_dump(mode="json") == original


def test_specialized_projections_keep_scope_and_lineage_fields():
    scope = QueryScope()
    binding = ResourceBinding()
    sql = text2sql_context(goal="coverage", query_scope=scope, resource_binding=binding,
                           schema={"training": [{"name": "dataset_version"}]}, relationships=[])
    answer = final_answer_context({"question": "q", "goal_coverage": {"status": "PARTIAL"},
                                   "quality_status": "INSUFFICIENT_EVIDENCE", "evidence": [{"evidence_id": "ev-1"}]})
    assert sql.payload["query_scope"] == scope.model_dump(mode="json")
    assert sql.payload["resource_binding"] == binding.model_dump(mode="json")
    assert answer.payload["evidence"][0]["evidence_id"] == "ev-1"
