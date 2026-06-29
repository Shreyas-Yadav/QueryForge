"""Provider dispatcher: pick the Gemini or Claude agent based on config.

Both run on Google Vertex AI (GCP ADC auth). Switch with the ``PROVIDER`` env var.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .config import get_settings


def run_agent(question: str) -> Iterator[dict[str, Any]]:
    provider = get_settings().provider.lower()
    if provider == "claude":
        from .agent import run_agent as _run
    elif provider == "gemini":
        from .agent_gemini import run_agent as _run
    else:
        yield {"type": "error", "message": f"Unknown PROVIDER '{provider}' (use 'gemini' or 'claude')."}
        return
    yield from _run(question)
