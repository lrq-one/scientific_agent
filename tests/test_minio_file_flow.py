from io import BytesIO
import os
from pathlib import Path
import uuid

import psycopg
import pytest

from app.services.object_storage import ObjectStorageService, PostgresFileMetadataRepository
from app.services.workspace import WorkspaceService


ADMIN_URL = os.getenv("TEST_CHECKPOINT_URL", "postgresql://scientific:scientific@127.0.0.1:55432/scientific_agent")


def infrastructure_available() -> bool:
    try:
        from minio import Minio
        Minio("127.0.0.1:9000", access_key="minioadmin", secret_key="change-me", secure=False).list_buckets()
        with psycopg.connect(ADMIN_URL, connect_timeout=2) as connection:
            connection.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not infrastructure_available(), reason="MinIO/PostgreSQL integration services are unavailable")
def test_minio_upload_metadata_and_lazy_materialization(tmp_path: Path):
    from minio import Minio
    owner = f"owner-{uuid.uuid4().hex[:8]}"
    service = ObjectStorageService(
        client=Minio("127.0.0.1:9000", access_key="minioadmin", secret_key="change-me", secure=False),
        repository=PostgresFileMetadataRepository(ADMIN_URL),
        workspace=WorkspaceService(tmp_path),
    )
    payload = b"molecule_id,observed_rt,predicted_rt\nM1,1.0,1.1\n"
    metadata = service.upload(owner, "thread", "uploaded.csv", "text/csv", BytesIO(payload), len(payload))
    assert metadata.object_key.startswith(f"{owner}/thread/")
    assert service.list_files(owner, "thread") == ["uploaded.csv"]
    materialized = service.materialize(owner, "thread", "uploaded.csv")
    assert materialized.read_bytes() == payload

