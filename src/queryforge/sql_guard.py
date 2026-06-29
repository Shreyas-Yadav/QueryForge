"""Read-only SQL guard — defense-in-depth, NOT the security boundary.

The real boundary is a read-only Oracle user (GRANT SELECT only). This guard is
a second layer that catches mistakes and obvious bypasses before a query reaches
the database, and enforces a hard row cap.

It uses sqlglot's Oracle dialect to parse the statement and rejects anything that
is not a single, read-only ``SELECT`` (optionally a ``WITH`` / set-operation of
selects). DDL, DML, PL/SQL, multi-statement payloads, and ``SELECT ... INTO`` are
all rejected.
"""

from __future__ import annotations

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import ParseError, TokenError

_DIALECT = "oracle"

# Root expression types that represent a read-only query.
_ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = tuple(
    t
    for t in (
        getattr(exp, name, None)
        for name in ("Select", "Union", "Intersect", "Except", "Subquery", "With", "Paren")
    )
    if t is not None
)

# Any of these appearing ANYWHERE in the tree means the statement is not a pure
# read-only select. ``Command`` catches statements sqlglot can't model (GRANT,
# TRUNCATE, anonymous PL/SQL blocks, etc.) — a useful catch-all.
_DISALLOWED_NODES: tuple[type[exp.Expression], ...] = tuple(
    t
    for t in (
        getattr(exp, name, None)
        for name in (
            "Insert",
            "Update",
            "Delete",
            "Merge",
            "Create",
            "Drop",
            "Alter",
            "TruncateTable",
            "Command",
            "Into",  # SELECT ... INTO writes rows
            "Set",  # SET / session changes
            "Use",
        )
    )
    if t is not None
)


class SqlGuardError(ValueError):
    """Raised when a statement fails read-only validation."""


def validate_select(sql: str) -> exp.Expression:
    """Validate that ``sql`` is exactly one read-only SELECT and return its AST.

    Raises:
        SqlGuardError: if the input is empty, multi-statement, unparseable, or
            contains any non-read-only construct.
    """
    if not sql or not sql.strip():
        raise SqlGuardError("Empty query.")

    try:
        statements = [s for s in sqlglot.parse(sql, dialect=_DIALECT) if s is not None]
    except (ParseError, TokenError) as e:
        raise SqlGuardError(f"Could not parse SQL: {e}") from e

    if len(statements) == 0:
        raise SqlGuardError("No statement found.")
    if len(statements) > 1:
        raise SqlGuardError("Only a single statement is allowed.")

    root = statements[0]

    if not isinstance(root, _ALLOWED_ROOTS):
        raise SqlGuardError(
            f"Only read-only SELECT queries are allowed (got {root.key.upper()})."
        )

    for node in root.walk():
        if isinstance(node, _DISALLOWED_NODES):
            raise SqlGuardError(
                f"Disallowed operation in query: {node.key.upper()}. Only SELECT is permitted."
            )

    return root


def apply_row_cap(sql: str, max_rows: int) -> str:
    """Wrap a validated SELECT so it returns at most ``max_rows`` rows.

    The integer is coerced with ``int()`` so it can never carry an injection.
    """
    capped = int(max_rows)
    inner = sql.strip().rstrip(";").strip()
    return f"SELECT * FROM (\n{inner}\n) FETCH FIRST {capped} ROWS ONLY"


def guard(sql: str, max_rows: int) -> str:
    """Validate ``sql`` as read-only and return executable, row-capped SQL.

    Raises:
        SqlGuardError: if validation fails.
    """
    validate_select(sql)
    return apply_row_cap(sql, max_rows)
