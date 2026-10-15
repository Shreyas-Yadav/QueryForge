# Agent Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every agent question as one OpenTelemetry trace (AGENT → LLM/TOOL spans) and view it in a local Arize Phoenix.

**Architecture:** `observability.py` owns all OpenTelemetry code: setup/shutdown, `trace_agent_run` (AGENT span around the event stream, re-attached around every `next()` so thread hops keep one tree) and `trace_tool` (TOOL span). OpenInference instrumentors record LLM spans automatically. `runner.run_agent` and `agent_core.execute_tool` each gain one call; the provider loops are untouched.

**Tech Stack:** opentelemetry-sdk + OTLP/HTTP exporter, OpenInference (semconv, google-genai and anthropic instrumentors), Arize Phoenix in Docker, pytest with `InMemorySpanExporter`.

**Spec:** `docs/superpowers/specs/2026-10-15-observability-design.md`

## Global Constraints

- Tracing on only when `PHOENIX_COLLECTOR_ENDPOINT` is set; `setup_tracing()` never raises.
- Exporter: `OTLPSpanExporter(endpoint=<base>/v1/traces, timeout=2)`, `BatchSpanProcessor`, resource `openinference.project.name=queryforge`. No `arize-phoenix-otel`.
- `TRACE_HIDE_CONTENT=true` → no `input.value`/`output.value` on AGENT/TOOL spans; `TraceConfig(hide_inputs, hide_outputs)` for instrumentors.
- AGENT outcomes: `answered` | `gave_up` | `error` | `cancelled`; status ERROR unless `answered`.
- Provider loops (`agent.py`, `agent_gemini.py`) are not modified.
- Phoenix image `arizephoenix/phoenix:version-20.19.0`, container `qf-phoenix`, port 6006, volume `qf-phoenix-data`.
- Run tests with `uv run pytest -q`.

## Review Focus

1. Real Starlette thread hopping (not just the synthetic new-thread test) keeps one tree — Task 1 `test_password_never_in_spans` drives `/query` through `TestClient`, and Task 3 checks it live in Phoenix.
2. A user's own `.env` setting `PHOENIX_COLLECTOR_ENDPOINT` must not make offline tests send spans — Task 1 autouse fixture forces it empty.
3. Instrumentors are process-global singletons; repeated setup/shutdown in tests must not double-instrument — Task 1 `test_setup_instruments_both_sdks_once`.
4. Client disconnect mid-stream must end the span and close the provider loop — Task 1 `test_client_disconnect_records_cancelled`.
5. App shutdown with Phoenix down must not hang — Task 1 `test_unreachable_phoenix_does_not_slow_questions`.

---

## File Structure

| File | Change |
|---|---|
| `pyproject.toml`, `uv.lock` | add 6 dependencies |
| `src/queryforge/config.py` | `phoenix_collector_endpoint`, `trace_hide_content` |
| `src/queryforge/observability.py` | new |
| `src/queryforge/runner.py` | `_provider_loop()` seam; wrap in `trace_agent_run` |
| `src/queryforge/agent_core.py` | `execute_tool` → `trace_tool(... _execute_tool)` |
| `src/queryforge/web/app.py` | lifespan setup/shutdown |
| `tests/test_observability.py` | new |
| `README.md`, `.env.example` | docs |

---

### Task 1: Tracing core, wired into the agent

**Files:** Create `src/queryforge/observability.py`, `tests/test_observability.py`. Modify `pyproject.toml`, `uv.lock`, `src/queryforge/config.py`, `src/queryforge/runner.py`, `src/queryforge/agent_core.py`, `src/queryforge/web/app.py`.

**Interfaces:**
- Consumes: `db.active_profile()`, `db.NotLoggedIn`, `db.replace_pool`, fixture `fake_pools` (from `feature/db-login`).
- Produces: `observability.setup_tracing(provider=None) -> bool`, `observability.shutdown_tracing() -> None`, `observability.trace_agent_run(question, *, provider, model, user, database, run) -> Iterator[dict]`, `observability.trace_tool(name, tool_input, call) -> tuple[str, bool, dict | None]`, `runner._provider_loop(provider) -> Callable | None`, `Settings.phoenix_collector_endpoint`, `Settings.trace_hide_content`.

- [ ] **Step 1: Add dependencies**

