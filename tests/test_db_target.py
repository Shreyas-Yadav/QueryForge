"""Tests for switching the active database target at runtime.

The switch rewrites .env — a file holding live credentials that a human also
edits by hand — so these tests are mostly about proving nothing else in that file
is disturbed.
"""

from __future__ import annotations

import pytest

from queryforge import config

SAMPLE_ENV = """\
# QueryForge configuration
PROVIDER=gemini
GEMINI_API_KEY=secret-key-value

DB_TARGET=cloud

# --- cloud ---
CLOUD_ORACLE_USER=qf_readonly
CLOUD_ORACLE_PASSWORD=p@ss=word#with-punctuation
"""


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """An isolated .env, with the real os.environ left untouched afterwards."""
    path = tmp_path / ".env"
    path.write_text(SAMPLE_ENV, encoding="utf-8")
    monkeypatch.setattr(config, "ENV_PATH", path)
    monkeypatch.delenv("DB_TARGET", raising=False)
    yield path
    config.get_settings.cache_clear()


# --- rewriting the file ------------------------------------------------------


def test_switch_updates_only_the_db_target_line(env_file):
    config.set_db_target("local", env_path=env_file)
    after = env_file.read_text(encoding="utf-8")

    assert "DB_TARGET=local" in after
    assert "DB_TARGET=cloud" not in after
    # Everything else — comments, blank lines, secrets, punctuation — survives.
    assert after == SAMPLE_ENV.replace("DB_TARGET=cloud", "DB_TARGET=local")


def test_switch_preserves_credentials_verbatim(env_file):
    config.set_db_target("local", env_path=env_file)
    after = env_file.read_text(encoding="utf-8")

    assert "GEMINI_API_KEY=secret-key-value" in after
    assert "CLOUD_ORACLE_PASSWORD=p@ss=word#with-punctuation" in after


def test_switch_appends_when_db_target_absent(tmp_path):
    path = tmp_path / ".env"
    path.write_text("PROVIDER=gemini\n", encoding="utf-8")

    config.set_db_target("local", env_path=path)

    assert path.read_text(encoding="utf-8") == "PROVIDER=gemini\nDB_TARGET=local\n"


def test_switch_appends_newline_when_file_lacks_trailing_one(tmp_path):
    path = tmp_path / ".env"
    path.write_text("PROVIDER=gemini", encoding="utf-8")

    config.set_db_target("cloud", env_path=path)

    assert path.read_text(encoding="utf-8") == "PROVIDER=gemini\nDB_TARGET=cloud\n"


def test_commented_out_target_is_not_treated_as_the_assignment(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# DB_TARGET=local\nDB_TARGET=cloud\n", encoding="utf-8")

    config.set_db_target("local", env_path=path)

    assert path.read_text(encoding="utf-8") == "# DB_TARGET=local\nDB_TARGET=local\n"


def test_file_permissions_are_preserved(env_file):
    env_file.chmod(0o600)
    config.set_db_target("local", env_path=env_file)
    assert env_file.stat().st_mode & 0o777 == 0o600


# --- refusals ----------------------------------------------------------------


def test_unknown_target_rejected_before_touching_the_file(env_file):
    with pytest.raises(ValueError, match="Unknown target"):
        config.set_db_target("staging", env_path=env_file)

    assert env_file.read_text(encoding="utf-8") == SAMPLE_ENV


def test_missing_env_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="copy .env.example"):
        config.set_db_target("local", env_path=tmp_path / "nope.env")


# --- effect on settings ------------------------------------------------------


def test_switch_invalidates_cached_settings(env_file, monkeypatch):
    monkeypatch.chdir(env_file.parent)
    config.get_settings.cache_clear()
    assert config.get_settings().db_target == "cloud"

    config.set_db_target("local", env_path=env_file)

    assert config.get_settings().db_target == "local"


def test_switch_syncs_os_environ(env_file, monkeypatch):
    """A stale DB_TARGET env var would outrank the .env we just wrote."""
    monkeypatch.setenv("DB_TARGET", "cloud")

    config.set_db_target("local", env_path=env_file)

    import os

    assert os.environ["DB_TARGET"] == "local"


def test_endpoint_switch_clears_the_schema_cache(env_file, monkeypatch):
    """The prompt's schema overview must not survive a database switch."""
    from fastapi.testclient import TestClient

    from queryforge import prompt
    from queryforge.web import app as web_app

    monkeypatch.chdir(env_file.parent)
    cleared = []
    monkeypatch.setattr(prompt, "clear_schema_cache", lambda: cleared.append(True))
    monkeypatch.setattr(web_app.db, "close_pool", lambda: None)
    monkeypatch.setattr(web_app.db, "ping", lambda: None)

    r = TestClient(web_app.app).post("/db-target", json={"target": "local"})

    assert r.status_code == 200
    assert r.json()["target"] == "local"
    assert cleared == [True], "schema cache must be cleared on switch"


def test_endpoint_rejects_unknown_target(env_file, monkeypatch):
    from fastapi.testclient import TestClient

    from queryforge.web import app as web_app

    monkeypatch.chdir(env_file.parent)
    r = TestClient(web_app.app).post("/db-target", json={"target": "staging"})

    assert r.status_code == 400
    assert env_file.read_text(encoding="utf-8") == SAMPLE_ENV
