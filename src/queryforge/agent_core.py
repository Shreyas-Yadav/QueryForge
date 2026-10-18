"""Provider-neutral agent core: the tool contract and tool execution.

Both the Claude (:mod:`queryforge.agent`) and Gemini
(:mod:`queryforge.agent_gemini`) loops share these pieces. Keeping them here —
rather than in either provider module — means neither provider depends on the
other's internals. Each provider converts :data:`TOOL_SPECS` into its own tool
format and drives its own SDK loop, but the tool logic lives here, once.
"""

from __future__ import annotations

import json
from typing import Any

from . import db, observability
from .sql_guard import SqlGuardError

MAX_TURNS = 12
# rows of a result the *model* sees (UI gets the full capped set)
PREVIEW_ROWS = 20

# What ``list_tables`` returns: the key Oracle performance views. Application
# tables are never listed — QueryForge only reads Oracle-maintained objects.
PERFORMANCE_VIEWS: list[dict[str, str]] = [
    {"name": "V$SQLAREA", "about": "Per-statement totals for cursors in the shared pool (time in microseconds)."},
    {"name": "V$SQL", "about": "Per-child-cursor statistics, plan hash and parsing schema."},
    {"name": "V$SQLSTATS", "about": "Statement statistics kept longer than V$SQL; cheap to query."},
    {"name": "V$SQL_PLAN", "about": "Execution plans of cursors in the shared pool."},
    {"name": "V$SESSION", "about": "Current sessions: SQL_ID, EVENT, WAIT_CLASS, BLOCKING_SESSION."},
    {"name": "V$ACTIVE_SESSION_HISTORY", "about": "ASH: one sample per active session per second (~last hour)."},
    {"name": "V$SYSTEM_EVENT", "about": "Cumulative wait-event totals since instance startup."},
    {"name": "V$SYSSTAT", "about": "Cumulative instance statistics (logical reads, parses, ...)."},
    {"name": "V$SYS_TIME_MODEL", "about": "DB time / DB CPU breakdown since startup."},
    {"name": "V$SGASTAT", "about": "SGA memory by pool and component."},
    {"name": "V$PGASTAT", "about": "PGA memory usage statistics."},
    {"name": "V$LOCK", "about": "Locks held and requested."},
    {"name": "GV$SESSION", "about": "V$SESSION across all RAC instances."},
    {"name": "DBA_HIST_SNAPSHOT", "about": "AWR snapshots: SNAP_ID with BEGIN/END_INTERVAL_TIME."},
    {"name": "DBA_HIST_SQLSTAT", "about": "AWR per-snapshot SQL statistics (deltas in *_DELTA columns)."},
    {"name": "DBA_HIST_SQLTEXT", "about": "AWR SQL text by SQL_ID."},
    {"name": "DBA_HIST_SQL_PLAN", "about": "AWR historical execution plans."},
    {"name": "DBA_HIST_ACTIVE_SESS_HISTORY", "about": "AWR-persisted ASH (1-in-10 samples)."},
    {"name": "DBA_HIST_SYSTEM_EVENT", "about": "AWR wait-event totals per snapshot."},
    {"name": "DBA_HIST_SYS_TIME_MODEL", "about": "AWR time-model statistics per snapshot."},
    {"name": "DBA_USERS", "about": "Resolve a USER_ID from a performance row to a username."},
    {"name": "DBA_OBJECTS", "about": "Resolve an OBJECT_ID / CURRENT_OBJ# to an object name."},
    {"name": "DBMS_XPLAN", "about": "Package: SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR('<sql_id>'))."},
]

# Provider-neutral tool specs (name / description / JSON-schema). Each provider
# converts these into its own tool format.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "list_tables",
        "description": (
            "List the key Oracle performance and data-dictionary views you may query, "
            "each with a one-line description. Only Oracle-maintained objects may be "
            "queried; application tables are never available."
        ),
        "schema": {"type": "object", "properties": {}},
    },
    {
        "name": "describe_table",
        "description": (
            "Get columns (name, type, nullability, comments) of one Oracle data-dictionary "
            "or performance view (e.g. V$SQLAREA). Call this before writing SQL against a "
            "view you have not inspected. Application tables are refused."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "View name, optionally OWNER.NAME (case-insensitive)."}
            },
            "required": ["name"],
        },
    },
    {
        "name": "run_query",
        "description": (
            "Execute a single read-only Oracle SELECT and return a preview of the rows. "
            "Only SELECT over Oracle data-dictionary / performance views is permitted; "
            "writes, DDL and application tables are rejected. Results are capped, "
            "and the full capped result set is shown to the user automatically — you "
            "receive only a preview, so do not attempt to paginate."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A single Oracle SELECT statement."}
            },
            "required": ["sql"],
        },
    },
]


def execute_tool(name: str, tool_input: dict[str, Any]) -> tuple[str, bool, dict[str, Any] | None]:
    """Run a tool inside a TOOL trace span. See :func:`_execute_tool` for the contract."""
    return observability.trace_tool(name, tool_input, lambda: _execute_tool(name, tool_input))


def _execute_tool(name: str, tool_input: dict[str, Any]) -> tuple[str, bool, dict[str, Any] | None]:
    """Run a tool.

    Returns ``(content_for_model, is_error, ui_payload)`` where ``ui_payload`` is a
    full result dict to surface to the UI (only for ``run_query``), else None.
    """
    if name == "list_tables":
        return json.dumps(PERFORMANCE_VIEWS), False, None

    if name == "describe_table":
        try:
            return json.dumps(db.describe_table(tool_input["name"]), default=str), False, None
        except ValueError as e:
            return str(e), True, None

    if name == "run_query":
        sql = tool_input.get("sql", "")
        try:
            result = db.run_select(sql)
        except SqlGuardError as e:
            return f"Query rejected by safety guard: {e}", True, None
        except Exception as e:  # noqa: BLE001 — surface DB errors for self-correction
            return f"Database error: {e}", True, None

        preview = {
            "columns": result["columns"],
            "rows": result["rows"][:PREVIEW_ROWS],
            "row_count": result["row_count"],
            "preview_rows": min(PREVIEW_ROWS, result["row_count"]),
            "truncated": result["truncated"],
        }
        return json.dumps(preview, default=str), False, result

    return f"Unknown tool: {name}", True, None
