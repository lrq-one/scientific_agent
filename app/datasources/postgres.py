from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row


@dataclass(frozen=True)
class PostgresDatasource:
    datasource_id: str
    database_url: str

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)


class PostgresSchemaInspector:
    def __init__(self, datasource: PostgresDatasource, allowed_tables: set[str] | None = None):
        self.datasource = datasource
        self.allowed_tables = allowed_tables or {
            "datasets",
            "dataset_versions",
            "molecules",
            "molecular_features",
            "experiments",
            "model_versions",
            "model_runs",
            "predictions",
            "training_memberships",
            "retention_time_measurements",
            "msms_spectra",
            "annotations",
            # Kept for backwards-compatible Case A/B queries.
            "training_molecules",
        }

    def tables(self) -> dict[str, list[dict[str, str]]]:
        sql = """
            SELECT c.table_name, c.column_name, c.data_type,
                   coalesce(obj_description(cls.oid), '') AS table_description,
                   coalesce(col_description(cls.oid, attr.attnum), '') AS description,
                   EXISTS (
                     SELECT 1 FROM pg_index idx
                     WHERE idx.indrelid = cls.oid AND idx.indisprimary
                       AND attr.attnum = ANY(idx.indkey)
                   ) AS is_primary_key
            FROM information_schema.columns c
            JOIN pg_class cls ON cls.relname = c.table_name
            JOIN pg_namespace ns ON ns.oid = cls.relnamespace AND ns.nspname = c.table_schema
            JOIN pg_attribute attr ON attr.attrelid = cls.oid AND attr.attname = c.column_name
            WHERE c.table_schema = 'public' AND c.table_name = ANY(%s)
            ORDER BY c.table_name, c.ordinal_position
        """
        schema: dict[str, list[dict[str, str]]] = {}
        with self.datasource.connect() as connection, connection.cursor() as cursor:
            cursor.execute(sql, (list(self.allowed_tables),))
            for row in cursor.fetchall():
                schema.setdefault(row["table_name"], []).append(
                    {
                        "name": row["column_name"],
                        "type": row["data_type"],
                        "description": row["description"],
                        "table_description": row["table_description"],
                        "primary_key": row["is_primary_key"],
                    }
                )
        return schema

    def relationships(self) -> list[dict[str, str]]:
        sql = """
            SELECT
              source.relname AS source_table,
              source_col.attname AS source_column,
              target.relname AS target_table,
              target_col.attname AS target_column
            FROM pg_constraint c
            JOIN pg_class source ON source.oid = c.conrelid
            JOIN pg_class target ON target.oid = c.confrelid
            JOIN pg_attribute source_col ON source_col.attrelid = c.conrelid AND source_col.attnum = c.conkey[1]
            JOIN pg_attribute target_col ON target_col.attrelid = c.confrelid AND target_col.attnum = c.confkey[1]
            JOIN pg_namespace ns ON ns.oid = source.relnamespace
            WHERE c.contype = 'f' AND ns.nspname = 'public'
              AND source.relname = ANY(%s) AND target.relname = ANY(%s)
        """
        with self.datasource.connect() as connection, connection.cursor() as cursor:
            allowed = list(self.allowed_tables)
            cursor.execute(sql, (allowed, allowed))
            return [dict(row) for row in cursor.fetchall()]


class PostgresQueryExecutor:
    def __init__(self, datasource: PostgresDatasource, timeout_ms: int = 5000, max_rows: int = 500):
        self.datasource = datasource
        self.timeout_ms = timeout_ms
        self.max_rows = max_rows

    def check(self, sql: str, params: dict[str, Any] | None = None) -> None:
        with self.datasource.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", (str(self.timeout_ms),))
            cursor.execute(f"EXPLAIN (FORMAT JSON) {sql}", params or {})
            cursor.fetchone()

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        limited = f"SELECT * FROM ({sql.rstrip(';')}) AS guarded_query LIMIT {self.max_rows}"
        with self.datasource.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", (str(self.timeout_ms),))
            cursor.execute(limited, params or {})
            return [dict(row) for row in cursor.fetchall()]

