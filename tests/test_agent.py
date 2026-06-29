"""Agent loop tests with the Vertex client and DB fully mocked.

These exercise the tool-use loop, the model-preview vs full-result split, and the
self-correction path without touching Vertex or Oracle.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from queryforge import agent
from queryforge.sql_guard import SqlGuardError


def _blk(**kw):  # a stand-in for an SDK content block
    return SimpleNamespace(**kw)


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        # Snapshot the messages list — the agent keeps mutating the original.
        snap = dict(kwargs)
        snap["messages"] = list(kwargs.get("messages", []))
        self.calls.append(snap)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch):
    monkeypatch.setattr(
        agent,
        "get_settings",
        lambda: SimpleNamespace(
            model="claude-sonnet-4-6", gcp_project_id="proj", vertex_region="us-east5"
        ),
    )
    # Keep the schema overview empty/offline during tests.
    agent._schema_overview.cache_clear()
    monkeypatch.setattr(agent.db, "list_tables", lambda: [])


def _install_client(monkeypatch, responses):
    client = _FakeClient(responses)
    monkeypatch.setattr(agent, "AnthropicVertex", lambda **kw: client)
    return client


def test_happy_path_runs_query_and_answers(monkeypatch):
    responses = [
        _blk(
            content=[
                _blk(type="text", text="Let me check."),
                _blk(
                    type="tool_use",
                    name="run_query",
                    input={"sql": "SELECT COUNT(*) FROM orders"},
                    id="tu_1",
                ),
            ],
            stop_reason="tool_use",
        ),
        _blk(
            content=[_blk(type="text", text="There are 42 orders.")],
            stop_reason="end_turn",
        ),
    ]
    _install_client(monkeypatch, responses)
    monkeypatch.setattr(
        agent.db,
        "run_select",
        lambda sql: {
            "columns": ["COUNT(*)"],
            "rows": [[42]],
            "row_count": 1,
            "truncated": False,
            "sql": sql,
        },
    )

    events = list(agent.run_agent("how many orders?"))
    types = [e["type"] for e in events]

    assert "sql" in types
    assert "result" in types
    answer = next(e for e in events if e["type"] == "answer")
    assert "42 orders" in answer["text"]

    result = next(e for e in events if e["type"] == "result")
    assert result["columns"] == ["COUNT(*)"]
    assert result["rows"] == [[42]]


def test_self_corrects_after_guard_rejection(monkeypatch):
    responses = [
        _blk(
            content=[
                _blk(type="tool_use", name="run_query",
                     input={"sql": "DELETE FROM orders"}, id="tu_1")
            ],
            stop_reason="tool_use",
        ),
        _blk(
            content=[
                _blk(type="tool_use", name="run_query",
                     input={"sql": "SELECT COUNT(*) FROM orders"}, id="tu_2")
            ],
            stop_reason="tool_use",
        ),
        _blk(content=[_blk(type="text", text="42 orders.")], stop_reason="end_turn"),
    ]
    client = _install_client(monkeypatch, responses)

    calls = {"n": 0}

    def fake_run_select(sql):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SqlGuardError("Disallowed operation in query: DELETE.")
        return {"columns": ["C"], "rows": [[42]], "row_count": 1, "truncated": False, "sql": sql}

    monkeypatch.setattr(agent.db, "run_select", fake_run_select)

    events = list(agent.run_agent("delete then count"))
    types = [e["type"] for e in events]

    # The guard rejection surfaced as a tool_error and the loop recovered.
    assert "tool_error" in types
    assert any(e["type"] == "answer" for e in events)
    # The error was fed back as a tool_result so the model could correct.
    second_call_messages = client.messages.calls[1]["messages"]
    last = second_call_messages[-1]
    assert last["role"] == "user"
    assert last["content"][0]["is_error"] is True


def test_model_request_failure_yields_error(monkeypatch):
    class _Boom:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("vertex down")

    monkeypatch.setattr(agent, "AnthropicVertex", lambda **kw: _Boom())
    events = list(agent.run_agent("anything"))
    assert events[-1]["type"] == "error"
    assert "vertex down" in events[-1]["message"]
