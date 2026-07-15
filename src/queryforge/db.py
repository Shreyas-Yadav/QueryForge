"""Oracle connectivity (python-oracledb, thin mode) and read-only query helpers.

Thin mode needs no Oracle Instant Client. For an Autonomous Database using mTLS
the PEM wallet (``ewallet.pem``) is used via ``config_dir`` + ``wallet_location``
+ ``wallet_password``. If the ADB is configured for one-way TLS instead, leave the
wallet settings blank and put the full connect descriptor in ``ORACLE_DSN``.

Every public function here is read-only. ``run_select`` additionally routes SQL
through :mod:`queryforge.sql_guard` and enforces a row cap + per-query timeout.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import threading
from typing import Any

import oracledb

from .config import Settings, get_settings
from .sql_guard import apply_row_cap, validate_select

_pool: oracledb.ConnectionPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> oracledb.ConnectionPool:
    """Return a process-wide connection pool, creating it on first use."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = _create_pool(get_settings())
    return _pool


def _create_pool(cfg: Settings) -> oracledb.ConnectionPool:
    kwargs: dict[str, Any] = dict(
        user=cfg.oracle_user,
        password=cfg.oracle_password,
        dsn=cfg.oracle_dsn,
        min=1,
        max=4,
        increment=1,
        homogeneous=True,
    )
    if cfg.uses_wallet:
        # mTLS: PEM wallet (thin mode).
        kwargs.update(
            config_dir=cfg.oracle_config_dir,
            wallet_location=cfg.oracle_wallet_location,
            wallet_password=cfg.oracle_wallet_password,
        )
    return oracledb.create_pool(**kwargs)


def close_pool() -> None:
    """Close the pool (call on application shutdown)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def _schema_name() -> str:
    """The schema the agent reads — ORACLE_SCHEMA if set, else the connecting user."""
    cfg = get_settings()
    return (cfg.oracle_schema or cfg.oracle_user).upper()


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
