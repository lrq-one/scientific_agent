import pytest

from app.tools.sql_guard import SQLGuard, SQLGuardError


def test_sql_read_only_guard_accepts_select_and_cte():
    guard = SQLGuard()
    assert guard.validate("SELECT COUNT(*) FROM training_molecules", {"training_molecules"})
    assert guard.validate("WITH x AS (SELECT * FROM training_molecules) SELECT COUNT(*) FROM x", {"training_molecules"})


@pytest.mark.parametrize("sql", [
    "DELETE FROM training_molecules",
    "UPDATE training_molecules SET structure_type='x'",
    "DROP TABLE training_molecules",
    "CREATE TABLE x(a int)",
])
def test_sql_read_only_guard_rejects_writes(sql: str):
    with pytest.raises(SQLGuardError):
        SQLGuard().validate(sql, {"training_molecules"})


def test_sql_multi_statement_rejection():
    with pytest.raises(SQLGuardError, match="one SQL statement"):
        SQLGuard().validate("SELECT 1; SELECT 2", {"training_molecules"})


def test_sql_table_allowlist():
    with pytest.raises(SQLGuardError, match="not authorized"):
        SQLGuard().validate("SELECT * FROM secrets", {"training_molecules"})


def test_parameter_binding_requires_exact_non_null_values():
    guard = SQLGuard()
    sql = "SELECT * FROM training_molecules WHERE dataset_version = %(version)s"
    guard.validate_bindings(sql, {"version": "train_v3"})
    with pytest.raises(SQLGuardError, match="missing"):
        guard.validate_bindings(sql, {})
    with pytest.raises(SQLGuardError, match="extra"):
        guard.validate_bindings(sql, {"version": "train_v3", "unrelated": 1})
    with pytest.raises(SQLGuardError, match="NULL"):
        guard.validate_bindings(sql, {"version": None})

