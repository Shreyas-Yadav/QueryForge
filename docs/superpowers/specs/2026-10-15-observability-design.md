# Agent observability (Arize Phoenix) — design

**Date:** 2026-10-15
**Status:** Approved in chat, awaiting written-spec review
**Branch:** `feature/observability` (built on `feature/db-login`)

## Goal

Record every question the agent answers as one **trace** — a tree of every
model call and tool call with prompts, outputs, tokens, timings and errors — and
view it in **Arize Phoenix** running locally, using the industry-standard stack
(OpenTelemetry + OpenInference) kept light enough for a laptop.

## Decisions (from brainstorming)

| Decision | Choice | Why |
|---|---|---|
| Goal | Practical debugging **and** the industry-standard stack | User choice "C" |
| Viewer | Arize Phoenix, self-hosted in Docker | Local-only data, one ~320 MB arm64 container, OTel-native, reusable for evals later |
| Instrumentation | Approach 1: auto-instrumented LLM spans + hand-written AGENT and TOOL spans | Least code, full picture |
| Tracing switch | On only when `PHOENIX_COLLECTOR_ENDPOINT` is set | App must work unchanged without Phoenix |
| Content | Full content by default; `TRACE_HIDE_CONTENT=true` hides it | Everything stays on the laptop |

## Verified facts this design relies on

Measured in a throwaway environment before writing this spec:

- `openinference-instrumentation-google-genai` 1.4.x and
  `openinference-instrumentation-anthropic` 3.0.x exist and instrument the two SDKs
  QueryForge uses; `TraceConfig(hide_inputs=..., hide_outputs=...)` controls content.
- Plain OpenTelemetry SDK `TracerProvider` with resource attribute
  `openinference.project.name=queryforge` + `BatchSpanProcessor` +
  `OTLPSpanExporter(endpoint=".../v1/traces", timeout=2)`:
  recording a span costs ~0.1 ms even with Phoenix down; `shutdown()` with Phoenix
  down takes ~1 s. (With the default exporter timeout, a flush blocked ~6 s.)
- `arize-phoenix-otel`'s `register()` is therefore **not** used: it does not expose
  the exporter timeout. The plain SDK is used instead.
- With the exporter's logger at ERROR, a failed batch logs one line instead of a
  retry warning per attempt.

## Design

### Span tree for one question

```
🧭 queryforge.question   AGENT  (runner.run_agent)
├─ 🤖 LLM span           LLM    (automatic, OpenInference instrumentor)
├─ 🔧 tool.describe_table TOOL  (agent_core.execute_tool)
├─ 🤖 LLM span
├─ 🔧 tool.run_query     TOOL
└─ 🤖 LLM span
```

### What each span records

**AGENT** — name `queryforge.question`, `openinference.span.kind=AGENT`:
- `input.value` = question; `output.value` = final answer (both omitted when hidden)
- `user.id` = logged-in Oracle user (if any)
- `metadata` = JSON `{"database": <entry name>, "provider": ..., "model": ...}`
- `queryforge.outcome` = `answered` | `gave_up` | `error` | `cancelled`
- `queryforge.tool_calls`, `queryforge.tool_errors` (ints)
- Status `ERROR` (with the error message) unless outcome is `answered`.

Outcome rules, from the event stream: an `answer` event → `answered`; an `error`
event whose message starts with `Gave up after` → `gave_up`; any other `error`
event, an exception, or a stream ending with neither → `error`; the client
disconnecting mid-stream (`GeneratorExit`) → `cancelled`.

**LLM** — produced by the OpenInference instrumentors: model, input/output
messages, tool definitions, token counts, latency. Phoenix shows cost when it
knows the model's price.

**TOOL** — name `tool.<name>`, `openinference.span.kind=TOOL`:
- `tool.name`; `input.value` = JSON of the tool input; `output.value` = the content
  returned to the model (both omitted when hidden)
- `queryforge.row_count` for `run_query` results
- Status `ERROR` with the error text when the tool reports an error.

### Components

1. **`src/queryforge/observability.py` (new)** — the only module that imports
   OpenTelemetry/OpenInference:
   - `setup_tracing(provider=None) -> bool` — if a provider is given (tests) or
     `PHOENIX_COLLECTOR_ENDPOINT` is set, build the provider (plain SDK as above),
     instrument both SDKs with a `TraceConfig` honouring `TRACE_HIDE_CONTENT`, and
     install the module tracer. Never raises: failures log a warning and leave
     tracing off.
   - `shutdown_tracing()` — flush and shut down the provider, uninstrument the SDKs,
     reset to a no-op tracer.
   - `trace_agent_run(question, *, provider, model, user, database, run)` — wraps
     an agent event iterator in the AGENT span (see "Threading" below).
   - `trace_tool(name, tool_input, call)` — runs `call()` inside a TOOL span.
