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
