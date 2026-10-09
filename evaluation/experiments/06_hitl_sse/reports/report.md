# HITL + Checkpoint + SSE

Cases: 20; split: isolated_e2e_source_fixture.
Baseline: 3ed25de (pre-durable HITL/SSE lifecycle).
Optimized code: app/services/checkpointing.py; app/api/routes.py; web/src/executionState.js; web/src/executionStages.js.

## Measurement boundary

Backend isolated integration and frontend/browser tests are preserved. A new reconnect/fault-point distribution is not measured.

## Limitations

- The source E2E set has 20 cases, below the preferred 30–50; this is reported rather than padded with duplicates.
- Browser opt-in tests are not claimed as run when skipped by environment.

## Failure analysis

- Persisted event IDs are replayed after a cursor; UI stage grouping does not delete audit events.
- Reconnection and duplicate suppression need a dedicated browser fault-injection runner before claiming a rate.
