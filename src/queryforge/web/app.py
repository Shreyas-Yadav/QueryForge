"""FastAPI app: a streaming natural-language query endpoint + a minimal UI.

``POST /query`` runs the agent and streams progress as Server-Sent Events. The
synchronous agent generator is iterated by Starlette in a worker thread, so the
blocking Oracle / Vertex calls don't stall the event loop.

The app is single-user and local: ``POST /login`` connects with the database,
user and password chosen on the login screen and holds that one login in memory
for every request until ``POST /logout`` or a restart. There are no cookies —
keep the server bound to 127.0.0.1.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import oracledb
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import connections, db, session
from ..runner import run_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("queryforge_audit.log")],
)

_STATIC_DIR = Path(__file__).parent / "static"
audit = logging.getLogger("queryforge.audit")


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


class LoginIn(BaseModel):
    database: str
    user: str
    password: str = Field(repr=False)


def _load_databases() -> list[connections.DatabaseEntry]:
    try:
        return connections.load_databases()
    except connections.DatabasesFileError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/databases")
def list_databases() -> dict[str, object]:
    return {
        "databases": [
            {
                "name": e.name,
                "host": e.host,
                "port": e.port,
                "service_name": e.service_name,
                "users": e.users,
            }
            for e in _load_databases()
        ]
    }


@app.get("/session")
def get_session() -> dict[str, object]:
    return session.status()


def _audit_login(database: str, user: str, outcome: str) -> None:
    audit.info("login database=%r user=%r outcome=%s", database, user, outcome)


@app.post("/login")
def login(payload: LoginIn) -> dict[str, object]:
    """Connect as the chosen user. The password is held in memory only."""
    _load_databases()  # surface a broken databases.json as 500, not "unknown database"
    try:
        entry = connections.find(payload.database)
    except KeyError:
        _audit_login(payload.database, payload.user, "rejected")
        raise HTTPException(
            status_code=400, detail=f"Unknown database '{payload.database}'"
        ) from None

    wanted = payload.user.strip().upper()
    user = next((u for u in entry.users if u.upper() == wanted), None)
    if user is None:
        _audit_login(entry.name, payload.user, "rejected")
        raise HTTPException(
            status_code=400,
            detail=f"User '{payload.user}' is not configured for '{entry.name}'",
        )
    if not payload.password:
        _audit_login(entry.name, user, "rejected")
        raise HTTPException(status_code=400, detail="Password is required")

    try:
        session.login(entry, user, payload.password)
    except oracledb.Error as e:
        if "ORA-01017" in str(e):
            _audit_login(entry.name, user, "invalid credentials")
            raise HTTPException(status_code=401, detail="Invalid username or password") from None
        _audit_login(entry.name, user, "unreachable")
        raise HTTPException(
            status_code=502,
            detail=f"Could not connect to {entry.host}:{entry.port}/{entry.service_name}: {e}",
        ) from None

    _audit_login(entry.name, user, "success")
    return session.status()


@app.post("/logout")
def logout() -> dict[str, bool]:
    try:
        profile = db.active_profile()
        audit.info("logout database=%r user=%r", profile.target, profile.user)
    except db.NotLoggedIn:
        pass
    session.logout()
    return {"logged_in": False}


@app.post("/query")
def query(payload: QueryIn) -> StreamingResponse:
    try:
        db.active_profile()
    except db.NotLoggedIn as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

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
