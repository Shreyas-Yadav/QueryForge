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

from . import db
from .sql_guard import SqlGuardError

MAX_TURNS = 12
# rows of a result the *model* sees (UI gets the full capped set)
PREVIEW_ROWS = 20

# Provider-neutral tool specs (name / description / JSON-schema). Each provider
# converts these into its own tool format.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "list_tables",
        "description": (
            "List the objects available to query, with any comments. Each entry has an "
            "'object_type' of 'TABLE' or 'SYNONYM'; synonyms also show 'points_to' (the "
            "underlying OWNER.NAME). Query a synonym by its own name like any table."
        ),
        "schema": {"type": "object", "properties": {}},
    },
    {
        "name": "describe_table",
        "description": (
            "Get columns (name, type, nullability, comments), the primary key, and "
            "foreign keys for one table. Call this before writing SQL against a table "
            "you have not inspected."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Table name (case-insensitive)."}
            },
            "required": ["name"],
        },
    },
    {
        "name": "run_query",
        "description": (
            "Execute a single read-only Oracle SELECT and return a preview of the rows. "
            "Only SELECT is permitted; writes and DDL are rejected. Results are capped, "
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
    """Run a tool.

    Returns ``(content_for_model, is_error, ui_payload)`` where ``ui_payload`` is a
    full result dict to surface to the UI (only for ``run_query``), else None.
    """
    if name == "list_tables":
        return json.dumps(db.list_tables(), default=str), False, None

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
