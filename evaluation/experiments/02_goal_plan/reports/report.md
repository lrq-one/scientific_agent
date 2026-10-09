# GoalContract + PlanStep

Cases: 34; split: replay_protocol.
Baseline: 66909da (pre-immutable GoalContract implementation / legacy completion paths).
Optimized code: app/agents/goal_contract.py; app/agents/plan_protocol.py; app/models/schemas.py.

## Measurement boundary

Deterministic protocol tests are the current evidence. A 34-case full paired metric run is not yet measured; test pass counts are not converted into task success claims.

## Limitations

- No independent semantic re-labeling of a new 34-case holdout in this batch.
- Historical A/B/C production records remain read-only references.

## Failure analysis

- Current runtime freezes user goals and rejects plan-only obligations.
- Independent semantic false-success measurement is still required before claiming improvement.
