# Test Profile Migration

## Profiles

| Profile | Activation | External services | Purpose |
|---|---|---|---|
| `offline` | default `pytest` or `scripts/test-offline.ps1` | none; in-memory/temp fixtures | CI-safe protocol and regression suite |
| `integration_isolated` | `scripts/test-integration-isolated.ps1` | isolated PostgreSQL/MinIO only | real infrastructure boundary validation with Fake providers |
| `live_llm_manual` | manual operator setup only | explicitly authorized model and controlled resources | future human acceptance; not run in this round |

## Migration changes

- `app/config.py` no longer loads `.env` for the two test profiles.
- `tests/conftest.py` sets offline mode before importing application modules and clears service/model variables.
- PostgreSQL, checkpoint and MinIO tests no longer contain `55432`, `9000`, or the formal database as implicit defaults.
- MinIO tests read endpoint, credentials and bucket from explicit `TEST_MINIO_*` variables.
- The integration command supplies all URLs explicitly and uses a different port, volume and bucket identity.
- Existing tests remain present; integration tests skip only when their explicit profile variables are absent.

This separates unit/offline, infrastructure integration and future live-model acceptance. It does not convert a skipped integration test into a pass and does not restore heuristic fallback.
