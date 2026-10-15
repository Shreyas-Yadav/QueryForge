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

    The new pool is built and pinged before anything is swapped, so a bad
    password or an unreachable listener raises here and leaves the current login
    exactly as it was.
    """
    global _pool, _profile
    logger.info(
        "Connecting to Oracle [%s] user=%s dsn=%s", profile.target, profile.user, profile.dsn
    )
    new_pool = _create_pool(profile)
    try:
        with new_pool.acquire() as conn:
            conn.ping()
    except BaseException:
        _close(new_pool)
        raise
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

def list_tables() -> list[dict[str, Any]]:
    """List queryable objects in the target schema: base tables plus synonyms.

    Each item carries ``table_name`` (the name to query), ``comments``, and an
    ``object_type`` of ``"TABLE"`` or ``"SYNONYM"``; synonyms also carry
    ``points_to`` (``"OWNER.NAME"`` of the underlying object).

    Synonyms include the target schema's private synonyms plus business PUBLIC
    synonyms. Oracle-maintained system PUBLIC synonyms, DB-link synonyms, and
    synonyms whose target no longer exists are filtered out.
    """
    tables_sql = """
        SELECT t.table_name, c.comments
        FROM all_tables t
        LEFT JOIN all_tab_comments c
               ON c.owner = t.owner AND c.table_name = t.table_name
        WHERE t.owner = :owner
        ORDER BY t.table_name
    """
    synonyms_sql = """
        SELECT s.synonym_name, s.table_owner, s.table_name, c.comments
        FROM all_synonyms s
        LEFT JOIN all_users u
               ON u.username = s.table_owner
        LEFT JOIN all_tab_comments c
               ON c.owner = s.table_owner AND c.table_name = s.table_name
        WHERE s.db_link IS NULL
          AND ( s.owner = :owner
                OR (s.owner = 'PUBLIC' AND NVL(u.oracle_maintained, 'N') = 'N') )
          AND EXISTS (
                SELECT 1 FROM all_tables t
                 WHERE t.owner = s.table_owner AND t.table_name = s.table_name
                UNION ALL
                SELECT 1 FROM all_views v
                 WHERE v.owner = s.table_owner AND v.view_name = s.table_name )
        ORDER BY s.synonym_name
    """
    owner = _schema_name()
    with _acquire() as conn, conn.cursor() as cur:
        cur.execute(tables_sql, owner=owner)
        tables = [{"table_name": name, "comments": comments} for name, comments in cur]

        cur.execute(synonyms_sql, owner=owner)
        synonyms = [
            {
                "synonym_name": syn_name,
                "table_owner": tab_owner,
                "table_name": tab_name,
                "comments": comments,
            }
            for syn_name, tab_owner, tab_name, comments in cur
        ]

    return _merge_listing(tables, synonyms)


def _merge_listing(
    tables: list[dict[str, Any]], synonyms: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Combine base tables and synonyms into one tagged listing (pure helper).

    Tables come first (tagged ``object_type="TABLE"``), then synonyms tagged
    ``"SYNONYM"`` with a ``points_to`` target. A synonym whose name collides with
    a base table is dropped — the real table shadows it at query-resolution time.
    """
    table_names = {t["table_name"] for t in tables}
    merged: list[dict[str, Any]] = [
        {"table_name": t["table_name"], "comments": t["comments"], "object_type": "TABLE"}
        for t in tables
    ]
    for s in synonyms:
        if s["synonym_name"] in table_names:
            continue
        merged.append(
            {
                "table_name": s["synonym_name"],
                "comments": s["comments"],
                "object_type": "SYNONYM",
                "points_to": f"{s['table_owner']}.{s['table_name']}",
            }
        )
    return merged


def describe_table(name: str) -> dict[str, Any]:
    """Return columns (with comments), primary key, and foreign keys for a table.

    Resolves synonyms: if ``name`` is not a table/view in the target schema, it is
    looked up in ``ALL_SYNONYMS`` (private preferred over PUBLIC) and the
    underlying object is introspected instead. The returned ``table_name`` stays
    the name the caller passed (queryable via ``current_schema``); when a synonym
    was followed, ``resolved_to`` names the underlying ``OWNER.NAME``.
    """
    table = name.strip().upper()
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

    synonym_sql = """
        SELECT table_owner, table_name FROM all_synonyms
        WHERE synonym_name = :name AND db_link IS NULL AND owner IN (:owner, 'PUBLIC')
        ORDER BY CASE WHEN owner = :owner THEN 0 ELSE 1 END
        FETCH FIRST 1 ROW ONLY
    """

    def _columns(cur, owner: str) -> list[dict[str, Any]]:
        cur.execute(columns_sql, owner=owner, tname=table)
        return [
            {
                "name": cname,
                "type": _format_type(dtype, dlen, dprec, dscale),
                "nullable": nullable == "Y",
                "comment": comment,
            }
            for cname, dtype, dlen, dprec, dscale, nullable, comment in cur
        ]

    with _acquire() as conn, conn.cursor() as cur:
        # Try the name as a table/view in the target schema first.
        owner = schema
        resolved_to: str | None = None
        columns = _columns(cur, owner)

        if not columns:
            # Fall back to resolving a synonym to its underlying object.
            cur.execute(synonym_sql, name=table, owner=schema)
            row = cur.fetchone()
            if row is not None:
                owner, base_table = row
                resolved_to = f"{owner}.{base_table}"
                # The columns/PK/FK queries key on table_name, so re-target it.
                table = base_table
                columns = _columns(cur, owner)
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
    if resolved_to is not None:
        result["resolved_to"] = resolved_to
    return result


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
    ``sql``. Raises :class:`~queryforge.sql_guard.SqlGuardError` for unsafe SQL
    and ``oracledb`` errors for execution failures (caller decides how to surface).
    """
    cfg = get_settings()
    cap = cfg.max_rows if max_rows is None else max_rows

    validate_select(sql)
    # Fetch one extra row so we can tell the result was truncated.
    executable = apply_row_cap(sql, cap + 1)

    with _acquire() as conn, conn.cursor() as cur:
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
