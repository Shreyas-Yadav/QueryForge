"""Login / logout / session endpoints (no live database — pools are faked)."""

from __future__ import annotations

import json
import logging

import oracledb
import pytest
from fastapi.testclient import TestClient

from queryforge import config
from queryforge.web import app as app_module

client = TestClient(app_module.app)

DATABASES = [
    {
        "name": "AIWORLD (local)",
        "host": "localhost",
        "port": 1539,
        "service_name": "AIWORLD",
        "schema": "SHREYAS",
        "users": ["qf_readonly", "shreyas"],
    },
    {"name": "Scratch", "host": "localhost", "service_name": "FREEPDB1", "users": ["system"]},
]
PASSWORD = "s3cret-pw"


@pytest.fixture
def databases_file(tmp_path, monkeypatch):
    path = tmp_path / "databases.json"
    path.write_text(json.dumps(DATABASES), encoding="utf-8")
    monkeypatch.setenv("DATABASES_FILE", str(path))
    config.get_settings.cache_clear()
    yield path
    config.get_settings.cache_clear()


def login(**overrides):
    body = {"database": "AIWORLD (local)", "user": "shreyas", "password": PASSWORD, **overrides}
    return client.post("/login", json=body)


# --- logged out ---------------------------------------------------------------


def test_logged_out_by_default(databases_file):
    s = client.get("/session").json()
    assert s["logged_in"] is False
    assert s["user"] is None


def test_query_requires_login(databases_file):
    r = client.post("/query", json={"question": "anything"})
    assert r.status_code == 401


def test_health_degraded_when_logged_out(databases_file):
    assert client.get("/health").json()["status"] == "degraded"


# --- /databases ---------------------------------------------------------------


def test_databases_lists_entries(databases_file):
    r = client.get("/databases")
    assert r.status_code == 200
    dbs = r.json()["databases"]
    assert [d["name"] for d in dbs] == ["AIWORLD (local)", "Scratch"]
    assert dbs[0] == {
        "name": "AIWORLD (local)",
        "host": "localhost",
        "port": 1539,
        "service_name": "AIWORLD",
        "users": ["qf_readonly", "shreyas"],
    }
    assert dbs[1]["port"] == 1521


def test_databases_file_invalid_returns_500(databases_file):
    databases_file.write_text("{not json", encoding="utf-8")
    r = client.get("/databases")
    assert r.status_code == 500
    assert str(databases_file) in r.json()["detail"]


def test_databases_file_is_reread(databases_file):
    databases_file.write_text(json.dumps(DATABASES[:1]), encoding="utf-8")
    assert len(client.get("/databases").json()["databases"]) == 1


# --- /login -------------------------------------------------------------------


def test_login_success(databases_file, fake_pools):
    r = login()

    assert r.status_code == 200
    body = r.json()
    assert body["logged_in"] is True
    assert body["database"] == "AIWORLD (local)"
    assert body["user"] == "shreyas"
    assert body["reachable"] is True

    profile = fake_pools.created[0].profile
    assert profile.dsn == oracledb.makedsn("localhost", 1539, service_name="AIWORLD")
    assert profile.schema_name == "SHREYAS"
    assert profile.password == PASSWORD
    assert client.get("/session").json()["user"] == "shreyas"


def test_user_match_is_case_insensitive(databases_file, fake_pools):
    r = login(user="SHREYAS")
    assert r.status_code == 200
    assert r.json()["user"] == "shreyas"


def test_invalid_credentials_returns_401(databases_file, fake_pools):
    fake_pools.next_error = oracledb.DatabaseError(
        "ORA-01017: invalid username/password; logon denied"
    )
    r = login()

    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid username or password"
    assert fake_pools.created == []
    assert client.get("/session").json()["logged_in"] is False


def test_unreachable_returns_502(databases_file, fake_pools):
    fake_pools.next_error = oracledb.OperationalError("DPY-6005: cannot connect to database")
    r = login()

    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "localhost:1539/AIWORLD" in detail
    assert "DPY-6005" in detail


def test_other_oracle_error_shows_driver_message(databases_file, fake_pools):
    fake_pools.next_error = oracledb.DatabaseError("ORA-28000: the account is locked")
    r = login()

    assert r.status_code == 502
    assert "ORA-28000" in r.json()["detail"]


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"database": "Nope"}, "Unknown database"),
        ({"user": "system"}, "not configured"),
        ({"password": ""}, "Password is required"),
    ],
)
def test_bad_requests_return_400_without_connecting(databases_file, fake_pools, overrides, fragment):
    r = login(**overrides)
    assert r.status_code == 400
    assert fragment in r.json()["detail"]
    assert fake_pools.created == []


def test_login_with_broken_file_returns_500(databases_file, fake_pools):
    databases_file.write_text("[]", encoding="utf-8")
    assert login().status_code == 500


def test_failed_login_keeps_previous_session(databases_file, fake_pools):
    assert login().status_code == 200
    fake_pools.next_error = oracledb.DatabaseError("ORA-01017: invalid username/password")

    assert login(user="qf_readonly", password="wrong").status_code == 401

    s = client.get("/session").json()
    assert s["logged_in"] is True
    assert s["user"] == "shreyas"
    assert fake_pools.created[0].closed is False


def test_relogin_replaces_previous_pool(databases_file, fake_pools):
    login()
    assert login(database="Scratch", user="system").status_code == 200

    assert fake_pools.created[0].closed is True
    s = client.get("/session").json()
    assert (s["database"], s["user"]) == ("Scratch", "system")


# --- /logout ------------------------------------------------------------------


def test_logout(databases_file, fake_pools):
    login()

    r = client.post("/logout")

    assert r.json() == {"logged_in": False}
    assert fake_pools.created[0].closed is True
    assert client.get("/session").json()["logged_in"] is False
    assert client.post("/query", json={"question": "x"}).status_code == 401


# --- audit + query ------------------------------------------------------------


def test_password_never_logged(databases_file, fake_pools, caplog):
    caplog.set_level(logging.DEBUG)
    login()
    fake_pools.next_error = oracledb.DatabaseError("ORA-01017: invalid username/password")
    login(password="other-pw")
    client.post("/logout")

    assert PASSWORD not in caplog.text
    assert "other-pw" not in caplog.text
    assert "outcome=success" in caplog.text
    assert "outcome=invalid credentials" in caplog.text
    assert "logout" in caplog.text


def test_query_streams_when_logged_in(databases_file, fake_pools, monkeypatch):
    monkeypatch.setattr(
        app_module, "run_agent", lambda q: iter([{"type": "answer", "text": "hi"}])
    )
    login()
    r = client.post("/query", json={"question": "x"})
    assert r.status_code == 200
    assert '"answer"' in r.text
