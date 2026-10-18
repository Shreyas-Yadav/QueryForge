"""Tests for the internal-objects-only policy (Oracle-maintained owners only)."""

from __future__ import annotations

import pytest

from queryforge.object_policy import check_access, referenced_objects
from queryforge.sql_guard import SqlGuardError, validate_select

# A fake dictionary: (owner, name) -> final (owner, name) after following synonyms.
# Unqualified names resolve in current schema SHREYAS first, then PUBLIC.
OBJECTS = {
    ("SHREYAS", "EMPLOYEE"): ("SHREYAS", "EMPLOYEE"),
    ("SHREYAS", "F"): ("SHREYAS", "F"),
    ("SHREYAS", "PIPE"): ("SHREYAS", "PIPE"),
    ("SHREYAS", "DBA_FAKE"): ("SHREYAS", "DBA_FAKE"),
    ("PUBLIC", "V$SQLAREA"): ("SYS", "V_$SQLAREA"),
    ("PUBLIC", "DUAL"): ("SYS", "DUAL"),
    ("PUBLIC", "DBMS_XPLAN"): ("SYS", "DBMS_XPLAN"),
    ("PUBLIC", "DBMS_XMLGEN"): ("SYS", "DBMS_XMLGEN"),
    ("SYS", "DBMS_XPLAN"): ("SYS", "DBMS_XPLAN"),
    ("SYS", "DBMS_XMLGEN"): ("SYS", "DBMS_XMLGEN"),
    ("PUBLIC", "DBA_USERS"): ("SYS", "DBA_USERS"),
    ("PUBLIC", "EMP_PUB"): ("SHREYAS", "EMPLOYEE"),  # public synonym onto a user table
    ("SYS", "V_$SQLAREA"): ("SYS", "V_$SQLAREA"),
}
INTERNAL = {"SYS", "SYSTEM"}


def resolve(owner, name):
    if owner is not None:
        return OBJECTS.get((owner, name))
    return OBJECTS.get(("SHREYAS", name)) or OBJECTS.get(("PUBLIC", name))


def check(sql):
    check_access(validate_select(sql), resolve, INTERNAL.__contains__)


# --- reference extraction -------------------------------------------------------

def test_referenced_tables_uppercase_unquoted_and_keep_quoted_case():
    tables, _ = referenced_objects(validate_select('SELECT * FROM shreyas.employee, "Mixed"'))
    assert ("SHREYAS", "EMPLOYEE") in tables
    assert (None, "Mixed") in tables


def test_cte_names_are_not_tables():
    tables, _ = referenced_objects(validate_select("WITH e AS (SELECT 1 x FROM dual) SELECT * FROM e"))
    assert tables == [(None, "DUAL")]


def test_scalar_subquery_tables_are_found():
    tables, _ = referenced_objects(validate_select("SELECT (SELECT COUNT(*) FROM employee) FROM dual"))
    assert (None, "EMPLOYEE") in tables


def test_function_calls_report_their_full_dotted_path():
    _, funcs = referenced_objects(
        validate_select("SELECT sys.dbms_xplan.display_cursor('x'), nvl(1, 2), f(1) FROM dual")
    )
    assert ("SYS", "DBMS_XPLAN", "DISPLAY_CURSOR") in funcs
    assert ("F",) in funcs


def test_cte_name_only_shadows_inside_its_own_query():
    tables, _ = referenced_objects(validate_select(
        "SELECT * FROM employee WHERE EXISTS "
        "(WITH employee AS (SELECT 1 x FROM dual) SELECT 1 FROM employee)"
    ))
    assert (None, "EMPLOYEE") in tables


def test_db_link_is_rejected():
    with pytest.raises(SqlGuardError, match="database link"):
        referenced_objects(validate_select("SELECT * FROM emp@remote"))


# --- scenario (b): querying a user table is refused --------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "select * from employee",
        "SELECT * FROM shreyas.employee",
        "SELECT * FROM v$sqlarea WHERE sql_id IN (SELECT 'x' FROM employee)",
        "SELECT * FROM emp_pub",  # public synonym pointing at a user table
        "SELECT * FROM dba_fake",  # user table with a dictionary-looking name
        "SELECT shreyas.f() FROM dual",  # user function could read user tables
        "SELECT f(1) FROM dual",
        "SELECT * FROM TABLE(shreyas.pipe())",
        "SELECT * FROM no_such_table",  # unresolvable → fail closed
        # a CTE in a subquery must not hide the outer user table
        (
            "SELECT * FROM employee WHERE EXISTS "
            "(WITH employee AS (SELECT 1 x FROM dual) SELECT 1 FROM employee)"
        ),
        "SELECT 1 FROM employee UNION ALL (WITH employee AS (SELECT 1 x FROM dual) SELECT x FROM employee)",
        # Oracle packages that run SQL handed to them as text
        "SELECT DBMS_XMLGEN.GETXML('SELECT * FROM employee') FROM dual",
        "SELECT sys.dbms_xmlgen.getxml('SELECT * FROM employee') FROM dual",
        "SELECT * FROM XMLTABLE('x' COLUMNS a NUMBER PATH 'a')",
        "SELECT dbms_xplan.display_cursor('x') FROM dual WHERE f(1) = 1",
    ],
)
def test_user_objects_are_rejected(sql):
    with pytest.raises(SqlGuardError, match="Oracle"):
        check(sql)


def test_rejection_names_the_object():
    with pytest.raises(SqlGuardError, match="EMPLOYEE"):
        check("select * from employee")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT sql_id, elapsed_time FROM v$sqlarea ORDER BY elapsed_time DESC FETCH FIRST 10 ROWS ONLY",
        "SELECT * FROM sys.v_$sqlarea",
        "SELECT * FROM TABLE(dbms_xplan.display_cursor('abc'))",
        "SELECT * FROM TABLE(sys.dbms_xplan.display_cursor('abc'))",
        "SELECT NVL(1, 2), UPPER('a'), SYS_CONTEXT('USERENV', 'DB_NAME') FROM dual",
        "WITH t AS (SELECT sql_id FROM v$sqlarea) SELECT * FROM t",
        "SELECT u.username FROM v$sqlarea s JOIN dba_users u ON u.user_id = s.parsing_user_id",
    ],
)
def test_internal_objects_are_allowed(sql):
    check(sql)
