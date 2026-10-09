# Integration Environment Isolation

## Safety boundary

The existing development stack was inspected before any integration action:

- Compose project: `scientific_agent`
- PostgreSQL container: `scientific_agent-postgres-1`, port `55432`, volume `scientific_agent_postgres_data`
- MinIO container: `scientific_agent-minio-1`, ports `9000/9001`, volume `scientific_agent_minio_data`

Those containers, volumes, databases, buckets, tasks, events and historical evidence were not stopped, modified or queried by the integration run.

The isolated profile uses a separate Compose project and exact resource identities:

| Resource | Isolated value |
|---|---|
| Compose project | `scientific_agent_p1_integration` |
| PostgreSQL port | `55433` |
| MinIO API/console | `9002/9003` |
| PostgreSQL volume | `scientific_agent_p1_integration_postgres_data` |
| MinIO volume | `scientific_agent_p1_integration_minio_data` |
| Network | `scientific_agent_p1_integration_default` |
| MinIO bucket | `scientific-agent-p1-integration` |

The isolated database is named `scientific_agent` only inside the separate PostgreSQL container so the existing checked-in `docker/init.sql` can be reused. It is not the development database.

## Hard checks

`docker-compose.integration.yml` exposes only the isolated ports. `scripts/test-integration-isolated.ps1` refuses to start when the exact project, exact volumes, or any isolated port already exists. It never calls `down -v`, `volume prune`, or any broad cleanup command.

`SCIENTIFIC_AGENT_TEST_PROFILE=offline` and `SCIENTIFIC_AGENT_TEST_PROFILE=integration_isolated` prevent `app.config` from loading `.env`. Plain pytest clears database, MinIO and LLM variables before importing application modules. A live profile must be explicitly selected and is not part of this run.

The isolated stack was started successfully and reported healthy PostgreSQL and MinIO containers. The resources remain available for inspection; only the exact project may be stopped with `scripts/stop-integration-isolated.ps1`, which preserves both volumes.
