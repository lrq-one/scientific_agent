# Implementation Status

## IMPLEMENTED

- FastAPI/Vue/SSE unified interface and structured Agent Trace UI.
- Deterministic resource-aware router plus real LLM structured-output integration path.
- DeepAgents 0.5.1 `create_deep_agent` runtime with tools, selected skills, FilesystemBackend and checkpointer.
- LangGraph plan state and PostgreSQL-backed checkpoint with `interrupt()` / `Command(resume=...)`.
- Generic PostgreSQL schema/relationship adapters and BM25 table retrieval.
- Structured Text-to-SQL candidate, named params, SQLGlot guard, EXPLAIN checker and normalized read-only execution.
- PostgreSQL synthetic fixtures and database-level read-only `agent_reader`.
- MinIO upload, PostgreSQL file metadata, owner/thread isolation and lazy workspace materialization.
- Real MCP stdio client/server list-tools and call-tool flow for `get_molecule_features`.
- Deterministic Evidence and bounded tool/replan/time limits.
- Automated tests for router, Text-to-SQL, PostgreSQL, checkpoint, HITL, MinIO, MCP, skills and mixed E2E.
- PostgreSQL-persistent conversations, messages, tasks, task events, evidence and artifacts with user isolation and soft deletion.
- Browser history routes at `/c/{conversation_id}`, reloadable messages, grouped history, rename/delete, and task-level trace replay.
- Nine contract-style scientific skills with validated YAML metadata and explicit tool/evidence/HITL policies.
- A 23-tool registry with input/output contracts, capability/risk/timeout/side-effect/role metadata, deterministic filters and structured LLM selection path.
- Expanded scientific schema for datasets, versions, features, experiments, model runs, predictions, training memberships, RT measurements, spectra and annotations.
- BM25 schema index over table/column names and descriptions plus PK/FK context and configurable Top-K.
- Real PNG chart and CSV/XLSX result artifact creation, MinIO storage, PostgreSQL metadata and authorized download.
- Evaluation data builders/validators and metric functions for intent, skill/tool routing, schema retrieval, Text-to-SQL and E2E; no benchmark score is claimed.

## PARTIAL

- LLM Request Router and LLM Text-to-SQL are implemented but not validated against a live external model in this environment because no API credentials were configured. Automated tests use deterministic structured fakes; runtime falls back explicitly.
- DeepAgents is the actual high-level scaffold and performs registered tool calls, while deterministic ScientificAgent code remains the authoritative business executor and evidence builder.
- Workspace copies are lazy and disposable, but automatic end-of-task cleanup is not yet enabled to keep debugging artifacts inspectable.
- PostgreSQL checkpoint and product history persistence are implemented; production connection pooling and a versioned migration framework are not included.

## NOT_IMPLEMENTED

- Real TC-TopoRT weights/inference.
- Dense schema retrieval, vector database or hybrid RRF.
- Production authentication/authorization provider beyond required `X-User-Id` and demo datasource policy.
- Distributed task queue, multi-process event replay and production observability stack.
- Formal live-LLM benchmark execution and score reports (intentionally not run in this phase).

