# Scientific Agent Phase 4 v2 — evaluation protocol (draft; not frozen)

Date: 2026-10-07. This protocol is **not** a completed evaluation. Historic Phase 4 v1 reports, source JSONL, frozen config and metrics are immutable baselines. The inspected Follow-up Frozen Test is development-visible historical regression data, not a new independent Test. `evaluation/v2/followup_dev.jsonl` is a development set.

## 1. Evaluation populations and split hygiene

Create new Dev and Test cases from distinct concrete scenarios and prompt templates, not version-swapped copies. Record `scenario_family`, `template_family`, fixture snapshot/hash, required resource permissions, expected behavior, gold evidence requirements and review status in each case. The independent Test is not opened for model/prompt tuning after freeze. Deduplicate normalized prompts, check template-family non-overlap, and have an independent reviewer assess semantic scenario overlap (a lexical validator cannot prove it). A reviewer signs off gold labels and negative/insufficient-evidence expectations before generating `test_manifest.json` with SHA-256. Report Dev and Test separately. Any viewed Test case subsequently becomes historical regression and must not be relabeled as independent in another experiment.

The comparison uses the **same new case set** for a V1-compatible workflow and V2 workflow where both support the capability. New-capability coverage is reported separately from improvement of existing capabilities. Unsupported legacy cases count as coverage gaps, not as evidence of accuracy gain.

## 2. Coverage matrix

Each family needs normal, boundary, failure and negative cases; not all cells are currently authored. Scientific model inference is conditional and excluded until a verified checkpoint, preprocessing and adapter exist.

| Family | Representative measurable decision | Critical negative/failure |
|---|---|---|
| File-only | aligned metrics and subgroup Error | missing column / unaligned IDs |
| DB-only | guarded versioned statistics | zero rows / wrong dataset version |
| File + DB | molecule-ID join and source agreement | fixture ID namespaces differ |
| Scientific Tool / MCP | actual feature result provenance | MCP unavailable; no synthetic replacement |
| Multi-step Planning | dependency order and completion | blocked predecessor |
| Observation-driven Replanning | one repair after column error | duplicate SQLCandidate / exhausted budget |
| Tool Failure Recovery | classified action and bound | permission error must not retry |
| HITL / Checkpoint | same task/thread resume | duplicate, expired, wrong-thread, cancel |
| Permission / Security | authorized read only | SQL write, unapproved datasource |
| Empty / Conflicting Evidence | abstention with reason | 0 rows ≠ scientific absence |
| Artifact | content equals Evidence | empty/mismatched rows, cross-task isolation |
| Multi-turn Follow-up | prior task resolution | ambiguous referent / stale Evidence |
| Task Refinement | changed version only | old version Evidence reuse |
| Evidence Provenance | Claim→Evidence→Tool→Source | unsupported claim citation |
| Scientific Model Inference | checkpointed prediction | BLOCKED until real model available |

## 3. Metrics and denominators

Module metrics: Intent Accuracy/Macro-F1, Follow-up Routing Accuracy, Skill Recall@3/Top-1/MRR and multi-skill recall, Tool Selection Accuracy, JSON Schema Argument Validity, Required Completeness, Semantic Correctness, Execution-valid Rate, Schema Table/Column Recall@K, SQL Guard/EXPLAIN/Result Accuracy. Do not substitute exact-match for semantic validity or vice versa.

Agent metrics: Planning Success (all required dependent steps completed), Replan Trigger Precision/Recall, Recovery Success conditional on eligible recoverable failures, HITL durable Resume Success, Evidence Grounding Accuracy, Provenance Completeness, Artifact **content** Correctness, Multi-turn Task Success, Unauthorized Action Rate, Unsupported Conclusion Rate. Report numerator, denominator, confidence interval if sample size permits, and per-family failures. Empty/blocked cases are not silently removed.

Efficiency metrics: input/output tokens and LLM calls per task, Tool calls per task, cost per task and successful task, mean/P50/P95 wall latency, HTTP/SSE total and time-to-first-progress. Split latency into context loading, PostgreSQL retrieval, LLM API, tool execution, checkpoint I/O and finalization. Log actual `model`, HTTP status, fallback, token usage and request/call correlation without logging the API key. If a component does not expose telemetry, mark it `not_measured`; never impute zero.

## 4. Run and cost controls

1. Freeze fixtures, prompts, gold review and runner config before independent Test. Record exact code revision or source hash, PostgreSQL fixture version, model, SDK versions, seeds and cache settings.
2. Static checks and unit/integration tests precede real-LLM smoke. Each development batch begins with at most 20 real Qwen calls, default no-thinking for routing; cache only exact config/prompt/model matches. Do not run historic Frozen Test as a new Test.
3. Before a full v2 run, estimate `cases × methods × expected calls`, token range and price from a currently verified vendor tariff. Stop and request an explicit cost decision if the initial budget would be exceeded. No full v2 benchmark has yet been authorized under this protocol.
4. Per run save config, per-case JSONL, aggregate metrics and errors, plus source/config hash. Capture failed and fallback cases rather than retrying until favorable.
5. Never change gold labels or hidden business rules to improve a score. Analyze and report regressions, especially P95 and cost. Use a genuinely new independent Test if Test was inspected during optimization.

## 5. Current gate

The current code has targeted 10-Skill and mixed-resource checks, plus per-call Tool-selection and SQL-generation usage/latency on the smoke path. It still lacks full product HTTP/SSE component telemetry, a complete scenario/template-disjoint reviewed Test, generalized Skill execution, verified scientific-model weights and a full content-level E2E artifact matrix. Therefore the v2 comprehensive benchmark and any before/after accuracy/cost claims remain pending. This document freezes no Test data by itself.

`evaluation/v2/scenario_cases.jsonl` now contains 15 Dev and 15 `test_draft` cases, one per family in each split. `evaluation.v2.validate_scenarios` verifies IDs, family coverage, no shared template label and no exact normalized prompt overlap. These cases are **agent-drafted only**, lack full runnable fixture contracts and the four cases per family required for comprehensive coverage, and are not an independent frozen Test. The lexical checks cannot establish semantic scenario disjointness; review is still required.
