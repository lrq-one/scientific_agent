# End-to-end controlled delivery

Cases: 34; split: replay_plus_live_reference.
Baseline: 209fda8 (engineering/intelligence baseline used by ten-case live comparison).
Optimized code: app/agents/runtime.py; app/api/routes.py; web/src/executionStages.js.

## Measurement boundary

Only the committed 10-case real-Qwen baseline/optimized/no-skill artifacts and existing isolated E2E runs are used. The 34-case package is a reproducibility scaffold, not a new success claim.

## Limitations

- Ten-case live comparison is insufficient to generalize six-track intelligence gains.
- No new Qwen calls were made because the prior ledger has 189/250 provider calls and the remaining budget is reserved for a separately approved paired run.

## Failure analysis

- Optimized strict success increased by one case in the committed 10-case run, while provider calls and P95 latency increased.
- No-skill matched optimized strict success; attribution to Skill/Prompt/Context is not established.
