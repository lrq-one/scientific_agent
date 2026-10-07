# Phase 4 Conversation-aware Follow-up Routing

Updated: 2026-10-07T07:06:05+08:00

## Root cause

Before this change, `conversation_chat_stream` created a new task for every user message and unconditionally entered `ScientificAgent.stream`. `ScientificAgent.stream` called `RequestRouter.route_async(query, resources)`, whose structured prompt contained only the current user query and resource inventory. It did not receive the previous assistant message, previous task ID, conversation messages, task events, evidence, or artifacts.

Evidence was persisted by the API route and returned by the conversation-detail endpoint, but no execution path loaded it before intent routing. Consequently, a provenance question such as “给我证据你是怎么得到的这些？” could be interpreted as a new database task and repeat schema retrieval, Text-to-SQL, query checking, and SQL execution.

Concrete source locations after the fix:

- `app/agents/request_router.py:88-104`: the existing scientific intent router still accepts only `query` and `resources`; it is intentionally unchanged.
- `app/agents/scientific_agent.py:202-214`: the existing workflow still calls that router for actual analysis turns.
- `app/services/conversation_history.py:178`: the new repository lookup reconstructs the latest meaningful completed/failed analysis task.
- `app/api/routes.py:141-209`: the conversation endpoint now resolves follow-up context before the existing workflow.

The database already treated conversation and graph thread as distinct concepts. The frontend creates a new graph `thread_id` after each completed run (`web/src/App.vue:199,218`), while the same `conversation_id` links turns. The unique `(conversation_id, thread_id)` index remains unchanged.

## Architecture change

`ConversationContextResolver` classifies every conversation turn into:

- `NEW_TASK`
- `EVIDENCE_EXPLANATION`
- `RESULT_EXPLANATION`
- `CONTINUE_ANALYSIS`
- `REFINE_PREVIOUS_TASK`
- `RERUN_PREVIOUS_TASK`
- `ERROR_QUESTION`

Explicit language uses deterministic rules. Ambiguous contextual references use qwen3.7-flash structured output; a classifier failure takes the safe no-tool interpretation.

`EVIDENCE_EXPLANATION`, `RESULT_EXPLANATION`, and `ERROR_QUESTION` load persisted PostgreSQL rows and build `ProvenanceRecord`. They do not enter `ScientificAgent.stream`, Text-to-SQL, or SQL execution. The record includes datasource, dataset/model version, selected skills, tool results, SQL candidate, SQL parameters, raw rows, Evidence IDs, artifacts, final answer, and uncertainties.

The trace for these turns is exactly:

1. `FOLLOW_UP_TYPE`
2. `PROVENANCE_LOADED`
3. `FINAL_ANSWER`

It includes `previous_task_id`, `loaded_evidence_count`, `provenance_source=postgres`, and `new_tool_calls=0`.

`CONTINUE_ANALYSIS`, `REFINE_PREVIOUS_TASK`, `RERUN_PREVIOUS_TASK`, and `NEW_TASK` continue into the existing Agent workflow. Intent, skill routing, tool routing, and Text-to-SQL internals were not replaced.

For `REFINE_PREVIOUS_TASK` and `RERUN_PREVIOUS_TASK`, the API restores the previous user goal before entering the workflow. An explicit `train_v2` refinement replaces the old `train_v3` reference and supplies `dataset_version=train_v2` to the existing Agent. This was added after a real smoke run showed that sending only “换成 train_v2 再分析一次” produced an unrelated empty SQL result.

## Evidence quality gate

Empty SQL rows no longer create a synthetic `fused_ring coverage = 0` Evidence record. A nonempty result without a fused-ring group also no longer creates a false zero coverage claim. The finalizer returns `INSUFFICIENT_EVIDENCE` when Evidence is empty, a database task lacks database Evidence, or rows lack structural, count, or error fields required by the user's target question. It explicitly states that `0 rows` does not mean the scientific object does not exist.

Evidence persistence now retains `dataset_version` and `model_version`. Existing installations are migrated with idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements.

## Evaluation data

- Dev: 50 cases
- Frozen Test: 20 cases
- Categories: evidence questions, result explanations, continuations, refinements, reruns, error questions, and real new tasks
- Dataset SHA-256: `d8aa5f27b02fb0b6ef5336fa150cf75b3a36b9af198541be9c4689e900afb295`
- Model: `qwen3.7-flash`
- Thinking: disabled
- The original Frozen Test was run after Dev calibration. It was rerun without changing classification rules after a separate real four-turn smoke test exposed the missing prior-goal restoration for REFINE/RERUN. Both run versions are retained.
- Timing, token, and ToolCall metrics below come from the saved per-case real-LLM runs using an in-memory fixture shaped like persisted PostgreSQL rows. They do not include HTTP or PostgreSQL fetch latency. The separate four-turn smoke test verifies actual PostgreSQL persistence and reuse.

