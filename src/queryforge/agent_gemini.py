"""Gemini implementation of the QueryForge agent (Google Vertex AI).

Mirrors :func:`queryforge.agent.run_agent` — same yielded event shapes and the
same read-only tools — but drives Google's ``google-genai`` SDK with its native
function-calling format. The provider-neutral pieces are shared: tool execution
and specs from :mod:`queryforge.agent_core`, the system prompt from
:mod:`queryforge.prompt`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from google import genai
from google.genai import types

from .agent_core import MAX_TURNS, TOOL_SPECS, execute_tool
from .config import get_settings
from .prompt import system_prompt_text

logger = logging.getLogger("queryforge.audit")


def _tools() -> list[types.Tool]:
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=s["name"],
                    description=s["description"],
                    parameters_json_schema=s["schema"],
                )
                for s in TOOL_SPECS
            ]
        )
    ]


def run_agent(question: str) -> Iterator[dict[str, Any]]:
    """Drive the Gemini tool-calling loop, yielding the same events as the Claude agent."""
    cfg = get_settings()
    if cfg.gemini_api_key:
        # Google AI Studio (Developer API) — no GCP project / ADC needed.
        client = genai.Client(api_key=cfg.gemini_api_key)
    elif cfg.gcp_project_id:
        # Vertex AI via Application Default Credentials.
        client = genai.Client(
            vertexai=True, project=cfg.gcp_project_id, location=cfg.vertex_region
        )
    else:
        yield {
            "type": "error",
            "message": "No GEMINI_API_KEY set and no GCP_PROJECT_ID for Vertex. "
            "Set one in .env.",
        }
        return
    config = types.GenerateContentConfig(
        system_instruction=system_prompt_text(),
        tools=_tools(),
        temperature=0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part.from_text(text=question)])
    ]
    yield {"type": "status", "message": "Thinking…"}

    for _ in range(MAX_TURNS):
        try:
            response = client.models.generate_content(
                model=cfg.gemini_model, contents=contents, config=config
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Vertex (Gemini) request failed")
            yield {"type": "error", "message": f"Model request failed: {e}"}
            return

        candidate = response.candidates[0]
        parts = candidate.content.parts or []
        contents.append(candidate.content)

        function_calls = [p.function_call for p in parts if p.function_call]

        for part in parts:
            if getattr(part, "text", None) and part.text.strip():
                yield {"type": "assistant_text", "text": part.text}

        if not function_calls:
            final = "".join(p.text for p in parts if getattr(p, "text", None)).strip()
            logger.info("Q=%r answered (gemini)", question)
            yield {"type": "answer", "text": final}
            return

        response_parts: list[types.Part] = []
        for fc in function_calls:
            args = dict(fc.args or {})
            yield {"type": "tool_call", "name": fc.name, "input": args}
            if fc.name == "run_query":
                yield {"type": "sql", "sql": args.get("sql", "")}

            content, is_error, ui = execute_tool(fc.name, args)
            logger.info("Q=%r tool=%s input=%s error=%s", question, fc.name, args, is_error)

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
                yield {"type": "tool_error", "name": fc.name, "message": content}

            payload = {"error": content} if is_error else {"result": content}
            response_parts.append(
                types.Part.from_function_response(name=fc.name, response=payload)
            )

        contents.append(types.Content(role="tool", parts=response_parts))

    yield {"type": "error", "message": f"Gave up after {MAX_TURNS} steps without a final answer."}
