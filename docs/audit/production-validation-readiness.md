# Production Validation Readiness

## Current state

P1 core protocol and the isolated PostgreSQL/MinIO boundary are validated. The isolated run used synthetic fixtures, Fake Skills/Decision/Response providers and a persistent checkpointer. The formal integration test count was `9 passed`; the current offline full suite is `390 passed / 10 failed / 38 skipped` and remains the default gate.

The existing development stack and historical data were not used as test fixtures. No Task/Event/Evidence history or frozen result was rewritten.

## LLM accounting

All formal offline and isolated integration commands cleared model variables and reported no paid-model calls. During development, one manual diagnostic command was accidentally run before clearing the inherited LLM environment and caused one real Text2SQL request. Its output is excluded from all acceptance results. No further model calls were made; the incident is recorded rather than hidden.

## Remaining conditions

The project is **not yet ready to claim live Qwen acceptance**. Before that stage, a separately authorized operator must still provide:

1. a reviewed `live_llm_manual` environment and explicit model-call budget;
2. route-level PostgreSQL persistence coverage for the complete A/B/C/D06/D09/M02/D08/HITL/RERUN/CANCEL matrix;
3. an independent review that the selected production database and MinIO identities are not the isolated or development identities;
4. a final human review of model telemetry, latency, error recovery and persisted status.

The safe merge posture is therefore: **P1 infrastructure boundary passed, live-model acceptance pending**. No push or merge to `main` was performed.
