from __future__ import annotations

from pathlib import Path
import os
import sqlite3
from typing import Any

from app.config import DEMO_DATA, MAX_ROWS
from app.datasources.postgres import PostgresDatasource, PostgresQueryExecutor, PostgresSchemaInspector
from app.models.schemas import ToolResult
from app.tools.sql_guard import SQLGuard, sanitize_result, SENSITIVE_FIELD


class DatabaseService:
    """Request-scoped read-only database branch; no module-level datasource state."""

    def __init__(
        self,
        datasource_id: str,
        authorized: list[str],
        db_path: Path | None = None,
        database_url: str | None = None,
        allowed_tables: set[str] | None = None,
        denied_tables: set[str] | None = None,
        sensitive_columns: set[str] | None = None,
    ):
        if datasource_id not in authorized:
            raise PermissionError(f"datasource not authorized: {datasource_id}")
        self.datasource_id = datasource_id
        self.db_path = db_path or DEMO_DATA / "training_demo.db"
        self.guard = SQLGuard()
        self.allowed_tables = allowed_tables
        self.denied_tables = denied_tables or {"secrets", "credentials", "users", "pg_authid", "pg_shadow"}
        self.sensitive_columns = sensitive_columns or set()
        self.database_url = database_url if database_url is not None else os.getenv("DATABASE_URL")
        self.backend = "postgres" if self.database_url and db_path is None else "sqlite"
        if self.backend == "postgres":
            datasource = PostgresDatasource(datasource_id, self.database_url)
            self.inspector = PostgresSchemaInspector(datasource, allowed_tables=allowed_tables,
                denied_tables=self.denied_tables, sensitive_columns=self.sensitive_columns)
            self.executor = PostgresQueryExecutor(datasource, max_rows=MAX_ROWS)

    def schema(self) -> ToolResult:
        if self.backend == "postgres":
            return ToolResult(
                success=True,
                data=self.inspector.tables(),
                source=self.datasource_id,
                metadata={"backend": "postgres", "schema_retrieval": {"complete": True, "scope": "authorized_datasource"}},
            )
        with sqlite3.connect(self.db_path) as connection:
            tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            schema = {}
            for table in tables:
                if table in self.denied_tables or (self.allowed_tables is not None and table not in self.allowed_tables):
                    continue
                schema[table] = [{"name": row[1], "type": row[2], "primary_key": bool(row[5])} for row in connection.execute(f"PRAGMA table_info('{table}')")
                                 if not SENSITIVE_FIELD.search(row[1]) and row[1] not in self.sensitive_columns]
        return ToolResult(success=True, data=schema, source=self.datasource_id,
            metadata={"schema_retrieval": {"complete": True, "scope": "authorized_datasource",
                "retrieved_tables": list(schema), "total_authorized_tables": len(schema)}})

    def get_table_schema(self, table: str) -> ToolResult:
        schema = self.schema().data
        if table == "*":
            return ToolResult(success=True, data=schema, source=self.datasource_id,
                metadata={"schema_retrieval": {"complete": True, "scope": "authorized_datasource",
                    "retrieved_tables": list(schema), "total_authorized_tables": len(schema)}})
        if table not in schema:
            return ToolResult(success=False, source=self.datasource_id, error=f"table not allowed: {table}")
        return ToolResult(success=True, data={table: schema[table]}, source=self.datasource_id,
            metadata={"schema_retrieval": {"complete": len(schema) == 1, "scope": "authorized_datasource",
                "retrieved_tables": [table], "total_authorized_tables": len(schema)}})

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
        # Lexical top-k can omit the dimension table that owns the grouping field.
        # Expand only one-hop FK neighbours from the already authorised schema.
        selected = {hit["table"] for hit in hits}
        neighbours = set()
        for relation in relationships:
            source, target = relation.get("source_table"), relation.get("target_table")
            if source in selected and target in schema:
                neighbours.add(target)
            if target in selected and source in schema:
                neighbours.add(source)
        expanded = sorted(neighbours - selected)[:limit]
        for table in expanded:
            hits.append({"table": table, "columns": schema[table], "score": 0,
                         "relationships": [r for r in relationships if table in (r.get("source_table"), r.get("target_table"))]})
        return ToolResult(success=True, data=hits, source=self.datasource_id,
                          metadata={"retriever": "bm25", "relationship_expanded_tables": expanded,
                              "schema_retrieval": {"complete": {h["table"] for h in hits} == set(schema),
                                  "scope": "authorized_datasource", "retrieved_tables": [h["table"] for h in hits],
                                  "total_authorized_tables": len(schema), "top_k": limit}})

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
        safe_sql = self.guard.validate(sql, set(schema), dialect=dialect, schema=schema, relationships=self.relationships().data)
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
        synthetic = (self.backend == "sqlite" and self.db_path.resolve().parent == DEMO_DATA.resolve()) or any(
            "synthetic" in str(column.get("table_description", "")).lower()
            for columns in schema.values() for column in columns
        )
        safe_sql = self.guard.validate(sql, set(schema), dialect=dialect, schema=schema, relationships=self.relationships().data)
        if self.backend == "postgres":
            rows = sanitize_result(self.executor.execute(safe_sql, params), self.sensitive_columns)
            return ToolResult(
                success=True,
                data=rows,
                source=self.datasource_id,
                metadata={"sql": safe_sql, "params": params or {}, "max_rows": MAX_ROWS, "read_only": True, "backend": "postgres", "data_origin": "synthetic_demo" if synthetic else "configured_datasource"},
            )
        limited = f"SELECT * FROM ({safe_sql.rstrip(';')}) AS guarded_query LIMIT {MAX_ROWS}"
        limited = self._sqlite_params(limited)
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(limited, params or {}).fetchall()]
        rows = sanitize_result(rows, self.sensitive_columns)
        return ToolResult(
            success=True,
            data=rows,
            source=self.datasource_id,
            metadata={"sql": safe_sql, "params": params or {}, "max_rows": MAX_ROWS, "read_only": True, "backend": "sqlite", "data_origin": "synthetic_demo" if synthetic else "configured_datasource"},
        )

