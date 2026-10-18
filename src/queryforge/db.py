"""Oracle connectivity (python-oracledb, thin mode) and read-only query helpers.

Thin mode needs no Oracle Instant Client — the same pure-Python driver connects
from Windows, Linux and macOS alike, so nothing here is OS-specific.

There is no pool until the user logs in. :func:`replace_pool` connects with the
profile built from their login, pings it, and only then makes it the active
connection, so a failed login never disturbs a working one. Until then every
database call raises :class:`NotLoggedIn`.

Wallet (mTLS) parameters on the profile are still honoured, so cloud databases
can be added back to ``databases.json`` later without touching this module.

Every public function here is read-only. ``run_select`` additionally routes SQL
through :mod:`queryforge.sql_guard` and enforces a row cap + per-query timeout.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import logging
import threading
from typing import Any

import oracledb

from .config import OracleProfile, get_settings
from .object_policy import check_access
from .sql_guard import apply_row_cap, validate_select

logger = logging.getLogger(__name__)

class NotLoggedIn(RuntimeError):
    """No database login is active — log in from the web UI first."""


_pool: oracledb.ConnectionPool | None = None
_profile: OracleProfile | None = None
_pool_lock = threading.Lock()


def get_pool() -> oracledb.ConnectionPool:
    """Return the active login's connection pool."""
    pool = _pool
    if pool is None:
        raise NotLoggedIn("Not connected — log in to a database first.")
    return pool


def active_profile() -> OracleProfile:
    """Return the profile of the active login."""
    profile = _profile
    if profile is None:
        raise NotLoggedIn("Not connected — log in to a database first.")
    return profile


def _pool_kwargs(profile: OracleProfile) -> dict[str, Any]:
    """Build ``create_pool`` arguments for one connection profile."""
    kwargs: dict[str, Any] = dict(
        user=profile.user,
        password=profile.password,
        dsn=profile.dsn,
        min=1,
        max=4,
        increment=1,
        homogeneous=True,
    )
    if profile.uses_wallet:
        # mTLS: PEM wallet (thin mode). Plain host/port/service entries have no
        # wallet, so they connect with the DSN alone.
        kwargs.update(
            config_dir=profile.config_dir,
            wallet_location=profile.wallet_location,
            wallet_password=profile.wallet_password,
        )
    return kwargs


def _create_pool(profile: OracleProfile) -> oracledb.ConnectionPool:
    return oracledb.create_pool(**_pool_kwargs(profile))


def _connect(profile: OracleProfile) -> oracledb.Connection:
    """One standalone connection — exactly one login attempt."""
    kwargs = _pool_kwargs(profile)
    for pool_only in ("min", "max", "increment", "homogeneous"):
        kwargs.pop(pool_only)
    return oracledb.connect(**kwargs)


def _close(pool: oracledb.ConnectionPool) -> None:
    """Close a pool, logging rather than raising.

    ``force=True`` because a plain close raises if any connection is still
    checked out — which is exactly the case when the user logs out or switches
    login while a query is streaming. The deliberate action should win.
    """
    try:
        pool.close(force=True)
    except Exception as e:  # noqa: BLE001 — never block a login change
        logger.warning("Error closing connection pool: %s", e)


def replace_pool(profile: OracleProfile) -> None:
    """Connect as ``profile`` and make it the active login.

    The credentials are checked with a single standalone connection before any
    pool exists, so a bad password or an unreachable listener raises here and
    leaves the current login exactly as it was. A pool is not used for that
    check because it tries a bad password twice, and every try counts toward
    the account's FAILED_LOGIN_ATTEMPTS lockout (verified on 19c). Once the
    password is proven, Oracle has reset that counter, so the pool's own
    connections are harmless.
    """
    global _pool, _profile
    logger.info(
        "Connecting to Oracle [%s] user=%s dsn=%s", profile.target, profile.user, profile.dsn
    )
    with _connect(profile) as conn:
        conn.ping()
    new_pool = _create_pool(profile)
    with _pool_lock:
        old, _pool, _profile = _pool, new_pool, profile
    if old is not None:
        _close(old)


def close_pool() -> None:
    """Log out: close the active pool (if any) and forget its profile."""
    global _pool, _profile
    with _pool_lock:
        old, _pool, _profile = _pool, None, None
    if old is not None:
        _close(old)


def _schema_name() -> str:
    """The schema the agent reads — the entry's schema if set, else the user."""
    profile = active_profile()
    return (profile.schema_name or profile.user).upper()


