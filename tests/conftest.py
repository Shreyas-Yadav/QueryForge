"""Shared fixtures: a fake Oracle pool so login flows run without a database."""

from __future__ import annotations

import pytest

from queryforge import db


class FakeConn:
    def __init__(self, pool: "FakePool") -> None:
        self.pool = pool
        self.call_timeout = None
        self.current_schema = None

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def ping(self) -> None:
        if self.pool.ping_error is not None:
            raise self.pool.ping_error


class FakePool:
    def __init__(self, profile, ping_error: Exception | None = None) -> None:
        self.profile = profile
        self.ping_error = ping_error
        self.closed = False

    def acquire(self) -> FakeConn:
        return FakeConn(self)

    def close(self, force: bool = False) -> None:
        self.closed = True


class PoolFactory:
    """Stands in for ``db._connect`` and ``db._create_pool``.

    Set ``next_error`` to make the next login attempt fail. ``login_attempts``
    counts how many times a password was tried against the "database".
    """

    def __init__(self) -> None:
        self.created: list[FakePool] = []
        self.next_error: Exception | None = None
        self.login_attempts = 0

    def connect(self, profile) -> FakeConn:
        self.login_attempts += 1
        error, self.next_error = self.next_error, None
        if error is not None:
            raise error
        return FakeConn(FakePool(profile))

    def __call__(self, profile) -> FakePool:
        pool = FakePool(profile)
        self.created.append(pool)
        return pool


@pytest.fixture
def fake_pools(monkeypatch) -> PoolFactory:
    factory = PoolFactory()
    monkeypatch.setattr(db, "_create_pool", factory)
    monkeypatch.setattr(db, "_connect", factory.connect)
    return factory


@pytest.fixture(autouse=True)
def _reset_db_login():
    """Every test starts and ends logged out."""
    db.close_pool()
    yield
    db.close_pool()
