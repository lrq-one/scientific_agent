from pathlib import Path

import pytest

from app.agents.scientific_agent import ScientificAgent, ToolLimitExceeded
from app.models.schemas import AgentDecision, Capability, PlanStep, ResourceSummary, ScientificAgentState, ToolResult
from tests.fake_runtime_support import copy_demo_files, database_step, make_agent
from tests.test_p1_langgraph_no_model_e2e import FakeDatabase, _sql_candidate


@pytest.mark.asyncio
async def test_simple_file_route_runs(tmp_path):
    plan = [PlanStep(step_id="1", goal="inspect the named CSV", selected_tools=["inspect_table"],
                     required_capabilities=[Capability.FILE])]
    agent, _ = make_agent(
        tmp_path, ResourceSummary(available_files=["model_v1.csv"]),
        [
            AgentDecision(action="REPLAN", plan=plan, reason_summary="inspect requested CSV"),
            AgentDecision(action="CALL_TOOL", step_id="1", tool_name="inspect_table",
                          tool_arguments={"filename": "model_v1.csv"}, reason_summary="read CSV"),
            AgentDecision(action="FINISH", reason_summary="row count observed"),
        ],
    )
    copy_demo_files(agent, "tester", "simple-file")
    events = [event async for event in agent.stream("model_v1.csv row count", "tester", "simple-file")]
    assert events[-1].event == "FINAL_ANSWER"
    assert any(event.event == "EVIDENCE_ADDED" and event.data["evidence"]["value"].get("rows") == 8 for event in events)


@pytest.mark.asyncio
async def test_mixed_end_to_end(tmp_path, monkeypatch):
    query = "compare model_v1.csv and model_v2.csv with training_db train_v3 structure coverage"

    async def fake_generate(self, goal, current_step, datasource, full_schema, relationships,
                            dataset_version=None, repair_feedback=None, skill_context=None,
                            original_goal=None, query_scope=None, resource_binding=None):
        candidate, validation = _sql_candidate(query_scope)
        return candidate, {"generator": "fake", "sql_candidate_status": "scope_verified",
                           "scope_validation": validation, "llm_telemetry": {"llm_called": True, "fallback": False}}

    monkeypatch.setattr("app.services.text2sql.TextToSQLService.generate", fake_generate)
    plan = [
        PlanStep(step_id="1", goal="compare files", selected_tools=["compare_models"], required_capabilities=[Capability.FILE]),
        database_step("3", "text_to_sql", depends_on=("1",)),
        database_step("4", "query_checker", depends_on=("3",)),
        database_step("5", "execute_readonly_sql", depends_on=("4",)),
    ]
    agent, _ = make_agent(
        tmp_path, ResourceSummary(available_files=["model_v1.csv", "model_v2.csv"], authorized_datasources=["training_db"]),
        [
            AgentDecision(action="REPLAN", plan=plan, reason_summary="independent file and database obligations"),
            AgentDecision(action="CALL_TOOL", step_id="1", tool_name="compare_models",
                          tool_arguments={"filenames": ["model_v1.csv", "model_v2.csv"]}, reason_summary="file evidence"),
            AgentDecision(action="CALL_TOOL", step_id="2", tool_name="search_schema",
                          tool_arguments={"query": query}, reason_summary="schema evidence"),
            AgentDecision(action="CALL_TOOL", step_id="3", tool_name="text_to_sql",
                          tool_arguments={"goal": query}, reason_summary="scoped SQL"),
            AgentDecision(action="FINISH", reason_summary="mixed evidence complete"),
        ],
        database_factory=lambda selected, allowed: FakeDatabase(selected, allowed),
    )
    copy_demo_files(agent, "tester", "mixed")
    events = [event async for event in agent.stream(query, "tester", "mixed", "training_db")]
    names = [event.event for event in events]
    assert "PLAN_CREATED" in names
    assert names.count("TOOL_STARTED") == 5
    assert {item.data["evidence"]["source_type"] for item in events if item.event == "EVIDENCE_ADDED"} >= {"file", "database"}
    final = events[-1]
    assert final.event == "FINAL_ANSWER"
    assert final.data["state"]["goal_coverage"]["status"] == "SATISFIED"
    assert final.data["state"]["quality_status"] in {"SUPPORTED_CONCLUSION", "INSUFFICIENT_EVIDENCE"}


