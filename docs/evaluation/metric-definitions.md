# Metric definitions

- Task success: terminal result satisfies persisted GoalCoverage and quality gate.
- Evidence coverage: required Evidence items are present and linked to the case.
- Skill Macro-F1: multilabel macro average over expected versus observed Skills.
- Tool success: successful observations divided by recorded tool calls.
- Invalid ToolCall rate: rejected, unauthorized or malformed calls divided by calls.
- Recovery success: a bounded failure path reaches a verified outcome without relaxing scope.
- Calls/replans: mean over non-null persisted case traces.
- Token/latency: usage ledger values; missing values are `not_measured`.

All comparisons are paired by case ID. Diagnostic Text2SQL candidates and failed
observations are never treated as success.
