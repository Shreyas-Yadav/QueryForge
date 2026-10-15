"""Provider dispatcher: pick the Gemini or Claude agent based on config.

Both run on Google Vertex AI (GCP ADC auth). Switch with the ``PROVIDER`` env var.
Every run is wrapped in an AGENT trace span (see :mod:`queryforge.observability`).
"""

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
