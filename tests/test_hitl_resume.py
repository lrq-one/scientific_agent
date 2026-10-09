import uuid

import pytest

from app.agents.scientific_agent import ScientificAgent
from app.models.schemas import AgentDecision, ResourceSummary, ScientificAgentState, ToolResult
from tests.fake_runtime_support import database_step, make_agent
from tests.test_p1_langgraph_no_model_e2e import FakeDatabase, _sql_candidate


@pytest.mark.asyncio
async def test_dataset_version_interrupt_and_command_resume(tmp_path, monkeypatch):
    async def fake_generate(self, goal, current_step, datasource, full_schema, relationships,
                            dataset_version=None, repair_feedback=None, skill_context=None,
                            original_goal=None, query_scope=None, resource_binding=None):
        candidate, validation = _sql_candidate(query_scope)
        return candidate, {"generator": "fake", "sql_candidate_status": "scope_verified",
                           "scope_validation": validation, "llm_telemetry": {"llm_called": True, "fallback": False}}

    monkeypatch.setattr("app.services.text2sql.TextToSQLService.generate", fake_generate)
    thread_id = f"hitl-{uuid.uuid4()}"
    query = "analyze fused-ring coverage in training_db"
    plan = [database_step("1", "search_schema"), database_step("3", "text_to_sql", depends_on=("1",)),
            database_step("4", "query_checker", depends_on=("3",)), database_step("5", "execute_readonly_sql", depends_on=("4",))]
    agent, _ = make_agent(
        tmp_path,
        ResourceSummary(authorized_datasources=["training_db"], resource_metadata={"dataset_versions": [{"version": "train_v3"}]}),
        [
            AgentDecision(action="ASK_USER", question_to_user="confirm dataset version",
                          missing_information=["dataset_version"], reason_summary="version required"),
            AgentDecision(action="REPLAN", plan=plan, reason_summary="install scoped query after confirmation"),
            AgentDecision(action="CALL_TOOL", step_id="1", tool_name="search_schema",
                          tool_arguments={"query": "train_v3 fused_ring coverage"}, reason_summary="schema"),
            AgentDecision(action="FINISH", reason_summary="coverage complete"),
        ],
        database_factory=lambda selected, allowed: FakeDatabase(selected, allowed),
    )
    first = [event async for event in agent.stream(query, "tester", thread_id, "training_db")]
    assert first[-1].event == "WAITING_FOR_USER"
    assert "dataset_version" in first[-1].data["missing_information"]

    resumed = [event async for event in agent.resume(thread_id, "train_v3", "tester")]
    assert resumed[-1].event == "FINAL_ANSWER"
    assert resumed[-1].data["state"]["thread_id"] == thread_id
    assert resumed[-1].data["state"]["hitl_answers"][-1]["answer"] == "train_v3"
    assert any(event.event == "EVIDENCE_ADDED" for event in resumed)


def test_multicolumn_sql_rows_are_preserved_as_evidence_and_finalized():
    agent = ScientificAgent()
    state = ScientificAgentState(
        user_id="tester",
        thread_id="multi-column",
        goal="analyze prediction errors for cyclic molecules",
        task_type="database_analysis",
    )
    rows = [
        {"is_fused_ring": False, "train_molecule_count": 4, "avg_absolute_error": 0.1967},
        {"is_fused_ring": True, "molecule_count": 2, "avg_absolute_error": 0.4200},
    ]

    evidence = agent._database_evidence(
        state,
        ToolResult(success=True, data=rows, source="training_db"),
        "training_db",
        "tool-1",
        "train_v3",
    )

    assert evidence[0].value == rows
    assert evidence[1].value == 2
    answer = agent._finalize(state)
    assert "avg_absolute_error" in answer
    assert answer != "structure_type"
