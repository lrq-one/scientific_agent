from __future__ import annotations

from pathlib import Path
import os
import sqlite3
from typing import Any

from app.config import DEMO_DATA, MAX_ROWS
from app.datasources.postgres import PostgresDatasource, PostgresQueryExecutor, PostgresSchemaInspector
from app.models.schemas import ToolResult
from app.tools.sql_guard import SQLGuard


class DatabaseService:
    """Request-scoped read-only database branch; no module-level datasource state."""

    def __init__(
        self,
        datasource_id: str,
        authorized: list[str],
        db_path: Path | None = None,
        database_url: str | None = None,
    ):
        if datasource_id not in authorized:
            raise PermissionError(f"datasource not authorized: {datasource_id}")
        self.datasource_id = datasource_id
        self.db_path = db_path or DEMO_DATA / "training_demo.db"
        self.guard = SQLGuard()
        self.database_url = database_url if database_url is not None else os.getenv("DATABASE_URL")
        self.backend = "postgres" if self.database_url and db_path is None else "sqlite"
        if self.backend == "postgres":
            datasource = PostgresDatasource(datasource_id, self.database_url)
            self.inspector = PostgresSchemaInspector(datasource)
            self.executor = PostgresQueryExecutor(datasource, max_rows=MAX_ROWS)

    def schema(self) -> ToolResult:
        if self.backend == "postgres":
            return ToolResult(
                success=True,
                data=self.inspector.tables(),
                source=self.datasource_id,
                metadata={"backend": "postgres"},
            )
        with sqlite3.connect(self.db_path) as connection:
            tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            schema = {}
            for table in tables:
                schema[table] = [{"name": row[1], "type": row[2]} for row in connection.execute(f"PRAGMA table_info('{table}')")]
        return ToolResult(success=True, data=schema, source=self.datasource_id)

    def get_table_schema(self, table: str) -> ToolResult:
        schema = self.schema().data
        if table not in schema:
            return ToolResult(success=False, source=self.datasource_id, error=f"table not allowed: {table}")
        return ToolResult(success=True, data={table: schema[table]}, source=self.datasource_id)

    def preview_table(self, table: str, limit: int = 20) -> ToolResult:
        schema = self.schema().data
        if table not in schema:
            return ToolResult(success=False, source=self.datasource_id, error=f"table not allowed: {table}")
        bounded = max(1, min(int(limit), 100))
        return self.execute(f'SELECT * FROM "{table}" LIMIT {bounded}')

    def search_schema(self, query: str, limit: int = 5) -> ToolResult:
        from app.services.text2sql import SchemaRetriever

        schema = self.schema().data
        relationships = self.relationships().data
        hits = SchemaRetriever(top_k=limit).search(query, schema, relationships)
        return ToolResult(success=True, data=hits, source=self.datasource_id, metadata={"retriever": "bm25"})

    def relationships(self) -> ToolResult:
        if self.backend == "postgres":
            return ToolResult(
                success=True,
                data=self.inspector.relationships(),
                source=self.datasource_id,
                metadata={"backend": "postgres"},
            )
        with sqlite3.connect(self.db_path) as connection:
            tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            relationships = []
            for table in tables:
                for row in connection.execute(f"PRAGMA foreign_key_list('{table}')"):
                    relationships.append(
                        {"source_table": table, "source_column": row[3], "target_table": row[2], "target_column": row[4]}
                    )
        return ToolResult(success=True, data=relationships, source=self.datasource_id, metadata={"backend": "sqlite"})

    def check_query(self, sql: str, params: dict[str, Any] | None = None) -> ToolResult:
        self.guard.validate_bindings(sql, params)
        schema = self.schema().data
        dialect = "postgres" if self.backend == "postgres" else "sqlite"
        safe_sql = self.guard.validate(sql, set(schema), dialect=dialect)
        if self.backend == "postgres":
            self.executor.check(safe_sql, params)
        else:
            sqlite_sql = self._sqlite_params(safe_sql)
            with sqlite3.connect(self.db_path, timeout=5) as connection:
                connection.execute(f"EXPLAIN QUERY PLAN {sqlite_sql}", params or {}).fetchall()
        return ToolResult(success=True, data={"valid": True}, source=self.datasource_id, metadata={"backend": self.backend})

    @staticmethod
    def _sqlite_params(sql: str) -> str:
        import re
        return re.sub(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s", r":\1", sql)

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> ToolResult:
        self.guard.validate_bindings(sql, params)
        schema = self.schema().data
        dialect = "postgres" if self.backend == "postgres" else "sqlite"
        safe_sql = self.guard.validate(sql, set(schema), dialect=dialect)
        if self.backend == "postgres":
            rows = self.executor.execute(safe_sql, params)
            return ToolResult(
                success=True,
                data=rows,
                source=self.datasource_id,
                metadata={"sql": safe_sql, "params": params or {}, "max_rows": MAX_ROWS, "read_only": True, "backend": "postgres"},
            )
        limited = f"SELECT * FROM ({safe_sql.rstrip(';')}) AS guarded_query LIMIT {MAX_ROWS}"
        limited = self._sqlite_params(limited)
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(limited, params or {}).fetchall()]
        return ToolResult(
            success=True,
            data=rows,
            source=self.datasource_id,
            metadata={"sql": safe_sql, "params": params or {}, "max_rows": MAX_ROWS, "read_only": True, "backend": "sqlite"},
        )

