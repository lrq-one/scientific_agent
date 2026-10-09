# RecoveryPolicy + Deterministic Executor

Cases: 30; split: dev_draft_protocol.
Baseline: d0a9d6b (unified recovery was not yet bounded by current failure codes).
Optimized code: app/services/recovery.py; app/agents/runtime.py.

## Measurement boundary

Fault-injection coverage is represented by existing tests and C live traces. No new Qwen or database run was executed.

## Limitations

- scenario_cases labels are agent_drafted and not a frozen holdout.
- Recovery latency/cost distributions are not measured for this package.

## Failure analysis

- C now records diagnostic candidate, scope reason and one directed repair budget.
- No-progress termination is enforced, but full fault-injection matrix still needs a dedicated runner.
