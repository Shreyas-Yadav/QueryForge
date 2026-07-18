"""FastAPI app: a streaming natural-language query endpoint + a minimal UI.

``POST /query`` runs the agent and streams progress as Server-Sent Events. The
synchronous agent generator is iterated by Starlette in a worker thread, so the
blocking Oracle / Vertex calls don't stall the event loop.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config, db, prompt
from ..runner import run_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("queryforge_audit.log")],
)

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    db.close_pool()


app = FastAPI(title="QueryForge", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


class QueryIn(BaseModel):
    question: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    try:
        db.ping()
        return {"status": "ok", "database": "reachable"}
    except Exception as e:  # noqa: BLE001
        return {"status": "degraded", "database": f"unreachable: {e}"}


class TargetIn(BaseModel):
    target: str


# Serialises target switches against each other. A switch tears down the pool
# mid-flight, so it must not interleave with another switch.
_switch_lock = threading.Lock()


def _target_status() -> dict[str, object]:
    """Current target plus a live reachability probe."""
    try:
        target = config.get_settings().db.target
    except ValueError as e:  # target selected but its settings are incomplete
        return {"target": config.get_settings().db_target, "reachable": False, "detail": str(e)}
    try:
        db.ping()
        return {"target": target, "reachable": True, "detail": "connected"}
    except Exception as e:  # noqa: BLE001 — surface why, don't crash the UI
        return {"target": target, "reachable": False, "detail": str(e)}


@app.get("/db-target")
def get_db_target() -> dict[str, object]:
    return {"targets": list(config.DB_TARGETS), **_target_status()}


@app.post("/db-target")
def set_db_target(payload: TargetIn) -> dict[str, object]:
    """Switch the active database and persist the choice to .env.

    Everything derived from the old database is torn down here: the connection
    pool and the cached schema overview baked into the agent's system prompt.
    """
    with _switch_lock:
        try:
            target = config.set_db_target(payload.target)
        except (ValueError, FileNotFoundError, OSError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        db.close_pool()
        prompt.clear_schema_cache()
        logging.getLogger(__name__).info("Switched database target to %s", target)
        return {"targets": list(config.DB_TARGETS), **_target_status()}


@app.post("/query")
def query(payload: QueryIn) -> StreamingResponse:
    def event_stream() -> Iterator[str]:
        try:
            for event in run_agent(payload.question):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:  # noqa: BLE001 — never break the SSE stream
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