```bash
uv add opentelemetry-sdk opentelemetry-exporter-otlp-proto-http openinference-semantic-conventions openinference-instrumentation openinference-instrumentation-google-genai openinference-instrumentation-anthropic
```
Expected: `pyproject.toml` and `uv.lock` updated; `uv run pytest -q` still green.

- [ ] **Step 2: Write the failing tests** — `tests/test_observability.py`:

```python
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


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """Never let a developer's .env turn on real exporting during tests."""
    set_env(monkeypatch, PHOENIX_COLLECTOR_ENDPOINT="", TRACE_HIDE_CONTENT="false")
    yield
    observability.shutdown_tracing()
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
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_observability.py -q`
Expected: collection error — `ImportError: cannot import name 'observability'`.

- [ ] **Step 4: Implement**

`src/queryforge/config.py` — add to `Settings` above `# --- Query guardrails ---`:

```python
    # --- Observability (Arize Phoenix over OpenTelemetry) ---
    phoenix_collector_endpoint: str | None = Field(
        None,
        description="Phoenix base URL, e.g. http://localhost:6006. Unset = tracing off.",
    )
    trace_hide_content: bool = Field(
        False,
        description="Record only structure, timings, tokens and errors — no prompts, "
        "responses, or tool inputs/outputs.",
    )
```

`src/queryforge/observability.py`:

