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


def test_format_cell_null_and_numbers():
    assert qf('QF.formatCell(null, "X")') == "—"
    assert qf('QF.formatCell(14882310, "BUFFER_GETS")') == "14,882,310"
    assert qf('QF.formatCell(412.6, "ELAPSED_S")') == "412.6"


def test_format_cell_keeps_id_columns_raw():
    assert qf('QF.formatCell(3912874411, "PLAN_HASH_VALUE")') == "3912874411"
    assert qf('QF.isCopyColumn("SQL_ID") && QF.isCopyColumn("plan_hash_value")') is True
    assert qf('QF.isCopyColumn("SID")') is False


def test_elapsed_column_detection():
    assert qf('QF.isElapsedColumn("ELAPSED_TIME") && QF.isElapsedColumn("elapsed_s")') is True
    assert qf('QF.isElapsedColumn("CPU_TIME")') is False


def test_sort_rows_numeric_desc_nulls_last():
    assert qf("QF.sortRows([[2],[null],[10],[1]], 0, -1)") == [[10], [2], [1], [None]]
    assert qf("QF.sortRows([[2],[null],[10]], 0, 1)") == [[2], [10], [None]]


def test_sort_rows_does_not_mutate_input():
    assert qf("(() => { const r = [[2],[1]]; QF.sortRows(r, 0, 1); return r; })()") == [[2], [1]]


def test_to_csv_quotes_special_characters():
    out = qf('QF.toCsv(["A","B"], [["x,y", "say \\"hi\\""], [null, "l1\\nl2"]])')
    assert out == 'A,B\r\n"x,y","say ""hi"""\r\n,"l1\nl2"'


def test_relative_time_buckets():
    assert qf("QF.relativeTime(0, 30e3)") == "just now"
    assert qf("QF.relativeTime(0, 2*60e3)") == "2 min ago"
    assert qf("QF.relativeTime(0, 3*3600e3)") == "3 hr ago"
    assert qf("QF.relativeTime(0, 30*3600e3)") == "yesterday"
    assert qf("QF.relativeTime(0, 72*3600e3)") == "3 days ago"


def test_clock_format():
    assert qf("QF.clock(3800)") == "00:03.8"
    assert qf("QF.clock(65000)") == "01:05.0"


def test_activity_line_and_live_step():
    assert qf('QF.activityLine({type:"sql", sql:"x"})') is None
    line = qf('QF.activityLine({type:"thinking", text:"a".repeat(300)})')
    assert line["kind"] == "think" and len(line["text"]) <= 201 and len(line["title"]) == 300
    assert qf('QF.activityLine({type:"result", row_count:5})') == {"kind": "tool", "text": "run_query → 5 rows"}
    assert qf('QF.activityLine({type:"tool_call", name:"describe_table", input:{name:"V$SQL"}})') == \
        {"kind": "tool", "text": 'describe_table({"name":"V$SQL"})'}
    assert qf('QF.activityLine({type:"tool_error", name:"run_query", message:"ORA-00942"})')["kind"] == "error"
    assert qf('QF.liveStep({type:"tool_call", name:"describe_table", input:{}})') == "describe_table…"
    assert qf('QF.liveStep({type:"status", message:"Thinking…"})') == "Thinking…"
    assert qf('QF.liveStep({type:"answer", text:"x"})') is None
