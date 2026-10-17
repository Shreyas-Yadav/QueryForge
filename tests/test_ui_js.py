"""Unit tests for the UI's pure ``QF`` helpers, run in Node (no browser, no DB)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from queryforge.web.app import app

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_client = TestClient(app)


def page_html() -> str:
    return _client.get("/").text


@pytest.fixture(scope="module")
def _script_path(tmp_path_factory) -> Path:
    script = page_html().split("<script>", 1)[1].split("</script>", 1)[0]
    path = tmp_path_factory.mktemp("ui") / "index.js"
    path.write_text(script, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _bind_script(_script_path):
    global _SCRIPT
    _SCRIPT = _script_path


_SCRIPT: Path | None = None


def qf(expr: str):
    """Evaluate a JS expression with `QF` in scope (Node) and return it as Python."""
    code = (
        f"const QF = require({json.dumps(str(_SCRIPT))});"
        f"const __v = ({expr});"
        "process.stdout.write(JSON.stringify(__v === undefined ? null : __v));"
    )
    result = subprocess.run(["node", "-e", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_qf_is_exported_to_node():
    assert qf("typeof QF") == "object"


def test_script_loads_in_node_without_a_dom():
    assert qf("1 + 1") == 2  # requiring the script must not touch `document`
