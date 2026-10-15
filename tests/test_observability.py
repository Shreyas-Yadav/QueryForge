"""Agent tracing, captured in memory — no Phoenix, model or database needed."""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from openinference.instrumentation.anthropic import AnthropicInstrumentor
from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from queryforge import agent_core, config, db, observability, runner
from queryforge.config import OracleProfile
from queryforge.web.app import app

QUESTION = "How many employees per department?"
ANSWER = "Sales has 2."
PASSWORD = "s3cret-pw"
LOCAL = OracleProfile(
    target="AIWORLD (local)", user="shreyas", password=PASSWORD, dsn="localhost:1539/AIWORLD"
)
RESULT = {
    "columns": ["DEPT", "N"],
    "rows": [["Sales", 2]],
    "row_count": 1,
    "truncated": False,
    "sql": "SELECT dept, COUNT(*) n FROM employees GROUP BY dept",
}


def set_env(monkeypatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    config.get_settings.cache_clear()


def capture() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    assert observability.setup_tracing(provider=provider) is True
    return exporter


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return capture()


@pytest.fixture
def logged_in(fake_pools):
    db.replace_pool(LOCAL)


@pytest.fixture
def fake_db(monkeypatch):
    monkeypatch.setattr(
        agent_core.db,
        "list_tables",
        lambda: [{"table_name": "EMPLOYEES", "comments": None, "object_type": "TABLE"}],
    )
    monkeypatch.setattr(agent_core.db, "run_select", lambda sql: RESULT)


DEFAULT_TOOLS = (("list_tables", {}), ("run_query", {"sql": "SELECT 1 FROM dual"}))


def use_loop(monkeypatch, *, tools=DEFAULT_TOOLS, ending=None) -> None:
    """Replace the provider loop with a scripted one that calls the real tools."""
    ending = ending or {"type": "answer", "text": ANSWER}

    def loop(question):
        yield {"type": "status", "message": "Thinking…"}
        for name, args in tools:
            yield {"type": "tool_call", "name": name, "input": args}
            content, is_error, _ = agent_core.execute_tool(name, args)
            if is_error:
                yield {"type": "tool_error", "name": name, "message": content}
        yield ending

    monkeypatch.setattr(runner, "_provider_loop", lambda provider: loop)


def drain_on_new_threads(gen) -> list[dict]:
    """Resume the generator on a brand-new thread each step, like Starlette does."""
    events: list[dict] = []
    while True:
        box: dict = {}

        def step():
            try:
                box["event"] = next(gen)
            except StopIteration:
                box["done"] = True
            except BaseException as e:  # noqa: BLE001 — re-raised on the test thread
                box["error"] = e

        thread = threading.Thread(target=step)
        thread.start()
        thread.join()
        if "error" in box:
            raise box["error"]
        if box.get("done"):
            return events
        events.append(box["event"])


def by_kind(exporter: InMemorySpanExporter, kind: str) -> list:
    return [
        s for s in exporter.get_finished_spans()
        if s.attributes.get("openinference.span.kind") == kind
    ]


# --- setup -----------------------------------------------------------------------


def test_tracing_is_off_without_an_endpoint():
    assert observability.setup_tracing() is False


def test_setup_instruments_both_sdks_once(spans):
    assert GoogleGenAIInstrumentor().is_instrumented_by_opentelemetry
    assert AnthropicInstrumentor().is_instrumented_by_opentelemetry
    capture()  # a second setup must not raise "already instrumented"
    observability.shutdown_tracing()
    assert not GoogleGenAIInstrumentor().is_instrumented_by_opentelemetry
    assert not AnthropicInstrumentor().is_instrumented_by_opentelemetry


# --- the AGENT span ------------------------------------------------------------------


def test_answered_question_records_the_run(spans, logged_in, fake_db, monkeypatch):
    use_loop(monkeypatch)

    events = list(runner.run_agent(QUESTION))

    assert events[-1] == {"type": "answer", "text": ANSWER}
    [agent] = by_kind(spans, "AGENT")
    attrs = agent.attributes
    assert agent.name == "queryforge.question"
    assert attrs["input.value"] == QUESTION
    assert attrs["output.value"] == ANSWER
    assert attrs["user.id"] == "shreyas"
    meta = json.loads(attrs["metadata"])
    assert meta["database"] == "AIWORLD (local)"
    assert {"provider", "model"} <= meta.keys()
    assert attrs["queryforge.outcome"] == "answered"
    assert attrs["queryforge.tool_calls"] == 2
    assert attrs["queryforge.tool_errors"] == 0
    assert agent.status.status_code != StatusCode.ERROR


def test_steps_on_different_threads_stay_in_one_tree(spans, logged_in, fake_db, monkeypatch):
    use_loop(monkeypatch)

    drain_on_new_threads(runner.run_agent(QUESTION))

    [agent] = by_kind(spans, "AGENT")
    tools = by_kind(spans, "TOOL")
    assert [t.name for t in tools] == ["tool.list_tables", "tool.run_query"]
    for tool in tools:
        assert tool.context.trace_id == agent.context.trace_id
        assert tool.parent is not None
        assert tool.parent.span_id == agent.context.span_id


@pytest.mark.parametrize(
    "message, outcome",
    [
        ("Gave up after 12 steps without a final answer.", "gave_up"),
        ("Model request failed: boom", "error"),
    ],
)
def test_failed_runs_record_outcome(spans, logged_in, fake_db, monkeypatch, message, outcome):
    use_loop(monkeypatch, ending={"type": "error", "message": message})

    list(runner.run_agent(QUESTION))

    [agent] = by_kind(spans, "AGENT")
    assert agent.attributes["queryforge.outcome"] == outcome
    assert agent.status.status_code == StatusCode.ERROR
    assert message in agent.status.description


def test_exception_in_loop_is_recorded_and_reraised(spans, logged_in, monkeypatch):
    def loop(question):
        yield {"type": "status", "message": "Thinking…"}
        raise RuntimeError("SDK exploded")

    monkeypatch.setattr(runner, "_provider_loop", lambda provider: loop)

    with pytest.raises(RuntimeError, match="SDK exploded"):
        list(runner.run_agent(QUESTION))

    [agent] = by_kind(spans, "AGENT")
    assert agent.attributes["queryforge.outcome"] == "error"
    assert agent.status.status_code == StatusCode.ERROR


def test_client_disconnect_records_cancelled(spans, logged_in, fake_db, monkeypatch):
    closed = []

    def loop(question):
        try:
            yield {"type": "status", "message": "Thinking…"}
            yield {"type": "status", "message": "still going"}
        finally:
            closed.append(True)

    monkeypatch.setattr(runner, "_provider_loop", lambda provider: loop)
    gen = runner.run_agent(QUESTION)
    next(gen)

    gen.close()

    [agent] = by_kind(spans, "AGENT")
    assert agent.attributes["queryforge.outcome"] == "cancelled"
    assert closed == [True]


def test_tracing_off_passes_events_through(logged_in, fake_db, monkeypatch):
    use_loop(monkeypatch)
    events = list(runner.run_agent(QUESTION))
    assert [e["type"] for e in events] == ["status", "tool_call", "tool_call", "answer"]


# --- the TOOL span -------------------------------------------------------------------


def test_tool_span_records_input_output_and_rows(spans, logged_in, fake_db, monkeypatch):
    use_loop(monkeypatch)
    list(runner.run_agent(QUESTION))

    run_query = by_kind(spans, "TOOL")[1]
    assert run_query.attributes["tool.name"] == "run_query"
    assert json.loads(run_query.attributes["input.value"]) == {"sql": "SELECT 1 FROM dual"}
    assert '"row_count": 1' in run_query.attributes["output.value"]
    assert run_query.attributes["queryforge.row_count"] == 1


def test_tool_error_marks_tool_span_red(spans, logged_in, fake_db, monkeypatch):
    def boom(sql):
        raise RuntimeError("ORA-00942: table or view does not exist")

    monkeypatch.setattr(agent_core.db, "run_select", boom)
    use_loop(monkeypatch, tools=(("run_query", {"sql": "SELECT * FROM v$sql"}),))

    list(runner.run_agent(QUESTION))

    [tool] = by_kind(spans, "TOOL")
    assert tool.status.status_code == StatusCode.ERROR
    assert "ORA-00942" in tool.status.description
    [agent] = by_kind(spans, "AGENT")
    assert agent.attributes["queryforge.tool_errors"] == 1


# --- privacy -------------------------------------------------------------------------


def test_hidden_content_keeps_structure_only(logged_in, fake_db, monkeypatch):
    set_env(monkeypatch, TRACE_HIDE_CONTENT="true")
    exporter = capture()
    use_loop(monkeypatch)

    list(runner.run_agent(QUESTION))

    for span in by_kind(exporter, "AGENT") + by_kind(exporter, "TOOL"):
        assert "input.value" not in span.attributes
        assert "output.value" not in span.attributes
    [agent] = by_kind(exporter, "AGENT")
    assert agent.attributes["queryforge.outcome"] == "answered"


def test_password_never_in_spans(spans, fake_pools, fake_db, monkeypatch, tmp_path):
    path = tmp_path / "databases.json"
    path.write_text(json.dumps([{
        "name": "AIWORLD (local)", "host": "localhost", "port": 1539,
        "service_name": "AIWORLD", "users": ["shreyas"],
    }]), encoding="utf-8")
    set_env(monkeypatch, DATABASES_FILE=str(path))
    use_loop(monkeypatch)
    client = TestClient(app)

    login = {"database": "AIWORLD (local)", "user": "shreyas", "password": PASSWORD}
    assert client.post("/login", json=login).status_code == 200
    r = client.post("/query", json={"question": QUESTION})

    assert r.status_code == 200 and '"answer"' in r.text
    [agent] = by_kind(spans, "AGENT")
    for tool in by_kind(spans, "TOOL"):  # real Starlette thread hops, still one tree
        assert tool.parent.span_id == agent.context.span_id
    for span in spans.get_finished_spans():
        for value in span.attributes.values():
            assert PASSWORD not in str(value)


# --- Phoenix unavailable ---------------------------------------------------------------


def test_unreachable_phoenix_does_not_slow_questions(logged_in, fake_db, monkeypatch):
    set_env(monkeypatch, PHOENIX_COLLECTOR_ENDPOINT="http://127.0.0.1:9")
    assert observability.setup_tracing() is True
    use_loop(monkeypatch)

    start = time.perf_counter()
    list(runner.run_agent(QUESTION))
    assert time.perf_counter() - start < 1.0

    start = time.perf_counter()
    observability.shutdown_tracing()
    assert time.perf_counter() - start < 5.0


# --- review fixes ----------------------------------------------------------------------


def test_client_disconnect_closes_the_run_without_gc(spans, logged_in, fake_db, monkeypatch):
    """Starlette abandons a sync body iterator when the client goes away; without an
    explicit close the AGENT span stays open until the cycle GC happens to run."""
    import gc

    import anyio

    from queryforge.web import app as app_module

    use_loop(monkeypatch)
    response = app_module.query(app_module.QueryIn(question=QUESTION))
    chunks: list[dict] = []

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            chunks.append(message)
            if len(chunks) == 2:
                raise OSError("client went away")

    async def receive():
        await anyio.sleep_forever()

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "POST",
             "path": "/query", "headers": []}
    async def serve_then_inspect():
        # Inspect while the event loop is still alive, as on a real server —
        # loop shutdown would close abandoned generators and hide the bug.
        with pytest.raises(Exception):  # Starlette turns the OSError into ClientDisconnect
            await response(scope, receive, send)
        await anyio.sleep(0.05)
        return by_kind(spans, "AGENT")

    gc.disable()
    try:
        agents = anyio.run(serve_then_inspect)
    finally:
        gc.enable()
    assert len(agents) == 1, "AGENT span was not ended on disconnect"
    assert agents[0].attributes["queryforge.outcome"] == "cancelled"


