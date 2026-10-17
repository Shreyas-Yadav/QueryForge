"""Smoke tests for the FastAPI app (no live DB/Vertex required)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from fastapi.testclient import TestClient

from queryforge.web.app import app

client = TestClient(app)


def test_index_serves_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "QueryForge" in r.text


def test_health_reports_degraded_without_db():
    # With no Oracle settings configured, health should report degraded, not crash.
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] in {"ok", "degraded"}


def test_index_has_login_form_and_no_target_switch():
    html = client.get("/").text
    for element_id in ('id="login-db"', 'id="login-user"', 'id="login-pw"', 'id="logout"'):
        assert element_id in html
    assert 'type="password"' in html
    assert "/db-target" not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_index_script_is_valid_javascript(tmp_path):
    html = client.get("/").text
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    path = tmp_path / "index.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_starting_the_app_in_tests_never_exports_traces():
    """A developer's .env may point at a live Phoenix; tests must not send to it."""
    from queryforge import observability

    with TestClient(app):
        assert observability._provider is None


def test_anthropic_instrumentor_matches_the_sdk_major_version():
    """Instrumentor 1.x supports anthropic 0.x; 2.x+ needs anthropic 1.x. Upgrade together."""
    from importlib.metadata import version

    instrumentor_major = int(version("openinference-instrumentation-anthropic").split(".")[0])
    sdk_major = int(version("anthropic").split(".")[0])
    assert (instrumentor_major >= 2) == (sdk_major >= 1)
