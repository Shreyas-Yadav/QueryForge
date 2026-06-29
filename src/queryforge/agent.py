"""The QueryForge agent: a Claude tool-use loop running on Google Vertex AI.

``run_agent`` is a generator that yields structured events as the agent works,
so the web layer can stream progress over SSE. The agent is given three
read-only tools (``list_tables``, ``describe_table``, ``run_query``); it inspects
the schema as needed, writes Oracle SQL, runs it, and self-corrects on errors.

Key design points (per the project plan):
- Model context vs UI output are separated: ``run_query`` returns only a small
  preview to the model, while the full row-capped result is emitted to the UI
  out-of-band via a ``result`` event.
- The schema overview and dialect instructions sit in a cached system block.
- Every (question, SQL, outcome) is written to an audit log.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from anthropic import AnthropicVertex

from . import db
from .config import get_settings
from .sql_guard import SqlGuardError

logger = logging.getLogger("queryforge.audit")

MAX_TURNS = 12
PREVIEW_ROWS = 20  # rows of a result the *model* sees (UI gets the full capped set)

# Provider-neutral tool specs (name / description / JSON-schema). Each provider
# converts these into its own tool format.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "list_tables",
        "description": "List the tables available in the database, with any table comments.",
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

# Anthropic tool format (used by the Claude provider below).
TOOLS: list[dict[str, Any]] = [
    {"name": s["name"], "description": s["description"], "input_schema": s["schema"]}
    for s in TOOL_SPECS
]


@lru_cache(maxsize=1)
def _schema_overview() -> str:
    """A compact 'table — comment' listing embedded in the (cached) system prompt.

    Best-effort: if the DB is unreachable at build time, return an empty string and
    let the model discover the schema via the list_tables tool.
    """
    try:
        tables = db.list_tables()
    except Exception as e:  # noqa: BLE001 — degrade gracefully
        logger.warning("Could not pre-load schema overview: %s", e)
        return ""
    lines = [
        f"- {t['table_name']}" + (f" — {t['comments']}" if t.get("comments") else "")
        for t in tables
    ]
    return "\n".join(lines)


def system_prompt_text() -> str:
    """The provider-neutral system prompt, including a best-effort schema overview."""
    overview = _schema_overview()
    schema_section = (
        f"\n\nTables in this schema:\n{overview}" if overview else
        "\n\nThe schema overview was not preloaded — call `list_tables` to discover tables."
    )
    return (
        "You are QueryForge, a careful data analyst with read-only access to an "
        "Oracle database. Turn the user's natural-language question into Oracle SQL, "
        "run it, and answer in plain language grounded in the actual results.\n\n"
        "Rules:\n"
        "- The database is ORACLE. Use Oracle SQL dialect: `FETCH FIRST n ROWS ONLY` "
        "(never `LIMIT`), `NVL(x, y)`, `SYSDATE`/`CURRENT_DATE`, `||` for string "
        "concatenation, and `DATE '2024-01-01'` for date literals.\n"
        "- Data-dictionary identifiers are uppercase; for case-insensitive text "
        "matching use `UPPER(col) LIKE UPPER('...')`.\n"
        "- Inspect unfamiliar tables with `describe_table` before writing SQL.\n"
        "- Only SELECT statements are allowed; any write/DDL will be rejected.\n"
        "- `run_query` returns a preview; the user is shown the full (capped) result "
        "separately, so don't paginate. If a result is truncated, say so.\n"
        "- If a query errors, read the error and fix the SQL.\n"
        "- The result rows are ALREADY displayed to the user in a table. Do NOT "
        "reproduce the rows, rebuild the table, or list per-row values in your answer. "
        "Instead give a brief (1-3 sentence) natural-language summary: the headline "
        "finding, totals, ranges, or anything notable. For a single aggregate value "
        "(e.g. a COUNT) state that number; otherwise describe the result, don't dump it."
        + schema_section
    )


def _system_blocks() -> list[dict[str, Any]]:
    return [{"type": "text", "text": system_prompt_text(), "cache_control": {"type": "ephemeral"}}]


def _execute_tool(name: str, tool_input: dict[str, Any]) -> tuple[str, bool, dict[str, Any] | None]:
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


def run_agent(question: str) -> Iterator[dict[str, Any]]:
    """Drive the agent loop, yielding event dicts for streaming to the UI.

    Event shapes (all have a ``type``):
      - ``{"type": "status", "message": str}``
      - ``{"type": "thinking", "text": str}``
      - ``{"type": "assistant_text", "text": str}``
      - ``{"type": "tool_call", "name": str, "input": dict}``
      - ``{"type": "sql", "sql": str}``
      - ``{"type": "result", "sql": str, "columns": [...], "rows": [...], "row_count": int, "truncated": bool}``
      - ``{"type": "tool_error", "name": str, "message": str}``
      - ``{"type": "answer", "text": str}``
      - ``{"type": "error", "message": str}``
    """
    cfg = get_settings()
    client = AnthropicVertex(project_id=cfg.gcp_project_id, region=cfg.vertex_region)

    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    yield {"type": "status", "message": "Thinking…"}

    for _ in range(MAX_TURNS):
        try:
            response = client.messages.create(
                model=cfg.model,
                max_tokens=4096,
                system=_system_blocks(),
                messages=messages,
                tools=TOOLS,
                thinking={"type": "adaptive"},
            )
        except Exception as e:  # noqa: BLE001 — report model/transport failures cleanly
            logger.exception("Vertex request failed")
            yield {"type": "error", "message": f"Model request failed: {e}"}
            return

        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "thinking" and getattr(block, "thinking", ""):
                yield {"type": "thinking", "text": block.thinking}
            elif block.type == "text" and block.text.strip():
                yield {"type": "assistant_text", "text": block.text}

        if response.stop_reason != "tool_use":
            final = "".join(b.text for b in response.content if b.type == "text").strip()
            logger.info("Q=%r answered (stop_reason=%s)", question, response.stop_reason)
            yield {"type": "answer", "text": final}
            return

        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            yield {"type": "tool_call", "name": block.name, "input": block.input}
            if block.name == "run_query":
                yield {"type": "sql", "sql": block.input.get("sql", "")}

            content, is_error, ui = _execute_tool(block.name, dict(block.input))
            logger.info(
                "Q=%r tool=%s input=%s error=%s", question, block.name, block.input, is_error
            )

            if ui is not None:
                yield {
                    "type": "result",
                    "sql": ui["sql"],
                    "columns": ui["columns"],
                    "rows": ui["rows"],
                    "row_count": ui["row_count"],
                    "truncated": ui["truncated"],
                }
            if is_error:
                yield {"type": "tool_error", "name": block.name, "message": content}

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                }
            )

        messages.append({"role": "user", "content": tool_results})

    yield {"type": "error", "message": f"Gave up after {MAX_TURNS} steps without a final answer."}