def _acquire():  # type: ignore[no-untyped-def]
    """Acquire a pooled connection with the configured timeout + target schema.

    Setting ``current_schema`` lets the agent write unqualified table names that
    resolve to the read schema even when connecting as a read-only user whose
    granted tables live in another schema.
    """
    cfg = get_settings()
    conn = get_pool().acquire()
    conn.call_timeout = cfg.query_timeout_s * 1000  # milliseconds
    conn.current_schema = _schema_name()
    return conn


def ping() -> None:
    """Open and close a connection to verify connectivity. Raises on failure."""
    with _acquire() as conn:
        conn.ping()


# --- JSON-safe value coercion -------------------------------------------------

def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        # Preserve integers as int, others as float for clean JSON.
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, oracledb.LOB):
        data = value.read()
        return data if isinstance(data, str) else base64.b64encode(data).decode("ascii")
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return str(value)


# --- Schema introspection (read-only) ----------------------------------------

def describe_table(name: str) -> dict[str, Any]:
    """Return columns (with comments), primary key, and foreign keys for one
    Oracle-maintained table or view.

    ``name`` may be ``NAME`` or ``OWNER.NAME``. It is resolved like a query would
    resolve it (current schema, then PUBLIC synonym, following synonyms); when a
    synonym was followed, ``resolved_to`` names the underlying ``OWNER.NAME``.

    Raises:
        ValueError: if the object does not exist or is not owned by an
            Oracle-maintained schema (application tables are off-limits).
    """
    owner_part, _, name_part = name.strip().upper().rpartition(".")
    schema = _schema_name()

    columns_sql = """
        SELECT col.column_name, col.data_type, col.data_length,
               col.data_precision, col.data_scale, col.nullable, com.comments
        FROM all_tab_columns col
        LEFT JOIN all_col_comments com
               ON com.owner = col.owner
              AND com.table_name = col.table_name
              AND com.column_name = col.column_name
        WHERE col.owner = :owner AND col.table_name = :tname
        ORDER BY col.column_id
    """
    pk_sql = """
        SELECT cc.column_name
        FROM all_constraints c
        JOIN all_cons_columns cc
               ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
        WHERE c.owner = :owner AND c.table_name = :tname AND c.constraint_type = 'P'
        ORDER BY cc.position
    """
    fk_sql = """
        SELECT cc.column_name, rc.table_name AS ref_table, rcc.column_name AS ref_column
        FROM all_constraints c
        JOIN all_cons_columns cc
               ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
        JOIN all_constraints rc
               ON rc.owner = c.r_owner AND rc.constraint_name = c.r_constraint_name
        JOIN all_cons_columns rcc
               ON rcc.owner = rc.owner
              AND rcc.constraint_name = rc.constraint_name
              AND rcc.position = cc.position
        WHERE c.owner = :owner AND c.table_name = :tname AND c.constraint_type = 'R'
        ORDER BY cc.position
    """

    with _acquire() as conn, conn.cursor() as cur:
        target = _resolve_object(cur, schema, owner_part or None, name_part)
        if target is None:
            raise ValueError(f"Table not found: {name}")
        owner, table = target
        if not _is_oracle_maintained(cur, owner):
            raise ValueError(
                f"Access denied: {name.strip().upper()} belongs to schema {owner}. Only Oracle "
                "data dictionary and performance views can be described."
            )

        cur.execute(columns_sql, owner=owner, tname=table)
        columns = [
            {
                "name": cname,
                "type": _format_type(dtype, dlen, dprec, dscale),
                "nullable": nullable == "Y",
                "comment": comment,
            }
            for cname, dtype, dlen, dprec, dscale, nullable, comment in cur
        ]
        if not columns:
            raise ValueError(f"Table not found: {name}")

        cur.execute(pk_sql, owner=owner, tname=table)
        primary_key = [row[0] for row in cur]

        cur.execute(fk_sql, owner=owner, tname=table)
        foreign_keys = [
            {"column": col, "references": f"{ref_table}.{ref_col}"}
            for col, ref_table, ref_col in cur
        ]

    result: dict[str, Any] = {
        "table_name": name.strip().upper(),
        "columns": columns,
        "primary_key": primary_key,
        "foreign_keys": foreign_keys,
    }
    resolved = f"{owner}.{table}"
    if resolved != result["table_name"]:
        result["resolved_to"] = resolved
    return result


# --- Object resolution (internal-objects-only policy) ------------------------

