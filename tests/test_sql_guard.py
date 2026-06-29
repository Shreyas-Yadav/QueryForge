"""Unit tests for the read-only SQL guard, including adversarial cases."""

from __future__ import annotations

import pytest

from queryforge.sql_guard import SqlGuardError, apply_row_cap, guard, validate_select


# --- statements that must be accepted ---

VALID = [
    "SELECT * FROM employees",
    "select id, name from customers where region = 'EMEA'",
    "SELECT department_id, COUNT(*) FROM employees GROUP BY department_id",
    "WITH dept AS (SELECT * FROM departments) SELECT * FROM dept",
    "SELECT id FROM a UNION SELECT id FROM b",
    "SELECT * FROM orders ORDER BY created_at DESC",
    # An inert comment that merely *looks* dangerous is still a single SELECT.
    "SELECT 1 FROM dual /* ; DROP TABLE t */",
]


@pytest.mark.parametrize("sql", VALID)
def test_valid_selects_pass(sql: str) -> None:
    validate_select(sql)  # should not raise


# --- statements that must be rejected ---

INVALID = [
    "",
    "   ",
    "INSERT INTO t (a) VALUES (1)",
    "UPDATE employees SET salary = 0",
    "DELETE FROM employees",
    "DROP TABLE employees",
    "CREATE TABLE x (id NUMBER)",
    "ALTER TABLE employees ADD (x NUMBER)",
    "TRUNCATE TABLE employees",
    "GRANT SELECT ON employees TO public",
    "MERGE INTO t USING s ON (t.id = s.id) WHEN MATCHED THEN UPDATE SET t.a = s.a",
    # multi-statement injection
    "SELECT 1 FROM dual; DROP TABLE employees",
    "SELECT 1 FROM dual; DELETE FROM employees --",
    # PL/SQL anonymous block
    "BEGIN DELETE FROM employees; END;",
    # SELECT ... INTO writes rows
    "SELECT * INTO backup FROM employees",
]


@pytest.mark.parametrize("sql", INVALID)
def test_invalid_statements_rejected(sql: str) -> None:
    with pytest.raises(SqlGuardError):
        validate_select(sql)


# --- row cap ---

def test_row_cap_wraps_with_fetch_first() -> None:
    out = apply_row_cap("SELECT * FROM employees", 50)
    assert "FETCH FIRST 50 ROWS ONLY" in out
    assert "SELECT * FROM employees" in out


def test_row_cap_strips_trailing_semicolon() -> None:
    out = apply_row_cap("SELECT 1 FROM dual;", 10)
    assert ");" not in out  # the trailing ';' must not leak into the wrapper
    assert "FETCH FIRST 10 ROWS ONLY" in out


def test_guard_validates_then_caps() -> None:
    out = guard("SELECT id FROM customers", 25)
    assert "FETCH FIRST 25 ROWS ONLY" in out


def test_guard_rejects_dml_before_capping() -> None:
    with pytest.raises(SqlGuardError):
        guard("DELETE FROM customers", 25)
