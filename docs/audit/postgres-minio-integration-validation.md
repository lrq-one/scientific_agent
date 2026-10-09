# PostgreSQL / MinIO Integration Validation

## PostgreSQL

The isolated PostgreSQL instance was initialized from `docker/init.sql` with synthetic fixtures only. It contains the schema used by the runtime, `agent_reader` with read-only transaction defaults, and `scientific` for isolated task/checkpoint metadata.

Validated with the real `DatabaseService`, `PostgresDatasource`, SQL guard and PostgreSQL executor:

- schema and relationship discovery passed;
- authorized read returned fixture rows;
- `DELETE` was rejected by the application SQL guard;
- direct write through `agent_reader` was rejected by PostgreSQL read-only transaction policy;
- `model_run -> experiment -> dataset_version` scope lineage was used by the real QueryScope checker;
- persistent `PostgresSaver` setup and restart/resume passed.

## MinIO

The isolated MinIO endpoint and bucket were used by the real `ObjectStorageService`, `PostgresFileMetadataRepository` and `ArtifactService`:

- CSV upload wrote an object and isolated metadata row;
- listing and lazy materialization returned the same bytes;
- D06 saved a generated CSV artifact to the isolated bucket;
- the test downloaded the object by its returned `object_key` and verified the CSV header/content;
- no object or metadata was written to the development bucket or database.

## Executed result

With all LLM variables removed and Fake Skills/Decision/Response providers injected:

```text
tests/test_postgres_runtime.py
tests/test_persistent_checkpoint.py
tests/test_minio_file_flow.py
tests/test_p1_langgraph_integration.py
9 passed
```

The run used the isolated endpoints on `55433` and `9002`; it did not use the existing `55432`/`9000` services.
