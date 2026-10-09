from __future__ import annotations

import psycopg
from app.services.database_errors import is_database_storage_corruption


def test_postgres_data_corruption_sqlstate_is_fatal():
    error = psycopg.errors.DataCorrupted("invalid page in block 0 of relation base/16384/16398")
    assert is_database_storage_corruption(error)


def test_postgres_invalid_page_fallback_is_fatal():
    error = psycopg.InternalError("invalid page in block 0 of relation base/16384/16398")
    assert is_database_storage_corruption(error)


def test_sql_repairable_errors_are_not_storage_corruption():
    assert not is_database_storage_corruption(psycopg.errors.UndefinedColumn("column absent"))
    assert not is_database_storage_corruption(ValueError("scope invalid"))


def test_nested_psycopg_storage_fault_is_detected():
    try:
        try:
            raise psycopg.errors.DataCorrupted("invalid page")
        except psycopg.Error as exc:
            raise RuntimeError("database tool execution failed") from exc
    except RuntimeError as outer:
        assert is_database_storage_corruption(outer)