## Measured results

### Dev (50)

| Metric | Baseline | Optimized |
|---|---:|---:|
| Follow-up Routing Accuracy | 16% | 100% |
| Evidence Provenance Accuracy | 0% | 100% |
| Evidence Citation Completeness | 0% | 100% |
| Unnecessary Tool Call Rate | 66.67% | 0% |
| Total Tool Calls | 174 | 88 |
| LLM API calls | 118 | 54 |
| Tokens / follow-up (all cases) | 2,880.90 | 1,405.96 |
| Mean follow-up latency | 10.52 s | 3.01 s |
| P95 follow-up latency | 32.98 s | 8.80 s |
| Tokens / persisted-evidence reuse | 2,495.04 | 0 |
| Tool Calls / persisted-evidence reuse | 80 | 0 |

An earlier optimized run contained an 86,627-token `CONTINUE_ANALYSIS` outlier and remains in `evaluation/runs/followup_dev_optimized/`. The final `goal_restore_v2` run shown above did not reproduce that outlier. The evidence-reuse path used zero tokens on Dev.

### Frozen Test (20)

| Metric | Baseline | Optimized |
|---|---:|---:|
| Follow-up Routing Accuracy | 15% | 80% |
| Evidence Provenance Accuracy | 0% | 100% |
| Evidence Citation Completeness | 0% | 100% |
| Unnecessary Tool Call Rate | 77.78% | 0% |
| Total Tool Calls | 66 | 46 |
| LLM API calls | 53 | 22 |
| Tokens / follow-up | 3,944.70 | 1,398.50 |
| Mean follow-up latency | 10.14 s | 6.74 s |
| P95 follow-up latency | 24.91 s | 31.36 s |
| Tokens / persisted-evidence reuse | 2,676.89 | 48.67 |
| Tool Calls / persisted-evidence reuse | 35 | 0 |

Strict Test routing misses were retained: one result explanation was labeled evidence explanation (same safe reuse behavior), one continuation was labeled error question, and two workflow-allowed turns were labeled `NEW_TASK`. The continuation miss is a functional limitation: “异常结构” is overmatched by the error rule. No frozen-test miss caused a persisted-evidence question to execute a tool. P95 rose in the final Test run because of a long workflow-allowed case; this remains an open core-Agent latency issue.

## Regression verification

The PostgreSQL-backed four-turn regression verifies:

1. train_v3 structure coverage performs the original workflow and persists SQL/Evidence.
2. “这些结果怎么得到的？” loads that task and executes no tools.
3. “把 fused-ring 的原始证据告诉我。” still resolves to the original analysis task and executes no tools.
4. “换成 train_v2 再分析一次。” is `REFINE_PREVIOUS_TASK` and is allowed to re-enter the workflow.

It also asserts conversation/task/thread relationships and the exact follow-up trace. Full backend regression after all changes: `64 passed`. Frontend history tests: `4 passed`.

A separate real qwen3.7-flash/PostgreSQL four-turn smoke run used conversation `e5d74930-7732-44d1-b8fb-0be55a7d0147`. The first task generated SQL through `llm_structured_output` and saved two train_v3 Evidence rows. Both evidence follow-ups loaded those two rows with zero new tool calls. The final train_v2 refinement restored the prior goal, executed a new SQL query, and saved train_v2 Evidence. No API key was logged.

The post-test PostgreSQL audit for conversation `41cb6be3-762e-4159-baf8-0a12e10545ce` showed both explanation tasks with event sequence `{FOLLOW_UP_TYPE, PROVENANCE_LOADED, FINAL_ANSWER}`. Task `2a5360dd-fac9-4db5-9191-5efc1b8255e0` pointed to previous task `4acd0c38-629d-4547-b241-ddaf196a0586`, loaded one Evidence row from PostgreSQL, and recorded `new_tool_calls=0`. The later `REFINE_PREVIOUS_TASK` contained tool events and persisted `dataset_version=train_v2`, as expected.

## Reproducibility artifacts

- `evaluation/followup/cases.jsonl`
- `evaluation/splits/followup.json`
- `evaluation/frozen_config.json`
- `evaluation/run_followup.py`
- `evaluation/followup_metrics.json`
- `evaluation/runs/followup_dev_baseline/`
- `evaluation/runs/followup_dev_optimized/`
- `evaluation/runs/followup_test_baseline/`
- `evaluation/runs/followup_test_optimized/`
- `evaluation/runs/followup_dev_optimized_goal_restore_v2/`
- `evaluation/runs/followup_test_optimized_goal_restore_v2/`
- `evaluation/smoke_followup_postgres.py`
