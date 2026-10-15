"""The databases offered on the login screen, read from ``databases.json``.

Each entry names a database and the Oracle users allowed to log in to it. The
file holds no secrets — passwords are typed at login and kept only in memory —
but it is still git-ignored because it describes your own machines.

The file is re-read on every call so edits apply without restarting the server.
"""

from __future__ import annotations

import json
from pathlib import Path

import oracledb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from .config import get_settings


class DatabasesFileError(Exception):
    """``databases.json`` is missing, is not valid JSON, or fails validation."""


class DatabaseEntry(BaseModel):
    """One connectable database: where it listens and who may log in to it."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1)
    host: str = Field(min_length=1)
    port: int = Field(1521, ge=1, le=65535, strict=True)
    service_name: str = Field(min_length=1)
    # "schema" shadows a BaseModel attribute, so it is stored as schema_name.
    schema_name: str | None = Field(None, alias="schema")
    users: list[str] = Field(min_length=1)

    @field_validator("users")
    @classmethod
    def _users_are_distinct(cls, users: list[str]) -> list[str]:
        if any(not u for u in users):
            raise ValueError("usernames must not be blank")
        folded = [u.upper() for u in users]
        if len(set(folded)) != len(folded):
            raise ValueError("duplicate usernames (Oracle usernames are case-insensitive)")
        return users

    @property
    def dsn(self) -> str:
        """Full TCP connect descriptor for this host/port/service."""
        return oracledb.makedsn(self.host, self.port, service_name=self.service_name)


_ENTRIES = TypeAdapter(list[DatabaseEntry])


def _describe(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
        for err in error.errors()
    )


def load_databases(path: Path | None = None) -> list[DatabaseEntry]:
    """Read and validate the databases file (``DATABASES_FILE`` by default)."""
    path = path or get_settings().databases_file
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise DatabasesFileError(
            f"{path} not found — copy databases.example.json to {path.name} and edit it."
        ) from e
    except json.JSONDecodeError as e:
        raise DatabasesFileError(f"{path} is not valid JSON: {e}") from e

    try:
        entries = _ENTRIES.validate_python(raw)
    except ValidationError as e:
        raise DatabasesFileError(f"{path} is invalid: {_describe(e)}") from e

    if not entries:
        raise DatabasesFileError(f"{path} lists no databases.")
    seen: set[str] = set()
    for entry in entries:
        key = entry.name.casefold()
        if key in seen:
            raise DatabasesFileError(f"{path} is invalid: duplicate database name '{entry.name}'.")
        seen.add(key)
    return entries


def find(name: str, path: Path | None = None) -> DatabaseEntry:
    """Look up an entry by name (case-insensitive). Raises ``KeyError`` if absent."""
    wanted = name.strip().casefold()
    for entry in load_databases(path):
        if entry.name.casefold() == wanted:
            return entry
    raise KeyError(name)
