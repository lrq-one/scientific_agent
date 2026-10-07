from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Protocol
import uuid

from minio import Minio
import psycopg
from psycopg.rows import dict_row

from app.models.schemas import FileMetadata
from app.services.workspace import WorkspaceService, WorkspaceError


class MetadataRepositoryProtocol(Protocol):
    def add(self, metadata: FileMetadata) -> None: ...
    def list(self, owner_id: str, thread_id: str) -> list[FileMetadata]: ...
    def find(self, owner_id: str, thread_id: str, filename: str) -> FileMetadata | None: ...


class PostgresFileMetadataRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def add(self, metadata: FileMetadata) -> None:
        with psycopg.connect(self.database_url) as connection:
            connection.execute(
                """
                INSERT INTO file_metadata
                  (file_id, object_key, owner_id, thread_id, filename, content_type, size)
                VALUES (%(file_id)s, %(object_key)s, %(owner_id)s, %(thread_id)s, %(filename)s, %(content_type)s, %(size)s)
                """,
                metadata.model_dump(),
            )

    def list(self, owner_id: str, thread_id: str) -> list[FileMetadata]:
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                """
                SELECT file_id::text, object_key, owner_id, thread_id, filename, content_type, size
                FROM file_metadata WHERE owner_id = %s AND thread_id = %s ORDER BY created_at
                """,
                (owner_id, thread_id),
            ).fetchall()
        return [FileMetadata.model_validate(row) for row in rows]

    def find(self, owner_id: str, thread_id: str, filename: str) -> FileMetadata | None:
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """
                SELECT file_id::text, object_key, owner_id, thread_id, filename, content_type, size
                FROM file_metadata
                WHERE owner_id = %s AND thread_id = %s AND filename = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (owner_id, thread_id, filename),
            ).fetchone()
        return FileMetadata.model_validate(row) if row else None


class ObjectStorageService:
    def __init__(
        self,
        client=None,
        repository: MetadataRepositoryProtocol | None = None,
        bucket: str | None = None,
        workspace: WorkspaceService | None = None,
    ):
        endpoint = os.getenv("MINIO_ENDPOINT")
        self.configured = client is not None or bool(endpoint and (repository or os.getenv("ADMIN_DATABASE_URL")))
        self.bucket = bucket or os.getenv("MINIO_BUCKET", "scientific-files")
        self.workspace = workspace or WorkspaceService()
        self.client = client
        self.repository = repository
        if self.client is None and self.configured:
            self.client = Minio(
                endpoint,
                access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
                secret_key=os.getenv("MINIO_SECRET_KEY", "change-me"),
                secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
            )
        if self.repository is None and self.configured:
            self.repository = PostgresFileMetadataRepository(os.environ["ADMIN_DATABASE_URL"])

    def ensure_bucket(self) -> None:
        if not self.configured:
            return
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def upload(
        self,
        owner_id: str,
        thread_id: str,
        filename: str,
        content_type: str,
        stream: BinaryIO,
        size: int,
    ) -> FileMetadata:
        self.workspace.safe_file(owner_id, thread_id, filename, create_workspace=False)
        self.ensure_bucket()
        file_id = str(uuid.uuid4())
        object_key = f"{owner_id}/{thread_id}/{file_id}/{filename}"
        stream.seek(0)
        self.client.put_object(self.bucket, object_key, stream, size, content_type=content_type)
        metadata = FileMetadata(
            file_id=file_id,
            object_key=object_key,
            owner_id=owner_id,
            thread_id=thread_id,
            filename=filename,
            content_type=content_type,
            size=size,
        )
        self.repository.add(metadata)
        return metadata

    def list_files(self, owner_id: str, thread_id: str) -> list[str]:
        if not self.configured:
            return []
        return [item.filename for item in self.repository.list(owner_id, thread_id)]

    def materialize(self, owner_id: str, thread_id: str, filename: str) -> Path | None:
        if not self.configured:
            return None
        metadata = self.repository.find(owner_id, thread_id, filename)
        if metadata is None:
            return None
        target = self.workspace.safe_file(owner_id, thread_id, filename, create_workspace=True)
        self.client.fget_object(self.bucket, metadata.object_key, str(target))
        return target

    def upload_artifact(
        self,
        owner_id: str,
        thread_id: str,
        filename: str,
        content_type: str,
        content: bytes,
        artifact_type: str,
        metadata: dict | None = None,
    ) -> dict:
        """Store generated output separately from user input file metadata."""
        self.workspace.safe_file(owner_id, thread_id, filename, create_workspace=False)
        if not self.configured:
            raise RuntimeError("MinIO is required for generated artifacts")
        self.ensure_bucket()
        artifact_id = str(uuid.uuid4())
        object_key = f"{owner_id}/{thread_id}/artifacts/{artifact_id}/{filename}"
        self.client.put_object(
            self.bucket,
            object_key,
            BytesIO(content),
            len(content),
            content_type=content_type,
        )
        return {
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "object_key": object_key,
            "filename": filename,
            "metadata": {"content_type": content_type, "size": len(content), **(metadata or {})},
        }

    def download_object(self, object_key: str) -> bytes:
        if not self.configured:
            raise RuntimeError("MinIO is not configured")
        response = self.client.get_object(self.bucket, object_key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

