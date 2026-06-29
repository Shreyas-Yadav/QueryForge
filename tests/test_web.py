"""Smoke tests for the FastAPI app (no live DB/Vertex required)."""

from __future__ import annotations

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
