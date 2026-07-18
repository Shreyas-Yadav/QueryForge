"""System-prompt construction for the QueryForge agent (provider-neutral).

Builds the instruction text both provider loops send as their system prompt,
including a best-effort schema overview and the dynamic-performance / AWR-ASH
view catalog. Kept separate from the agent loops so the prompt — the part most
often edited — has one clear home.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from . import db

logger = logging.getLogger("queryforge.audit")


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
        f"- {t['table_name']}" +
        (f" — {t['comments']}" if t.get("comments") else "")
        for t in tables
    ]
    return "\n".join(lines)


def clear_schema_cache() -> None:
    """Forget the cached schema overview.

    Must be called whenever the agent is pointed at a different database —
    otherwise the system prompt keeps advertising the previous database's tables
    and the model writes SQL against objects that aren't there.
    """
    _schema_overview.cache_clear()


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
        "- Beyond the schema tables listed below, you may also query Oracle's dynamic "
        "performance and data-dictionary views for questions about database performance, "
        "load, or monitoring. Key ones: `V$SQLAREA`/`V$SQL`/`V$SQLSTATS` hold per-statement "
        "stats (`ELAPSED_TIME`, `CPU_TIME`, `EXECUTIONS`, `BUFFER_GETS`, `DISK_READS`, "
        "`SQL_ID`, `SQL_TEXT`; time columns are in MICROSECONDS), and `DBA_HIST_SQLSTAT` "
        "holds AWR history. For active-session / wait analysis use ASH (Active Session "
        "History): `V$ACTIVE_SESSION_HISTORY` (recent, ~last hour, one sample per active "
        "session per second) and `DBA_HIST_ACTIVE_SESS_HISTORY` (AWR-persisted, 1-in-10 "
        "samples). Key ASH columns: `SAMPLE_TIME`, `SESSION_ID`, `SQL_ID`, `EVENT`, "
        "`WAIT_CLASS`, `SESSION_STATE` ('ON CPU' vs 'WAITING'), `BLOCKING_SESSION`; ASH is "
        "sampled, so aggregate with COUNT(*) (each row ≈ one second of activity) rather "
        "than reading individual rows. These views aren't in the table list — call "
        "`describe_table` (e.g. `describe_table('V$SQLAREA')`) to see their columns. "
        "Example — top 10 queries by execution time: `SELECT sql_id, executions, "
        "elapsed_time, sql_text FROM v$sqlarea ORDER BY elapsed_time DESC FETCH FIRST 10 "
        "ROWS ONLY`. Example — top wait events from ASH: `SELECT event, COUNT(*) AS samples "
        "FROM v$active_session_history GROUP BY event ORDER BY samples DESC FETCH FIRST 10 "
        "ROWS ONLY`.\n"
        "- Resolving ASH IDs and historical SQL text: ASH stores cryptic IDs, not names "
        "— translate them via dimension views, joining on the shared key. `USER_ID` → "
        "`DBA_USERS` (join `USER_ID`; gives `USERNAME`). ASH `CURRENT_OBJ#` → `DBA_OBJECTS` "
        "(join `DBA_OBJECTS.OBJECT_ID`; gives `OWNER`/`OBJECT_NAME`/`OBJECT_TYPE`) — but "
        "`CURRENT_OBJ#` is often -1 or NULL when no single object applies, so use an OUTER "
        "join. For the SQL text behind a `SQL_ID`: live ASH (`V$ACTIVE_SESSION_HISTORY`) "
        "still has it in `V$SQL`/`V$SQLAREA`, but historical ASH "
        "(`DBA_HIST_ACTIVE_SESS_HISTORY`) does NOT — `V$SQLAREA` only holds cursors still "
        "in the shared pool — so read it from `DBA_HIST_SQLTEXT` (join `SQL_ID`; gives "
        "`SQL_TEXT`). To scope historical ASH to a time window, join "
        "`DBA_HIST_ACTIVE_SESS_HISTORY.SNAP_ID` = `DBA_HIST_SNAPSHOT.SNAP_ID` and filter "
        "on `BEGIN_INTERVAL_TIME`/`END_INTERVAL_TIME`.\n"
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
