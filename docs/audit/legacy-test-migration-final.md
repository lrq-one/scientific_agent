# Legacy test migration final

The ten previously failing tests were migrated instead of skipped or made green through heuristic fallback. They now use the current contract: real LangGraph/runtime where applicable, fixed Fake Skill/Decision/Response providers, explicit SQLite read-only fixtures for cross-dataset semantics, and a direct service-level `PermissionError` assertion separate from HTTP/SSE mapping.

Migrated files:

- `tests/test_agent.py`: file evidence, mixed file/database obligations, current HITL resume and explicit-version SQL path.
- `tests/test_cross_dataset_skill.py`: guarded SQL/schema/permission/timeout and unsupported-dimension evidence contracts.
- `tests/test_hitl_resume.py`: current persisted HITL answer and scoped SQL replay.
- `tests/fake_runtime_support.py`: test-only isolated workspace/storage/checkpoint and deterministic provider boundaries.

The migrated group is `15 passed`. No production heuristic fallback was restored, no external service was contacted, and no test was marked `skip`/`xfail` to hide a failure. The PostgreSQL relationship assertion was made order-independent (`tests/test_postgres_runtime.py:28-34`) because the real schema contains multiple foreign-key relationships.
