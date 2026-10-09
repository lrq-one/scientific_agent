# Text2SQL + QueryScope

Cases: 100; split: dev+test_source_fixture.
Baseline: d4b3a65 (pre-lineage QueryScope/SQLCandidate recovery).
Optimized code: app/services/text2sql.py; app/services/query_scope.py; app/services/query_decomposition.py.

## Measurement boundary

Existing 70-case Qwen Text2SQL and focused C live runs are preserved. New decomposition/lineage checks are offline only; no scope relaxation or database writes.

## Limitations

- C remains a safe UNVERIFIED_SCOPE rejection in the focused live trace.
- Complex JOIN proof coverage needs additional independently labeled SQL cases.

## Failure analysis

- UNVERIFIED_SCOPE is distinct from SCOPE_VIOLATION and remains a safe rejection.
- model_run→experiment→dataset_version and training_memberships→dataset_version are now audited before recommending independent populations.
