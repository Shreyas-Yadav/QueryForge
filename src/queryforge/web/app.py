"""FastAPI app: a streaming natural-language query endpoint + a minimal UI.

``POST /query`` runs the agent and streams progress as Server-Sent Events. The
synchronous agent generator is iterated by Starlette in a worker thread, so the
blocking Oracle / Vertex calls don't stall the event loop.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import db
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
