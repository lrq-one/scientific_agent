# Scientific Agent Phase 4 v2 — evaluation status

Date: 2026-10-07. **Comprehensive Phase 4 v2 evaluation has not run.** This is an explicit status report, not a fabricated result table. Protocol: [`phase4_v2_evaluation_protocol.md`](phase4_v2_evaluation_protocol.md).

| Item | Observed status |
|---|---|
| Historic Phase 4 v1 | Preserved in `docs/phase4_evaluation_report.md` and `evaluation/final_metrics.json`; not rerun as new Test |
| Historic Follow-up Frozen Test | Inspected during previous debugging; historical regression only |
| Phase 4.5 Follow-up Dev | 20/20 deterministic local development audit; same cases were used for tuning, not independent Test |
| Batch 4 real PostgreSQL Skill smoke | `qwen3.7-flash` structured tool selection, fallback=false, read-only SQL returned 6 rows, 2 Evidence, `SUPPORTED_CONCLUSION` |
| Batch 4 live Tool-selection telemetry | Actual model `qwen3.7-flash`, no-thinking single-call smoke: 169 input / 151 output / 320 total tokens, 2723.03 ms, fallback=false. HTTP status not exposed. This is not whole-task usage. |
| Batch 5 mixed-resource real-Qwen smoke | Structured SQLCandidate for `train_v3`; PostgreSQL SQL returned 4 rows; versioned file–DB join returned 0 rows due ID mismatch; final `INSUFFICIENT_EVIDENCE`, no unnecessary HITL. One latest smoke: 906 measured LLM tokens across Tool selection + SQL, 10.67 s Agent stream, 2.21 ms first progress; not a distribution/P95. |
| Backend regression | 112 passed in 80.05 s after route/SQL changes; one later static v2 case-validator test passed separately |
| Frontend regression | 4 passed; production build passed after backend/frontend changes |
| New scenario/template draft | 15 Dev + 15 `test_draft` cases across 15 families; static validator passed; all `agent_drafted`, not reviewed/frozen |
| V1-compatible vs V2 same-new-set metrics | Not measured |
| Tokens, P95 latency, cost change | Not measured; no improvement claim |
| Scientific model inference | Blocked: no matching checkpoint/preprocessing adapter in project |

Observed failures and scope limits are recorded in [`phase45_implementation_report.md`](phase45_implementation_report.md). The next formal run must first satisfy the protocol's review, split, telemetry and cost gates. An independent Test must remain unopened until then.
