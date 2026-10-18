"""Internal-objects-only policy: queries may touch only Oracle-maintained objects.

QueryForge is for performance analysis through Oracle's data dictionary and
dynamic performance views (``V$``, ``DBA_HIST_``, ...), never application data.
Every table and user-callable function a statement references is resolved the
way Oracle would (explicit owner, else current schema, else PUBLIC synonym,
following synonym chains) and allowed only when the final owner is an
Oracle-maintained schema (``ALL_USERS.ORACLE_MAINTAINED = 'Y'``).

Deciding by owner rather than by name matters: a user can create a table called
``DBA_FAKE`` or a PUBLIC synonym onto their own table, and both must be refused.

Like :mod:`queryforge.sql_guard`, this is an application-level check. The strong
boundary is a login that owns no tables and holds only ``SELECT_CATALOG_ROLE``.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlglot import expressions as exp

from .sql_guard import SqlGuardError

# resolve(owner_or_None, name) -> final (owner, name) after synonyms, or None.
Resolver = Callable[[str | None, str], "tuple[str, str] | None"]
# is_internal(owner) -> True when the owner is an Oracle-maintained schema.
IsInternal = Callable[[str], bool]

_DENIED = (
    "Only Oracle data dictionary and performance views (V$, GV$, DBA_HIST_, DBA_/ALL_ "
    "views owned by SYS and other Oracle schemas) may be queried; {what} is not allowed."
)


def _ident(node: exp.Expression | None) -> str | None:
    """An identifier's name as Oracle stores it: uppercase unless quoted."""
    if node is None:
        return None
    if isinstance(node, exp.Identifier):
        return node.name if node.quoted else node.name.upper()
    return node.name.upper() if node.name else None


def _leftmost_name(node: exp.Expression) -> str | None:
    """First name of a dotted prefix such as ``sys.dbms_xplan`` → ``SYS``."""
    while isinstance(node, exp.Dot):
        node = node.this
    if isinstance(node, exp.Column):
        return _ident(node.parts[0]) if node.parts else None
    return _ident(node) if isinstance(node, exp.Identifier) else None


def referenced_objects(
    root: exp.Expression,
) -> tuple[list[tuple[str | None, str]], list[tuple[bool, str]]]:
    """Tables and functions a statement references.

    Returns ``(tables, functions)``: tables as ``(owner_or_None, name)`` with CTE
    names left out; functions sqlglot does not model (possible schema objects) as
    ``(qualified, name)``, where a qualified call reports its first name part
    (``dbms_xplan.display_cursor`` → ``(True, "DBMS_XPLAN")``).

    Raises:
        SqlGuardError: for database-link references, which cannot be checked.
    """
    ctes = {cte.alias.upper() for cte in root.find_all(exp.CTE) if cte.alias}
    tables: list[tuple[str | None, str]] = []
    for table in root.find_all(exp.Table):
        name = _ident(table.this) if isinstance(table.this, exp.Identifier) else None
        if name is None:  # TABLE(fn(...)) — the function is checked below
            continue
        if "@" in name or table.args.get("catalog"):
            raise SqlGuardError("Queries over a database link are not allowed.")
        owner = _ident(table.args.get("db"))
        if owner is None and name.upper() in ctes:
            continue
        if (owner, name) not in tables:
            tables.append((owner, name))

    functions: list[tuple[bool, str]] = []
    for func in root.find_all(exp.Anonymous):
        parent = func.parent
        if isinstance(parent, exp.Dot) and parent.expression is func:
            first = _leftmost_name(parent.this)
            ref = (True, first) if first else None
        else:
            ref = (False, func.name.upper())
        if ref and ref not in functions:
            functions.append(ref)
    return tables, functions


def check_access(root: exp.Expression, resolve: Resolver, is_internal: IsInternal) -> None:
    """Raise :class:`SqlGuardError` unless every referenced object is Oracle-maintained.

    Tables that cannot be resolved are refused (fail closed). An unqualified
    function that resolves to no schema object is a SQL built-in and is allowed.
    """
    tables, functions = referenced_objects(root)

    for owner, name in tables:
        shown = f"{owner}.{name}" if owner else name
        target = resolve(owner, name)
        if target is None:
            raise SqlGuardError(_DENIED.format(what=f"{shown} (not an Oracle-maintained object)"))
        if not is_internal(target[0]):
            raise SqlGuardError(_DENIED.format(what=f"{shown}, which belongs to schema {target[0]}"))

    for qualified, name in functions:
        target = resolve(None, name)
        if target is not None:
            owner = target[0]
        elif qualified:
            owner = name  # schema.function() or schema.package.function()
        else:
            continue  # built-in SQL function
        if not is_internal(owner):
            raise SqlGuardError(
                _DENIED.format(what=f"calling {name}, which belongs to schema {owner}")
            )
