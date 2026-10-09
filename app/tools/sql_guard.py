from __future__ import annotations

import re
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope, Scope


SENSITIVE_FIELD = re.compile(r"(?:password|passwd|secret|token|api[_-]?key|credential|private[_-]?key|access[_-]?key)", re.I)


def sanitize_result(rows: list[dict], sensitive_columns: set[str] | None = None) -> list[dict]:
    if any(SENSITIVE_FIELD.search(str(key)) or key in (sensitive_columns or set()) for row in rows for key in row):
        raise SQLGuardError("sensitive result column is forbidden")
    return rows


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
        relationships: list[dict] | None = None,
        allowed_schemas: set[str] | None = None,
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
        if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            raise SQLGuardError("only SELECT or WITH ... SELECT is allowed")
        if any(tree.find(kind) is not None for kind in self.FORBIDDEN):
            raise SQLGuardError("write or DDL statements are forbidden")
        if tree.find(exp.Into) is not None or tree.find(exp.Lock) is not None:
            raise SQLGuardError("SELECT INTO and row locking are forbidden")
        forbidden_functions = {"pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_sleep", "set_config", "nextval", "setval", "dblink", "lo_import", "lo_export"}
        for function in tree.find_all(exp.Func):
            name = function.name.lower() if isinstance(function, exp.Anonymous) else function.sql_name().lower()
            if name in forbidden_functions or name.startswith("dblink"):
                raise SQLGuardError("unsafe SQL function is forbidden")
        for table in tree.find_all(exp.Table):
            if table.catalog or (table.db and table.db.lower() not in (allowed_schemas or {"public"})):
                raise SQLGuardError("schema not authorized")
            if table.name.lower() in {"pg_authid", "pg_shadow", "pg_roles", "pg_user", "pg_settings"}:
                raise SQLGuardError("system metadata is forbidden")
        if any(SENSITIVE_FIELD.search(column.name) for column in tree.find_all(exp.Column)):
            raise SQLGuardError("sensitive column is forbidden")
        ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        # Scope distinguishes physical tables from CTE references, even when a
        # CTE shadows a physical table name. Never authorize by name subtraction.
        scopes = list(traverse_scope(tree))
        tables = {source.name.lower() for scope in scopes for source in scope.sources.values()
                  if isinstance(source, exp.Table)}
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
            cte_references = ctes | {table.alias_or_name.lower() for table in tree.find_all(exp.Table)
                                     if table.name.lower() in ctes}
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

            derived = {scope.expression.parent.alias_or_name.lower() for scope in traverse_scope(tree)
                       if isinstance(scope.expression.parent, exp.Subquery)}
            for column in tree.find_all(exp.Column):
                name = column.name.lower()
                if name == "*":
                    continue
                qualifier = (column.table or "").lower()
                if qualifier:
                    if qualifier in cte_references or qualifier in derived:
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
            # Validate aliases within their SQL scope, including CTE/subquery outputs.
            for scope in traverse_scope(tree):
                for column in scope.columns:
                    if not column.table or column.name == "*":
                        continue
                    owner = scope
                    while owner is not None and column.table not in owner.sources:
                        owner = owner.parent
                    if owner is None:
                        raise SQLGuardError(f"unknown table alias: {column.table}")
                    source = owner.sources[column.table]
                    if isinstance(source, Scope):
                        outputs = source.expression.named_selects
                        if "*" not in outputs and column.name not in outputs:
                            raise SQLGuardError(f"column {column.table}.{column.name} does not exist in derived schema")
                if relationships is not None:
                    valid_pairs = {frozenset(((r["source_table"].lower(), r["source_column"].lower()),
                                             (r["target_table"].lower(), r["target_column"].lower()))) for r in relationships}
                    for join in scope.expression.find_all(exp.Join):
                        on = join.args.get("on")
                        for equality in on.find_all(exp.EQ) if on is not None else []:
                            left, right = equality.left, equality.right
                            if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                                continue
                            lsource, rsource = scope.sources.get(left.table), scope.sources.get(right.table)
                            if isinstance(lsource, exp.Table) and isinstance(rsource, exp.Table) and lsource.name != rsource.name:
                                pair = frozenset(((lsource.name.lower(), left.name.lower()), (rsource.name.lower(), right.name.lower())))
                                stable_key = left.name == right.name and left.name.lower() in {"molecule_id", "dataset_version_id", "model_version_id", "experiment_id"}
                                primary_key = any(c.get("primary_key") and c.get("name") == left.name
                                    for table in (lsource.name, rsource.name) for c in schema.get(table, []))
                                if pair not in valid_pairs and not (stable_key and primary_key):
                                    raise SQLGuardError("join relationship not supported by authorized schema")
        return sql.strip().rstrip(";")