def test_setup_never_raises_even_if_shutdown_fails(monkeypatch):
    def broken():
        raise RuntimeError("instrumentor import blew up")

    monkeypatch.setattr(observability, "_instrumentors", broken)
    assert observability.setup_tracing() is False
    observability.shutdown_tracing()  # must not raise either


# --- deferred review minors --------------------------------------------------------------


class _SpyProvider(TracerProvider):
    def __init__(self) -> None:
        super().__init__()
        self.was_shut_down = False

    def shutdown(self) -> None:
        self.was_shut_down = True
        super().shutdown()


class _FailingInstrumentor:
    is_instrumented_by_opentelemetry = False

    def instrument(self, **kwargs):
        raise RuntimeError("instrument failed")


def test_partial_setup_failure_shuts_the_provider_down(monkeypatch):
    monkeypatch.setattr(observability, "_instrumentors", lambda: [_FailingInstrumentor()])
    provider = _SpyProvider()

    assert observability.setup_tracing(provider=provider) is False
    assert provider.was_shut_down, "exporter thread leaked after a failed setup"


def test_lifespan_flushes_traces_even_if_closing_the_pool_fails(monkeypatch):
    import anyio

    calls = []
    monkeypatch.setattr(observability, "setup_tracing", lambda: calls.append("setup"))
    monkeypatch.setattr(observability, "shutdown_tracing", lambda: calls.append("shutdown"))

    def broken_close():
        raise RuntimeError("pool close failed")

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            pass

    with monkeypatch.context() as patch:  # scoped: shared teardown calls close_pool too
        patch.setattr(db, "close_pool", broken_close)
        with pytest.raises(RuntimeError, match="pool close failed"):
            anyio.run(run_lifespan)
    assert calls == ["setup", "shutdown"]


