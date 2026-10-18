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


# Oracle packages a query may call, qualified as PKG.FN or SCHEMA.PKG.FN. Every
# other schema-level function or package is refused: some Oracle packages run SQL
# handed to them as text (DBMS_XMLGEN.GETXML('SELECT * FROM employee')), which
# would bypass the table check entirely.
ALLOWED_PACKAGES = frozenset({"DBMS_XPLAN"})

# Built-in SQL functions that can read arbitrary tables by path or query text.
_DENIED_BUILTINS = frozenset({"XMLQUERY", "XMLTABLE", "XMLEXISTS"})


def _dotted_path(node: exp.Expression) -> tuple[str, ...] | None:
    """Names of a dotted prefix (``sys.dbms_xplan`` → ``("SYS", "DBMS_XPLAN")``),
    or None if any part is not a plain name (e.g. a method on a call result)."""
    if isinstance(node, exp.Dot):
        left, right = _dotted_path(node.this), _dotted_path(node.expression)
        return left + right if left is not None and right is not None else None
    if isinstance(node, exp.Column):
        names = tuple(_ident(part) for part in node.parts)
        return names if all(names) else None  # type: ignore[return-value]
    if isinstance(node, exp.Identifier):
        return (_ident(node),)  # type: ignore[return-value]
    return None


def _cte_in_scope(table: exp.Table, name: str) -> bool:
    """True if a WITH clause enclosing ``table`` defines ``name``."""
    node = table.parent
    while node is not None:
        with_ = node.args.get("with_") or node.args.get("with")
        if isinstance(with_, exp.With) and any(
            cte.alias.upper() == name.upper() for cte in with_.expressions
        ):
            return True
        node = node.parent
    return False


def referenced_objects(
    root: exp.Expression,
) -> tuple[list[tuple[str | None, str]], list[tuple[str, ...]]]:
    """Tables and functions a statement references.

    Returns ``(tables, functions)``: tables as ``(owner_or_None, name)``, leaving
    out names that refer to a CTE in scope; functions sqlglot does not model
    (possible schema objects) as their full dotted path, e.g. ``("F",)`` or
    ``("SYS", "DBMS_XPLAN", "DISPLAY_CURSOR")``. A call whose prefix is not a
    plain dotted name is reported as ``("?", name)``.

    Raises:
        SqlGuardError: for database-link references and table sources other than
            a named object or ``TABLE(fn(...))``, which cannot be checked.
    """
    tables: list[tuple[str | None, str]] = []
    for table in root.find_all(exp.Table):
        if isinstance(table.this, exp.Anonymous):  # TABLE(fn(...)) — fn checked below
            continue
        if not isinstance(table.this, exp.Identifier):
            raise SqlGuardError(_DENIED.format(what=f"the row source {table.this.key.upper()}"))
        name = _ident(table.this)
        if "@" in name or table.args.get("catalog"):
            raise SqlGuardError("Queries over a database link are not allowed.")
        owner = _ident(table.args.get("db"))
        if owner is None and _cte_in_scope(table, name):
            continue
        if (owner, name) not in tables:
            tables.append((owner, name))

    functions: list[tuple[str, ...]] = []
    for func in root.find_all(exp.Anonymous):
        name = func.name.upper()
        parent = func.parent
        if isinstance(parent, exp.Dot) and parent.expression is func:
            prefix = _dotted_path(parent.this)
            path = (*prefix, name) if prefix is not None else ("?", name)
        else:
            path = (name,)
        if path not in functions:
            functions.append(path)
    return tables, functions


def check_access(root: exp.Expression, resolve: Resolver, is_internal: IsInternal) -> None:
    """Raise :class:`SqlGuardError` unless every referenced object is Oracle-maintained.

    Tables that cannot be resolved are refused (fail closed). An unqualified
    function that resolves to no schema object is a SQL built-in and is allowed;
    one that resolves to a schema object is refused. Qualified calls are allowed
    only into :data:`ALLOWED_PACKAGES`, and only when the package is Oracle's.
    """
    tables, functions = referenced_objects(root)

    for owner, name in tables:
        shown = f"{owner}.{name}" if owner else name
        target = resolve(owner, name)
        if target is None:
            raise SqlGuardError(_DENIED.format(what=f"{shown} (not an Oracle-maintained object)"))
        if not is_internal(target[0]):
            raise SqlGuardError(_DENIED.format(what=f"{shown}, which belongs to schema {target[0]}"))

    for path in functions:
        shown = ".".join(path)
        if len(path) == 1:
            if path[0] in _DENIED_BUILTINS or resolve(None, path[0]) is not None:
                raise SqlGuardError(_DENIED.format(what=f"calling {shown}()"))
            continue
        if len(path) in (2, 3) and path[-2] in ALLOWED_PACKAGES:
            target = resolve(path[0] if len(path) == 3 else None, path[-2])
            if target is not None and is_internal(target[0]):
                continue
        raise SqlGuardError(
            _DENIED.format(what=f"calling {shown}() (only {', '.join(sorted(ALLOWED_PACKAGES))} may be used)")
        )
