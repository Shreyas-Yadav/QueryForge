"""The QueryForge agent on Claude: a tool-use loop running on Google Vertex AI.

``run_agent`` is a generator that yields structured events as the agent works,
so the web layer can stream progress over SSE. The agent is given three
read-only tools (``list_tables``, ``describe_table``, ``run_query``); it inspects
the schema as needed, writes Oracle SQL, runs it, and self-corrects on errors.

This module holds only the Claude/Anthropic-specific glue. The provider-neutral
pieces live elsewhere: the tool contract and tool execution in
:mod:`queryforge.agent_core`, and the system prompt in :mod:`queryforge.prompt`.

Key design points (per the project plan):
- Model context vs UI output are separated: ``run_query`` returns only a small
  preview to the model, while the full row-capped result is emitted to the UI
  out-of-band via a ``result`` event.
- The schema overview and dialect instructions sit in a cached system block.
- Every (question, SQL, outcome) is written to an audit log.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from anthropic import AnthropicVertex

from .agent_core import MAX_TURNS, TOOL_SPECS, execute_tool
from .config import get_settings
from .prompt import system_prompt_text

logger = logging.getLogger("queryforge.audit")

# Anthropic tool format, derived from the provider-neutral specs.
TOOLS: list[dict[str, Any]] = [
    {"name": s["name"], "description": s["description"], "input_schema": s["schema"]}
    for s in TOOL_SPECS
]


def _system_blocks() -> list[dict[str, Any]]:
    return [{"type": "text", "text": system_prompt_text(), "cache_control": {"type": "ephemeral"}}]


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
    if not cfg.gcp_project_id:
        yield {
            "type": "error",
            "message": "PROVIDER=claude runs on Vertex and needs GCP_PROJECT_ID set "
            "in .env (Claude is not available via a Gemini API key).",
        }
        return
    client = AnthropicVertex(
        project_id=cfg.gcp_project_id, region=cfg.vertex_region)

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
            final = "".join(
                b.text for b in response.content if b.type == "text").strip()
            logger.info("Q=%r answered (stop_reason=%s)",
                        question, response.stop_reason)
            yield {"type": "answer", "text": final}
            return

        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            yield {"type": "tool_call", "name": block.name, "input": block.input}
            if block.name == "run_query":
                yield {"type": "sql", "sql": block.input.get("sql", "")}

            content, is_error, ui = execute_tool(block.name, dict(block.input))
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
