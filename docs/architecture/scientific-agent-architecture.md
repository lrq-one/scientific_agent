# Scientific Agent architecture

FastAPI persists conversations and events; the Scientific Agent builds a
GoalContract and ResourceBinding, selects Skills, and uses LangGraph decision
nodes. A deterministic executor validates PlanStep tool scopes and completion
predicates before dispatch. Schema/Text2SQL produces a SQLCandidate whose
QueryScope is verified before read-only execution. Evidence and artifacts are
persisted, GroundedResponse is gated by quality/coverage, and SSE projects the
same events to Vue. Recovery chooses bounded directed repair, HITL, replan or
safe stop according to failure kind.

The optimization layer is intentionally narrow: versioned prompts, read-only
node-specific Context Projection, Skill metadata caching and a UI stage
projection. It does not add a second Planner or mutate scientific state.