_MAX_SYNONYM_HOPS = 8

_SYNONYM_SQL = """
    SELECT table_owner, table_name, db_link FROM all_synonyms
    WHERE owner = :owner AND synonym_name = :name
"""
_OBJECT_SQL = """
    SELECT 1 FROM all_objects
    WHERE owner = :owner AND object_name = :name
      AND object_type NOT IN ('PACKAGE BODY', 'TYPE BODY', 'SYNONYM')
    FETCH FIRST 1 ROW ONLY
"""
_ORACLE_MAINTAINED_SQL = """
    SELECT 1 FROM all_users WHERE username = :owner AND oracle_maintained = 'Y'
"""


def _resolve_object(cur, schema: str, owner: str | None, name: str) -> tuple[str, str] | None:
    """Resolve a name the way Oracle does and return the final ``(owner, name)``.

    An unqualified name is looked up in ``schema`` (the session's current schema)
    first, then as a PUBLIC synonym. Synonym chains are followed. Returns None
    when nothing matches, or when a synonym points over a database link.
    """
    candidates = [owner] if owner else [schema, "PUBLIC"]
    for start in candidates:
        cur_owner, cur_name = start, name
        followed = False
        for _ in range(_MAX_SYNONYM_HOPS):
            cur.execute(_SYNONYM_SQL, owner=cur_owner, name=cur_name)
            syn = cur.fetchone()
            if syn is None:
                break
            if syn[2] is not None:  # remote object: cannot be checked
                return None
            cur_owner, cur_name, followed = syn[0], syn[1], True
        else:
            return None  # synonym loop
        if followed:
            # The target's owner is known from the synonym even when the object
            # itself is not visible to this user (the query would fail anyway).
            return cur_owner, cur_name
        cur.execute(_OBJECT_SQL, owner=cur_owner, name=cur_name)
        if cur.fetchone() is not None:
            return cur_owner, cur_name
    return None


def _is_oracle_maintained(cur, owner: str) -> bool:
    cur.execute(_ORACLE_MAINTAINED_SQL, owner=owner)
    return cur.fetchone() is not None


def _check_internal_only(cur, root) -> None:  # type: ignore[no-untyped-def]
    """Apply :func:`queryforge.object_policy.check_access` against the live dictionary."""
    schema = _schema_name()
    resolved: dict[tuple[str | None, str], tuple[str, str] | None] = {}
    internal: dict[str, bool] = {}

    def resolve(owner: str | None, name: str) -> tuple[str, str] | None:
        if (owner, name) not in resolved:
            resolved[(owner, name)] = _resolve_object(cur, schema, owner, name)
        return resolved[(owner, name)]

    def is_internal(owner: str) -> bool:
        if owner not in internal:
            internal[owner] = _is_oracle_maintained(cur, owner)
        return internal[owner]

    check_access(root, resolve, is_internal)


def _format_type(
    data_type: str,
    length: int | None,
    precision: int | None,
    scale: int | None,
) -> str:
    dt = data_type.upper()
    if dt in ("NUMBER",) and precision:
        return f"NUMBER({precision},{scale or 0})" if scale else f"NUMBER({precision})"
    if dt in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "RAW") and length:
        return f"{dt}({length})"
    return dt


# --- Read-only query execution -----------------------------------------------

def run_select(sql: str, max_rows: int | None = None) -> dict[str, Any]:
    """Validate, row-cap, and execute a read-only SELECT.

    Returns a dict with ``columns``, ``rows`` (JSON-safe), ``row_count``,
    ``truncated`` (True if more rows existed than the cap), and the executed
    ``sql``. Raises :class:`~queryforge.sql_guard.SqlGuardError` for unsafe SQL or
    SQL touching non-Oracle-maintained objects, and ``oracledb`` errors for execution failures (caller decides how to surface).
    """
    cfg = get_settings()
    cap = cfg.max_rows if max_rows is None else max_rows

    root = validate_select(sql)
    # Fetch one extra row so we can tell the result was truncated.
    executable = apply_row_cap(sql, cap + 1)

    with _acquire() as conn, conn.cursor() as cur:
        _check_internal_only(cur, root)
        cur.execute(executable)
        columns = [d.name for d in cur.description]
        fetched = cur.fetchall()

    truncated = len(fetched) > cap
    fetched = fetched[:cap]
    rows = [[_to_jsonable(v) for v in row] for row in fetched]

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "sql": executable,
    }