def test_tool_call_limit(monkeypatch):
    monkeypatch.setattr("app.agents.scientific_agent.MAX_TOOL_CALLS", 1)
    agent = ScientificAgent()
    state = ScientificAgentState(user_id="u", thread_id="t", goal="g")
    result = ToolResult(success=True, source="test")
    agent._record_tool(state, "one", result)
    with pytest.raises(ToolLimitExceeded):
        agent._record_tool(state, "two", result)


def test_evidence_construction():
    agent = ScientificAgent()
    state = ScientificAgentState(user_id="u", thread_id="t", goal="g")
    evidence = agent._add_evidence(state, "MAE", 1.25, "file", "x.csv", "tool-1", "v1")
    assert evidence.value == 1.25
    assert evidence.tool_call_id == "tool-1"
    assert state.evidence == [evidence]


@pytest.mark.asyncio
async def test_hitl_interrupt_and_resume(tmp_path):
    agent, _ = make_agent(
        tmp_path, ResourceSummary(),
        [
            AgentDecision(action="ASK_USER", question_to_user="provide the missing file",
                          missing_information=["filename"], reason_summary="file is required"),
            AgentDecision(action="FINISH", reason_summary="user supplied the file"),
        ],
    )
    first = [event async for event in agent.stream("analyze missing.csv", "tester", "hitl")]
    assert first[-1].event == "WAITING_FOR_USER"
    resumed = [event async for event in agent.resume("hitl", "uploaded missing.csv", "tester")]
    assert resumed[-1].event == "FINAL_ANSWER"


@pytest.mark.asyncio
async def test_explicit_dataset_version_does_not_trigger_hitl(tmp_path, monkeypatch):
    async def fake_generate(self, goal, current_step, datasource, full_schema, relationships,
                            dataset_version=None, repair_feedback=None, skill_context=None,
                            original_goal=None, query_scope=None, resource_binding=None):
        candidate, validation = _sql_candidate(query_scope)
        return candidate, {"generator": "fake", "sql_candidate_status": "scope_verified",
                           "scope_validation": validation, "llm_telemetry": {"llm_called": True, "fallback": False}}

    monkeypatch.setattr("app.services.text2sql.TextToSQLService.generate", fake_generate)
    query = "training_db train_v3 structure coverage"
    plan = [database_step("1", "search_schema"), database_step("3", "text_to_sql", depends_on=("1",)),
            database_step("4", "query_checker", depends_on=("3",)), database_step("5", "execute_readonly_sql", depends_on=("4",))]
    agent, _ = make_agent(
        tmp_path, ResourceSummary(authorized_datasources=["training_db"], resource_metadata={"dataset_versions": [{"version": "train_v3"}]}),
        [
            AgentDecision(action="REPLAN", plan=plan, reason_summary="explicit version"),
            AgentDecision(action="CALL_TOOL", tool_name="search_schema", step_id="1", tool_arguments={"query": query}, reason_summary="schema"),
            AgentDecision(action="FINISH", reason_summary="version covered"),
        ],
        database_factory=lambda selected, allowed: FakeDatabase(selected, allowed),
    )
    events = [item async for item in agent.stream(query, "tester", "explicit-version-no-hitl", "training_db")]
    assert not any(item.event == "WAITING_FOR_USER" for item in events)
    sql = next(item for item in events if item.event == "TOOL_FINISHED" and item.data.get("tool") == "execute_readonly_sql")
    assert sql.data["result"]["metadata"]["params"]["dataset_version"] == "train_v3"