def test_disconnect_after_the_answer_keeps_answered(spans, logged_in, fake_db, monkeypatch):
    use_loop(monkeypatch)
    gen = runner.run_agent(QUESTION)
    for event in gen:
        if event["type"] == "answer":
            break

    gen.close()  # client leaves before reading the trailing events

    [agent] = by_kind(spans, "AGENT")
    assert agent.attributes["queryforge.outcome"] == "answered"
    assert agent.status.status_code != StatusCode.ERROR


def test_exporter_retry_noise_is_filtered_but_real_warnings_are_kept(monkeypatch, caplog):
    import logging

    set_env(monkeypatch, PHOENIX_COLLECTOR_ENDPOINT="http://127.0.0.1:9")
    exporter_logger = logging.getLogger(observability._EXPORTER_LOGGER)
    level_before = exporter_logger.level
    assert observability.setup_tracing() is True

    with caplog.at_level(logging.WARNING, logger=observability._EXPORTER_LOGGER):
        exporter_logger.warning("Transient error ConnectionError encountered while exporting span batch, retrying in 1s.")
        exporter_logger.warning("Dropping span batch: export request exceeds max_request_size")
    assert "Transient error" not in caplog.text
    assert "Dropping span batch" in caplog.text

    observability.shutdown_tracing()
    assert exporter_logger.level == level_before
    assert not exporter_logger.filters


def _anthropic_client(reply_text: str):
    import anthropic
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test",
            "content": [{"type": "text", "text": reply_text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 3},
        })

    return anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("hidden, expect_canary", [(False, True), (True, False)])
def test_llm_span_content_follows_hide_setting(monkeypatch, hidden, expect_canary):
    canary = "CANARY-7f3a"
    set_env(monkeypatch, TRACE_HIDE_CONTENT=str(hidden).lower())
    exporter = capture()

    _anthropic_client(f"{canary} answer").messages.create(
        model="claude-test", max_tokens=10, system=f"{canary} system",
        messages=[{"role": "user", "content": f"{canary} question"}],
    )

    [llm] = by_kind(exporter, "LLM")
    dumped = json.dumps(dict(llm.attributes), default=str)
    assert (canary in dumped) is expect_canary
    assert llm.attributes.get("llm.token_count.total") == 8
