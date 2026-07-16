"""Gemini agent-loop test with the google-genai client and DB fully mocked.

The Gemini path is the primary runtime provider but previously had no loop
coverage. This exercises the happy path — a tool call, the model-preview vs
full-result split, and a final answer — without touching Vertex or Oracle.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from queryforge import agent_core, agent_gemini, prompt


def _part(text=None, function_call=None):
    return SimpleNamespace(text=text, function_call=function_call)


def _response(parts):
    content = SimpleNamespace(role="model", parts=parts)
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


class _FakeModels:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.models = _FakeModels(responses)


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch):
    monkeypatch.setattr(
        agent_gemini,
        "get_settings",
        lambda: SimpleNamespace(
            gemini_api_key="k", gcp_project_id=None,
            vertex_region="us-east5", gemini_model="gemini-3.1-flash-lite",
        ),
    )
    prompt._schema_overview.cache_clear()
    monkeypatch.setattr(prompt.db, "list_tables", lambda: [])


def test_happy_path_runs_query_and_answers(monkeypatch):
    responses = [
        _response([
            _part(text="Let me check."),
            _part(function_call=SimpleNamespace(
                name="run_query", args={"sql": "SELECT COUNT(*) FROM orders"})),
        ]),
        _response([_part(text="There are 42 orders.")]),
    ]
    monkeypatch.setattr(agent_gemini.genai, "Client", lambda **kw: _FakeClient(responses))
    monkeypatch.setattr(
        agent_core.db,
        "run_select",
        lambda sql: {
            "columns": ["COUNT(*)"],
            "rows": [[42]],
            "row_count": 1,
            "truncated": False,
            "sql": sql,
        },
    )

    events = list(agent_gemini.run_agent("how many orders?"))
    types = [e["type"] for e in events]

    assert "sql" in types
    assert "result" in types
    answer = next(e for e in events if e["type"] == "answer")
    assert "42 orders" in answer["text"]

    result = next(e for e in events if e["type"] == "result")
    assert result["columns"] == ["COUNT(*)"]
    assert result["rows"] == [[42]]
