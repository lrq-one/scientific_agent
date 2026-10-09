# Final isolated integration acceptance

Profile: `integration_isolated`; compose project `scientific_agent_p1_integration`; PostgreSQL `127.0.0.1:55433`; MinIO API `127.0.0.1:9002`; bucket `scientific-agent-p1-integration`. The original `scientific_agent` project (55432/9000) and its volumes were not used or modified.

Command result: `13 passed` covering the four infrastructure modules and the eight real LangGraph scenarios in `tests/test_p1_langgraph_integration.py`.

| Scenario | Acceptance evidence |
|---|---|
| A / D06 | real file analysis, isolated MinIO artifact, fresh rerun namespace |
| B | schema → Text2SQL → Query Checker → read-only executor; 4 calls, no duplicate SQL decision |
| C | failed Text2SQL is `diagnostic_only`; one bounded repair; then verified candidate, checker and executor |
| D09 | two user-declared populations have distinct population IDs, scopes, SQL candidates and evidence; both execute successfully |
| M02 | real CSV evidence and isolated PostgreSQL evidence are both required before `SATISFIED` |
| D08 | `model_runs` are linked through the experiment and dataset version; MAE rows are executed and exported to an isolated CSV artifact |
| HITL/checkpoint | persistent checkpoint survives service recreation |
| CANCEL | waiting and running cancellation reach terminal `cancelled`; late completion is rejected and CANCELLED events remain persisted |

The D09 runtime fix is limited to `app/agents/runtime.py:534-552`: a successful SQL tool may repeat only for a different trusted population; the same population and unplanned execution remain non-repeatable. This removes the false duplicate suppression without permitting loops.

## Execution-efficiency ledger

The fixed-provider traces retain every audit event. In the final isolated runs: D09 used 1 `REPLAN`, 2 scripted decision turns and 7 tool calls; M02 used 1 `REPLAN`, 5 decision turns and 5 tool calls; D08 used 1 `REPLAN`, 3 decision turns and 5 tool calls; B used 0 replans, 2 decision turns and 4 tool calls; C used 0 replans, 2 decision turns and 5 tool calls (the second Text2SQL call is the bounded directed repair). All final traces had 0 invalid ToolCalls. The historical C trace had one invalid Query Checker attempt, one overall replan and one no-progress replan; that behavior is covered by the C regression and is not hidden by deleting events.
