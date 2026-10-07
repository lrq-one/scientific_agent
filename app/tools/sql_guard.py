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

    def validate(self, sql: str, allowed_tables: set[str], dialect: str = "sqlite") -> str:
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
        return sql.strip().rstrip(";")

