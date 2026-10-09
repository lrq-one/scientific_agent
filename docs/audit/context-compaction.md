# Context projection and compaction

`app/services/context_projection.py` provides bounded Decision, Text2SQL and
final-response views. Projection is read-only and retains user requirements,
GoalContract, ResourceBinding, QueryScope, population IDs, SQL-candidate
lineage, Evidence IDs, artifacts and uncertainty. Only the model view is
compacted; state, checkpoints, ToolResults and audit events are untouched.
Each boundary records an approximate character/token ledger.
