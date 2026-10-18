"""The single active database login (one local user, one login at a time).

Logging in or out swaps the connection pool in :mod:`queryforge.db` under one
lock, so concurrent requests never see a half-replaced login.
"""

from __future__ import annotations

import threading

from . import db
from .config import OracleProfile
from .connections import DatabaseEntry

_lock = threading.Lock()


def login(entry: DatabaseEntry, user: str, password: str) -> None:
    """Connect to ``entry`` as ``user``. Raises the driver error on failure,
    leaving any existing login untouched."""
    profile = OracleProfile(
        target=entry.name,
        user=user,
        password=password,
        dsn=entry.dsn,
        schema_name=entry.schema_name,
    )
    with _lock:
        db.replace_pool(profile)


def logout() -> None:
    with _lock:
        db.close_pool()


def status() -> dict[str, object]:
    """Who is logged in to what, plus a live reachability probe."""
    try:
        profile = db.active_profile()
    except db.NotLoggedIn:
        return {
            "logged_in": False,
            "database": None,
            "user": None,
            "reachable": False,
            "detail": "not logged in",
        }
    try:
        db.ping()
        reachable, detail = True, "connected"
    except Exception as e:  # noqa: BLE001 — surface why, don't crash the UI
        reachable, detail = False, str(e)
    return {
        "logged_in": True,
        "database": profile.target,
        "user": profile.user,
        "reachable": reachable,
        "detail": detail,
    }
