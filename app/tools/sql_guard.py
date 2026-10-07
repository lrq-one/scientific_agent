from __future__ import annotations

import re
import sqlglot
from sqlglot import exp


class SQLGuardError(ValueError):
    pass


class SQLGuard:
    FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Alter, exp.Create, exp.Command)

    @staticmethod
    def validate_bindings(sql: str, params: dict | None) -> None:
        expected = set(re.findall(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s", sql))
        supplied = set((params or {}).keys())
        if expected != supplied:
            raise SQLGuardError(
                f"SQL parameter mismatch: missing={sorted(expected - supplied)}, extra={sorted(supplied - expected)}"
            )
        if any((params or {}).get(name) is None for name in expected):
            raise SQLGuardError("SQL parameter value cannot be NULL")

    def validate(
        self,
        sql: str,
        allowed_tables: set[str],
        dialect: str = "sqlite",
        schema: dict[str, list[dict]] | None = None,
    ) -> str:
        # Named DB-API placeholders are values, never identifiers. Replace only for AST parsing.
        parse_sql = re.sub(r"%\([A-Za-z_][A-Za-z0-9_]*\)s", "'__bound_param__'", sql)
        try:
            statements = sqlglot.parse(parse_sql, read=dialect)
        except sqlglot.errors.ParseError as exc:
            raise SQLGuardError(f"invalid SQL: {exc}") from exc
        if len(statements) != 1:
            raise SQLGuardError("only one SQL statement is allowed")
        tree = statements[0]
        if not isinstance(tree, (exp.Select, exp.Union)) and tree.find(exp.Select) is None:
            raise SQLGuardError("only SELECT or WITH ... SELECT is allowed")
        if any(tree.find(kind) is not None for kind in self.FORBIDDEN):
            raise SQLGuardError("write or DDL statements are forbidden")
        ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        tables = {table.name.lower() for table in tree.find_all(exp.Table)} - ctes
        disallowed = tables - {name.lower() for name in allowed_tables}
        if disallowed:
            raise SQLGuardError(f"table not authorized: {sorted(disallowed)}")

        if schema:
            normalized_schema = {
                str(table).lower(): {
                    str(column.get("name", "")).lower()
                    for column in columns
                    if isinstance(column, dict) and column.get("name")
                }
                for table, columns in schema.items()
            }
            alias_to_table: dict[str, str] = {}
            for table in tree.find_all(exp.Table):
                actual = table.name.lower()
                alias = (table.alias_or_name or table.name).lower()
                if actual not in ctes:
                    alias_to_table[alias] = actual
                    alias_to_table.setdefault(actual, actual)

            projection_aliases = {
                expression.alias.lower()
                for select in tree.find_all(exp.Select)
                for expression in select.expressions
                if expression.alias
            }
            known_columns = set().union(*normalized_schema.values()) if normalized_schema else set()

            for column in tree.find_all(exp.Column):
                name = column.name.lower()
                if name == "*":
                    continue
                qualifier = (column.table or "").lower()
                if qualifier:
                    if qualifier in ctes:
                        continue
                    actual_table = alias_to_table.get(qualifier)
                    if actual_table is None:
                        raise SQLGuardError(f"unknown table alias: {qualifier}")
                    allowed_columns = normalized_schema.get(actual_table, set())
                    if allowed_columns and name not in allowed_columns:
                        raise SQLGuardError(
                            f"column {qualifier}.{name} does not exist in authorized schema"
                        )
                else:
                    if name in projection_aliases:
                        continue
                    if known_columns and name not in known_columns:
                        raise SQLGuardError(
                            f"column {name} does not exist in authorized schema"
                        )
        return sql.strip().rstrip(";")