2. **`runner.py`** — resolves the provider loop via a small `_provider_loop()`
   helper (also the test seam), reads user/database from `db.active_profile()`,
   and returns `observability.trace_agent_run(...)` around the loop.
3. **`agent_core.execute_tool`** — becomes a thin wrapper:
   `observability.trace_tool(name, tool_input, lambda: _execute_tool(...))`; the
   existing body moves to `_execute_tool` unchanged.
4. **`web/app.py`** — `lifespan` calls `setup_tracing()` on startup and
   `shutdown_tracing()` on shutdown.
5. **`agent.py`, `agent_gemini.py`** — unchanged.
6. **`config.py`** — `phoenix_collector_endpoint: str | None = None`,
   `trace_hide_content: bool = False`.

### Threading (the key correctness point)

The agent is a generator that Starlette resumes on arbitrary worker threads, and
OpenTelemetry's "current span" lives in a contextvars context that each resumed step gets a fresh copy of. `trace_agent_run` therefore
does **not** keep the AGENT span current across `yield`s. Instead it drives the
inner generator itself and wraps **each `next()`** in
`trace.use_span(agent_span, end_on_exit=False)`. Every LLM and TOOL span created
while the inner loop runs (between two yields) becomes a child of the AGENT span,
whatever thread that step runs on. Nothing is attached while control is outside
the inner generator. On close (client disconnect) the inner generator is closed
too.

### Failure behaviour

| Situation | Behaviour |
|---|---|
| Endpoint unset | No-op tracer; events pass through unchanged |
| Phoenix down | Spans dropped by the background exporter; one ERROR log line per failed batch; questions not slowed |
| App shutdown with Phoenix down | Bounded by exporter timeout 2 s (measured ~1 s) |
| Error while setting up tracing | Logged warning, tracing stays off, app starts normally |
| Exception inside the agent | Recorded on the AGENT span, re-raised unchanged (existing SSE error handling applies) |

### Privacy

- Default records full content locally. `TRACE_HIDE_CONTENT=true` omits
  `input.value`/`output.value` on AGENT and TOOL spans and passes
  `hide_inputs`/`hide_outputs` to the instrumentors.
- Database passwords live only in `OracleProfile` and never enter prompts, tool
  inputs or tool outputs; a test asserts no span attribute contains the login
  password.

### Phoenix

```
docker run -d --name qf-phoenix -p 6006:6006 \
  -e PHOENIX_WORKING_DIR=/mnt/data -v qf-phoenix-data:/mnt/data \
  arizephoenix/phoenix:version-20.19.0
```
UI and OTLP/HTTP receiver both on `http://localhost:6006`. In `.env`:
`PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006`.

### Dependencies

`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`,
`openinference-semantic-conventions`, `openinference-instrumentation`,
`openinference-instrumentation-google-genai`, `openinference-instrumentation-anthropic`
— regular dependencies.

## Testing

Offline, with an in-memory span exporter (`setup_tracing(provider=...)`) and a
fake provider loop that calls the real `agent_core.execute_tool` against a
monkeypatched `db`:

- Answered question → one AGENT span with TOOL children, same trace id; outcome,
  user, database, counts, input/output correct.
- **Thread test** — each `next()` on a brand-new thread; all TOOL spans still
  children of the AGENT span.
- Tool error → TOOL span `ERROR` with the Oracle message; `tool_errors` counted.
- Gave-up and error streams → right outcome and `ERROR` status.
- Client disconnect (generator closed) → `cancelled`, span ended.
- Tracing off → no spans, events unchanged.
- `TRACE_HIDE_CONTENT=true` → no `input.value`/`output.value` on AGENT/TOOL spans.
- Login + query via the HTTP API with a known password → password in no span.
- Unreachable endpoint → a question completes in < 1 s.

Live check: Phoenix in Docker; a question as `shreyas` on the 19c test database
appears in Phoenix as AGENT → LLM/TOOL tree with token counts; the `V$SQL`
question as `qf_readonly` appears red with `gave_up`.

## Docs

README "Observability" section (start Phoenix, settings, what you see);
`.env.example` gains `PHOENIX_COLLECTOR_ENDPOINT` and `TRACE_HIDE_CONTENT`.

## Out of scope

Evals; metrics dashboards beyond Phoenix's built-ins; user feedback capture;
tracing of non-agent endpoints (`/login`, `/databases`); hosted Phoenix.
