# Scientific Agent interview guide

- LangGraph makes checkpoints, HITL resume, cancellation and state transitions auditable.
- GoalContract is the immutable user objective; PlanStep is an authorized execution proposal.
- QueryScope and SQLCandidate lineage prevent a syntactically valid but out-of-scope query from becoming Evidence.
- A failed Text2SQL candidate is diagnostic-only; Query Checker is callable only with a trusted candidate or legal historical/user SQL.
- Multiple populations receive independent bindings and cannot be deduplicated by identical SQL text.
- Context compaction changes only the model projection, never persisted observations or Evidence.
- Recovery is typed and bounded: repair parameters, retrieve schema, directed SQL repair, replan only on changed executable scope, or stop safely.
- The benchmark uses paired case IDs and reports missing token/latency data as `not_measured`; fake providers cannot establish Qwen quality.