```python
"""Agent tracing: OpenTelemetry spans, viewed in Arize Phoenix.

Each question becomes one trace: an AGENT span (:func:`trace_agent_run`), the
model calls inside it (recorded automatically by the OpenInference instrumentors
for google-genai and anthropic), and a TOOL span per tool call
(:func:`trace_tool`). This is the only module that knows about OpenTelemetry.

Tracing is off unless ``PHOENIX_COLLECTOR_ENDPOINT`` is set; the helpers then use
OpenTelemetry's no-op tracer and cost almost nothing. Spans are exported by a
background thread with a short timeout, so a stopped Phoenix never slows a
question and delays shutdown by at most a couple of seconds.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

from openinference.semconv.resource import ResourceAttributes
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Status, StatusCode

from .config import get_settings

logger = logging.getLogger(__name__)

PROJECT_NAME = "queryforge"
EXPORT_TIMEOUT_S = 2
_GAVE_UP_PREFIX = "Gave up after"
_EXPORTER_LOGGER = "opentelemetry.exporter.otlp.proto.http.trace_exporter"

ToolResult = tuple[str, bool, dict[str, Any] | None]

_provider: TracerProvider | None = None
_tracer: trace.Tracer = trace.NoOpTracer()
_hide_content = False


def _instrumentors() -> list[Any]:
    from openinference.instrumentation.anthropic import AnthropicInstrumentor
    from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor

    return [GoogleGenAIInstrumentor(), AnthropicInstrumentor()]


def _phoenix_provider(endpoint: str) -> TracerProvider:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource({ResourceAttributes.PROJECT_NAME: PROJECT_NAME}))
    exporter = OTLPSpanExporter(
        endpoint=endpoint.rstrip("/") + "/v1/traces", timeout=EXPORT_TIMEOUT_S
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    # One ERROR line per failed batch instead of a warning on every retry.
    logging.getLogger(_EXPORTER_LOGGER).setLevel(logging.ERROR)
    return provider


def setup_tracing(provider: TracerProvider | None = None) -> bool:
    """Turn tracing on; return True if spans will be recorded.

    ``provider`` is for tests (an in-memory exporter). Otherwise tracing turns on
    only when ``PHOENIX_COLLECTOR_ENDPOINT`` is set. Never raises — observability
    must not stop the app from starting.
    """
    global _provider, _tracer, _hide_content
    shutdown_tracing()
    try:
        cfg = get_settings()
        if provider is None:
            if not cfg.phoenix_collector_endpoint:
                return False
            provider = _phoenix_provider(cfg.phoenix_collector_endpoint)
        _hide_content = cfg.trace_hide_content

        from openinference.instrumentation import TraceConfig

        config = TraceConfig(hide_inputs=_hide_content, hide_outputs=_hide_content)
        for instrumentor in _instrumentors():
            instrumentor.instrument(tracer_provider=provider, config=config)
        _provider = provider
        _tracer = provider.get_tracer(__name__)
    except Exception as e:  # noqa: BLE001
        logger.warning("Tracing disabled: %s", e)
        shutdown_tracing()
        return False
    logger.info("Tracing on (Phoenix project '%s')", PROJECT_NAME)
    return True


def shutdown_tracing() -> None:
    """Send queued spans, undo instrumentation, and return to the no-op tracer."""
    global _provider, _tracer
    for instrumentor in _instrumentors():
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception as e:  # noqa: BLE001
            logger.warning("Error shutting down tracing: %s", e)
    _provider = None
    _tracer = trace.NoOpTracer()


def _set_content(span: trace.Span, attribute: str, value: Any) -> None:
    """Record a prompt/answer/tool payload unless content is hidden."""
    if _hide_content or value is None:
        return
    span.set_attribute(attribute, value if isinstance(value, str) else json.dumps(value, default=str))


def trace_tool(name: str, tool_input: dict[str, Any], call: Callable[[], ToolResult]) -> ToolResult:
    """Run one tool call inside a TOOL span."""
    attributes = {
        SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.TOOL.value,
        SpanAttributes.TOOL_NAME: name,
    }
    with _tracer.start_as_current_span(f"tool.{name}", attributes=attributes) as span:
        _set_content(span, SpanAttributes.INPUT_VALUE, tool_input)
        content, is_error, ui = call()
        _set_content(span, SpanAttributes.OUTPUT_VALUE, content)
        if ui is not None:
            span.set_attribute("queryforge.row_count", ui["row_count"])
        if is_error:
            span.set_status(Status(StatusCode.ERROR, content[:500]))
        return content, is_error, ui


def trace_agent_run(
    question: str,
    *,
    provider: str,
    model: str,
    user: str | None,
    database: str | None,
    run: Iterator[dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    """Wrap an agent's event stream in one AGENT span.

    Starlette resumes the stream on whichever worker thread is free, and
    OpenTelemetry's "current span" is per-thread. So the AGENT span is attached
    around each ``next()`` of the inner loop — never across a ``yield`` — and every
    LLM/TOOL span the loop creates during that step becomes its child, whatever
    thread the step ran on.
    """
    attributes: dict[str, Any] = {
        SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.AGENT.value,
        SpanAttributes.METADATA: json.dumps(
            {"database": database, "provider": provider, "model": model}
        ),
    }
    if user:
        attributes[SpanAttributes.USER_ID] = user
    span = _tracer.start_span("queryforge.question", attributes=attributes)
    _set_content(span, SpanAttributes.INPUT_VALUE, question)

    outcome, detail, answer = "error", "ended without an answer", None
    tool_calls = tool_errors = 0
    try:
        while True:
            with trace.use_span(span, end_on_exit=False):
                try:
                    event = next(run)
                except StopIteration:
                    break
            kind = event.get("type")
            if kind == "tool_call":
                tool_calls += 1
            elif kind == "tool_error":
                tool_errors += 1
            elif kind == "answer":
                outcome, answer = "answered", event.get("text", "")
            elif kind == "error":
                detail = event.get("message", "")
                outcome = "gave_up" if detail.startswith(_GAVE_UP_PREFIX) else "error"
            yield event
    except GeneratorExit:
        outcome, detail = "cancelled", "client disconnected"
        raise
    except BaseException as e:
        outcome, detail = "error", str(e)
        raise
    finally:
        close = getattr(run, "close", None)
        if close is not None:
            close()
        _set_content(span, SpanAttributes.OUTPUT_VALUE, answer)
        span.set_attribute("queryforge.outcome", outcome)
        span.set_attribute("queryforge.tool_calls", tool_calls)
        span.set_attribute("queryforge.tool_errors", tool_errors)
        if outcome != "answered":
            span.set_status(Status(StatusCode.ERROR, detail[:500]))
        span.end()
```

`src/queryforge/runner.py` — replace the body after the docstring:

