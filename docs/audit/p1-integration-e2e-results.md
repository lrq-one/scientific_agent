# P1 Integration E2E Results

The test module `tests/test_p1_langgraph_integration.py` runs the production LangGraph nodes, runtime, PlanStep/GoalContract gates, ToolRegistry, ToolDispatcher, real PostgreSQL database tools, real MinIO storage and a persistent PostgreSQL checkpointer. Only semantic providers are deterministic fakes; no paid model is needed.

| Scenario | Real boundary | Result |
|---|---|---|
| A | production graph + local CSV files | passed; file goal reached `SATISFIED` |
| B | PostgreSQL schema → fake Text2SQL candidate → real Query Checker → real read-only executor | passed; backend and verified scope recorded |
| C | PostgreSQL schema/executor with first `UNVERIFIED_SCOPE` candidate | passed; first candidate was `diagnostic_only`, Checker was unavailable before repair, one bounded repair succeeded |
| D06 | production artifact path + isolated MinIO object | passed; downloaded CSV bytes matched the ToolResult artifact |
| HITL | PostgreSQL `PostgresSaver`, process recreation and resume | passed |
| RERUN | isolated offline LangGraph regression | passed offline; a separate real-infrastructure rerun was not claimed |
| D09/M02/D08/CANCEL | closure matrix and offline regression evidence | preserved and passed offline; not claimed as a new full route-level PostgreSQL E2E |

The focused integration command completed `9 passed`. The separate offline LangGraph matrix still covers RERUN, HITL, D09/M02/D08 and cancellation contracts without contacting external services.

The real PostgreSQL result is explicitly marked synthetic fixture data. It is evidence that the runtime, scope checks, permissions and persistence boundaries work together; it is not a scientific conclusion about production data.
