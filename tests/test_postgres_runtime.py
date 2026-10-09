import os
import uuid

import psycopg
import pytest

from app.tools.database_tools import DatabaseService
from app.tools.sql_guard import SQLGuardError


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")


def postgres_available() -> bool:
    if not POSTGRES_URL:
        return False
    try:
        with psycopg.connect(POSTGRES_URL, connect_timeout=2) as connection:
            connection.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(not postgres_available(), reason="PostgreSQL integration service is unavailable")


def test_postgres_schema_select_and_relationships():
    database = DatabaseService("training_db", ["training_db"], database_url=POSTGRES_URL)
    assert database.schema().metadata["backend"] == "postgres"
    assert database.relationships().data[0]["target_table"] == "molecules"
    assert database.execute("SELECT COUNT(*) AS count FROM training_molecules").data[0]["count"] >= 7


def test_postgres_application_guard_rejects_delete():
    database = DatabaseService("training_db", ["training_db"], database_url=POSTGRES_URL)
    with pytest.raises(SQLGuardError):
        database.execute("DELETE FROM training_molecules")


def test_postgres_read_only_credential_rejects_write_even_without_guard():
    with psycopg.connect(POSTGRES_URL) as connection:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute("DELETE FROM training_molecules WHERE molecule_id = %s", (f"none-{uuid.uuid4()}",))

