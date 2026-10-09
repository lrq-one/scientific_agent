# Real-Qwen acceptance run (2026-10-10)

This report records the first explicitly authorized real-provider run. The
provider was the project-configured DashScope-compatible endpoint
(`dashscope.aliyuncs.com`) with model `qwen3.7-flash`. No API key, prompt body,
PostgreSQL connection, MinIO object, historical Task/Event/Evidence row, or
frozen benchmark file was read or written by the runner.

## Scope and budget

The harness used 10 paired acceptance cases (A, B, C, D06, D09, M02, D08,
HITL, RERUN, CANCEL) and synthetic schema/evidence only. It exercised the
production Skill, Planning, Text2SQL, Decision and GroundedResponse stages;
generated SQL was never sent to a database. Each request was sequential, had a
45-second provider timeout, and every retry/failure counted.

The three main runs used a per-run ceiling of 80 requests / 500,000 charged
tokens / $10 / 40 minutes. Aggregate main-run usage was 128 requests and
434,893 conservatively charged tokens. The focused C replay added 14 requests
and 42,532 charged tokens. Aggregate observed usage was therefore 142 requests
and 477,425 charged tokens, below the round limits of 250 requests,
1,500,000 tokens and $30. The conservative ledger estimated $0.08534 USD;
provider usage metadata was incomplete on some failure paths, so unknown total
tokens were charged from the reservation and never treated as zero. The Qwen
token rates used for the upper-tier estimate are the official Model Studio
rates: [Alibaba Cloud Model Studio pricing](https://help.aliyun.com/en/model-studio/model-pricing).

## Provenance and paired metrics

| run | checkout SHA | cases | requests | task success | p95 case latency | measured provider stages |
|---|---|---:|---:|---:|---:|---:|
| Baseline | `209fda8ecc15f130260b28af3e0dfdeb5fe6f9b4` | 10 | 46 | 0.60 | 21,213.86 ms | 42/46 |
| Optimized | `8e908aced7e39ac8cf31ff70e1f9cc957ebf621e` | 10 | 46 | 0.70 | 22,928.77 ms | 43/46 |
| No-Skill ablation | `8e908aced7e39ac8cf31ff70e1f9cc957ebf621e` | 10 | 36 | 0.70 | 17,617.16 ms | 33/46 |

Task success is intentionally strict: A file-only case needs a grounded
response; B/C/D08 database cases additionally require a successful
scope-verified Text2SQL stage. A prose response after a rejected SQL candidate
is not counted as completed scientific analysis. The ablation removes only the
Skill-routing request; it is not a second planner or a production workflow.

The complete machine-readable comparison is in
`evaluation/benchmark/results/live/live_comparison.json`, with cost and failure
ledgers alongside it. Each run directory contains an immutable manifest,
per-case JSONL, budget ledger and failure analysis.

## C focused replay

The focused C runs are:

- `c-focus-baseline-20261010` (baseline SHA above),
- `c-focus-optimized-20261010` (optimized SHA above),
- `c-focus-ablation-20261010` (optimized SHA, `no_skill`).

All three produced a `SQLScopeValidationError` with
`failure_code=UNVERIFIED_SCOPE`, reason
`outer/cross/implicit joins require population proof beyond this checker`.
The generated candidate is retained only as `diagnostic_candidate`; its
`params`, authoritative `QueryScope`, `scope_validation` and bounded recovery
instruction are persisted. The optimized C candidate used a
`training_molecules` CTE bound to `train_v3`/`train`, then left-joined
`molecules` and `predictions`; the baseline used a similarly bound CTE plus an
outer left join. This is a proof limitation for the complex join, not evidence
of a real scope violation, and the validator was not relaxed.

The Decision stage never received `query_checker` as an executable option after
this diagnostic failure: no trusted SQLCandidate or historical/user SQL source
existed. It selected schema retrieval as the next bounded action. No
`query_checker` call, SQL execution, PostgreSQL write or MinIO write occurred.

## Interpretation

The paired result supports one limited conclusion: the optimized branch
reduced the observed grounded-response failure on this synthetic sample (0.60
to 0.70 strict task success) while using the same number of main-stage
requests, but its p95 latency was higher. The C scope-proof limitation remains
unresolved by design and is reported as an honest bounded failure. These are
generation-stage acceptance measurements, not claims about real scientific
data quality or database throughput.