```python
from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from . import db, observability
from .config import get_settings

AgentLoop = Callable[[str], Iterator[dict[str, Any]]]


def _provider_loop(provider: str) -> AgentLoop | None:
    if provider == "claude":
        from .agent import run_agent as loop
    elif provider == "gemini":
        from .agent_gemini import run_agent as loop
    else:
        return None
    return loop


def run_agent(question: str) -> Iterator[dict[str, Any]]:
    cfg = get_settings()
    provider = cfg.provider.lower()
    loop = _provider_loop(provider)
    if loop is None:
        yield {"type": "error", "message": f"Unknown PROVIDER '{provider}' (use 'gemini' or 'claude')."}
        return
    try:
        profile = db.active_profile()
        user, database = profile.user, profile.target
    except db.NotLoggedIn:
        user = database = None
    model = cfg.model if provider == "claude" else cfg.gemini_model
    yield from observability.trace_agent_run(
        question, provider=provider, model=model, user=user, database=database, run=loop(question)
    )
```
Also add to the module docstring: "Every run is wrapped in an AGENT trace span (see :mod:`queryforge.observability`)."

`src/queryforge/agent_core.py`: `from . import db, observability`; rename the existing `execute_tool` to `_execute_tool` (body and docstring unchanged) and add above it:

```python
def execute_tool(name: str, tool_input: dict[str, Any]) -> tuple[str, bool, dict[str, Any] | None]:
    """Run a tool inside a TOOL trace span. See :func:`_execute_tool` for the contract."""
    return observability.trace_tool(name, tool_input, lambda: _execute_tool(name, tool_input))
```

`src/queryforge/web/app.py`: `from .. import connections, db, observability, session`, and:

```python
@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    observability.setup_tracing()
    yield
    db.close_pool()
    observability.shutdown_tracing()
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest -q` → all PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/queryforge/config.py src/queryforge/observability.py src/queryforge/runner.py src/queryforge/agent_core.py src/queryforge/web/app.py tests/test_observability.py
git commit -m "Trace agent runs with OpenTelemetry for Phoenix"
```

---

### Task 2: Docs

**Files:** `README.md`, `.env.example`.

- [ ] **Step 1:** `.env.example` — append before `# --- Query guardrails ---`:

```
# --- Observability (optional) ---
# Send a trace of every question to Arize Phoenix (see README "Observability").
# Leave blank to turn tracing off.
PHOENIX_COLLECTOR_ENDPOINT=
# true = record only structure, timings, tokens and errors (no prompts/results).
TRACE_HIDE_CONTENT=false
```

- [ ] **Step 2:** `README.md` — add `observability.py  # OpenTelemetry tracing → Arize Phoenix` to Project layout, and a section before `## Run`:

````
## Observability

Every question can be recorded as a trace — the question, each model call (prompt,
response, tokens, latency) and each tool call (SQL, result preview, errors) — and
viewed in [Arize Phoenix](https://github.com/Arize-ai/phoenix), running locally.

```
docker run -d --name qf-phoenix -p 6006:6006 \
  -e PHOENIX_WORKING_DIR=/mnt/data -v qf-phoenix-data:/mnt/data \
  arizephoenix/phoenix:version-20.19.0
```

Then in `.env` set `PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006`, restart
QueryForge, ask a question, and open http://localhost:6006 → project
**queryforge**. Each trace is one question: an `AGENT` span (user, database,
outcome — `answered`, `gave_up`, `error` or `cancelled`) containing `LLM` and
`TOOL` spans. Failed runs are red.

- Tracing is off when `PHOENIX_COLLECTOR_ENDPOINT` is blank.
- If Phoenix is down, questions are unaffected; spans are dropped.
- `TRACE_HIDE_CONTENT=true` keeps structure, timings, tokens and errors but no
  prompts, answers or query results.
- Traces stay on your machine (in the `qf-phoenix-data` Docker volume).
````

- [ ] **Step 3:** `uv run pytest -q` → PASS. Commit: `git add README.md .env.example && git commit -m "Document Phoenix observability"`.

---

### Task 3: Live verification (manual)

- [ ] Start Phoenix with the README command; wait for `curl -s localhost:6006/healthz` → OK.
- [ ] Add `PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006` to `.env`; restart `uv run queryforge`.
- [ ] Log in as `shreyas`, ask "What is the average salary per department?".
- [ ] Log in as `qf_readonly`, ask "Show me the 10 slowest SQL statements".
- [ ] Fetch spans from Phoenix's REST API (`/v1/projects/queryforge/spans`) and confirm: two AGENT spans; the first `answered` with LLM spans carrying token counts and TOOL children; the second `gave_up` (or `error`) with red TOOL spans showing ORA-00942; all children share their AGENT span's trace id.
- [ ] Screenshot the Phoenix trace view.
