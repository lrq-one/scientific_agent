import asyncio
import os
import uuid

import psycopg
import pytest

from app.services.checkpointing import CheckpointService


CHECKPOINT_URL = os.getenv("TEST_CHECKPOINT_URL", "postgresql://scientific:scientific@127.0.0.1:55432/scientific_agent")


def checkpoint_database_available() -> bool:
    try:
        with psycopg.connect(CHECKPOINT_URL, connect_timeout=2) as connection:
            connection.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


@pytest.mark.skipif(not checkpoint_database_available(), reason="PostgreSQL checkpoint database is unavailable")
def test_postgres_checkpoint_survives_service_recreation():
    thread_id = f"checkpoint-{uuid.uuid4()}"
    first = CheckpointService(CHECKPOINT_URL)
    interrupted = asyncio.run(first.start_hitl({"query": "coverage", "user_id": "u", "thread_id": thread_id, "datasource_id": "training_db"}))
    assert interrupted["__interrupt__"]
    assert first.persistent is True
    first.close()

    second = CheckpointService(CHECKPOINT_URL)
    resumed = asyncio.run(second.resume_hitl(thread_id, "train_v3"))
    assert resumed["status"] == "ready"
    assert resumed["dataset_version"] == "train_v3"
    second.close()

