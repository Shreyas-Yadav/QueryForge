"""The system prompt and list_tables tool expose only Oracle-internal objects."""

from __future__ import annotations

import json

from queryforge import agent_core, db, prompt


def _no_db(*_a, **_k):
    raise AssertionError("must not touch the database")


def test_prompt_never_lists_application_tables(monkeypatch):
    monkeypatch.setattr(db, "_acquire", _no_db)
    text = prompt.system_prompt_text()
    assert "Tables in this schema" not in text
    assert "only" in text.lower() and "data dictionary" in text.lower()


def test_prompt_refuses_listing_schemas_but_allows_resolving_ids():
    """Scenario (c): listing schemas/users is refused by the prompt."""
    text = prompt.system_prompt_text()
    assert "list" in text.lower() and "schemas" in text.lower()
    assert "refuse" in text.lower()
    assert "DBA_USERS" in text  # still allowed for resolving an ASH USER_ID


def test_list_tables_tool_returns_only_internal_views(monkeypatch):
    monkeypatch.setattr(db, "_acquire", _no_db)
    content, is_error, _ = agent_core._execute_tool("list_tables", {})
    names = [v["name"] for v in json.loads(content)]
    assert not is_error
    assert "V$SQLAREA" in names and "DBA_HIST_ACTIVE_SESS_HISTORY" in names
    assert all(n.startswith(("V$", "GV$", "DBA_", "DBMS_")) for n in names)
