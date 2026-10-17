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
    assert qf('QF.isCopyColumn("EXECUTIONS")') is False


def test_oracle_identifier_columns_are_raw_and_copyable():
    for col in ["HASH_VALUE", "OLD_HASH_VALUE", "DBID", "FORCE_MATCHING_SIGNATURE",
                "EXACT_MATCHING_SIGNATURE", "SID", "SERIAL#", "SQL_EXEC_ID"]:
        assert qf(f'QF.isCopyColumn("{col}")') is True, col
    assert qf('QF.formatCell(3912874411, "HASH_VALUE")') == "3912874411"
    assert qf('QF.formatCell(1234567890, "dbid")') == "1234567890"


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
    assert qf('QF.liveStep({type:"tool_call", name:"describe_table", input:{}})') == "describe_table()"
    assert qf('QF.liveStep({type:"status", message:"Thinking…"})') == "Thinking…"
    assert qf('QF.liveStep({type:"answer", text:"x"})') is None


def test_store_falls_back_without_local_storage():
    assert qf('QF.store.get("qf.theme", "light")') == "light"  # Node has no localStorage
    assert qf('(QF.store.set("qf.x", 1), "ok")') == "ok"  # must not throw


def test_store_round_trips_through_local_storage():
    code = ('(() => { const m = {}; globalThis.localStorage = {getItem: k => k in m ? m[k] : null,'
            ' setItem: (k, v) => { m[k] = String(v); }};'
            ' QF.store.set("qf.theme", "dark"); return QF.store.get("qf.theme", "light"); })()')
    assert qf(code) == "dark"


def test_load_bookmarks_survives_corrupt_json():
    assert qf('QF.parseBookmarks("not json")') == []
    assert qf('QF.parseBookmarks([{question:"q", group:"G", lastRun:null}, {nope:1}, 5])') == [
        {"question": "q", "group": "G", "lastRun": None}
    ]


def test_add_bookmark_defaults_group_and_dedupes():
    out = qf('QF.addBookmark(QF.addBookmark([], " Top SQL ", ""), "Top SQL", "Waits")')
    assert out == [{"question": "Top SQL", "group": "Waits", "lastRun": None}]
    assert qf('QF.addBookmark([], "q", "  ")')[0]["group"] == "Saved"


def test_remove_and_touch_bookmark():
    assert qf('QF.removeBookmark([{question:"a",group:"S",lastRun:null}], "a")') == []
    assert qf('QF.touchBookmark([{question:"a",group:"S",lastRun:null}], "a", 5)')[0]["lastRun"] == 5
    assert qf('QF.touchBookmark([{question:"a",group:"S",lastRun:null}], "b", 5)')[0]["lastRun"] is None


def test_bookmark_helpers_do_not_mutate():
    assert qf('(() => { const l = [{question:"a",group:"S",lastRun:null}];'
              ' QF.touchBookmark(l, "a", 5); QF.removeBookmark(l, "a"); return l; })()') == [
        {"question": "a", "group": "S", "lastRun": None}
    ]


def test_group_bookmarks_filters_and_drops_empty_groups():
    out = qf('QF.groupBookmarks([{question:"Top SQL by CPU",group:"Top SQL",lastRun:null},'
             '{question:"Blocking sessions",group:"Waits",lastRun:null}], "cpu")')
    assert out == [{"group": "Top SQL", "items": [{"question": "Top SQL by CPU", "group": "Top SQL", "lastRun": None}]}]


def test_page_uses_plex_fonts_and_theme_tokens():
    html = page_html()
    assert "fonts.googleapis.com" in html and "IBM+Plex+Sans" in html and "IBM+Plex+Mono" in html
    assert '[data-theme="dark"]' in html


def test_try_examples_row_is_gone():
    assert 'class="examples"' not in page_html()


def test_shell_ids_exist():
    html = page_html()
    for i in ["sidebar-toggle", "conn", "reconnect", "theme-light", "theme-dark", "sidebar", "q", "star",
              "ask", "stop", "answer", "sql", "copy-sql", "result-meta", "rerun", "csv", "trunc",
              "result", "activity-sec", "activity-live", "activity-meta", "activity", "toast"]:
        assert f'id="{i}"' in html, i


def test_activity_is_collapsed_by_default():
    html = page_html()
    tag = html.split('id="activity-sec"', 1)[0].rsplit("<", 1)[1] + html.split('id="activity-sec"', 1)[1].split(">", 1)[0]
    assert tag.startswith("details") and " open" not in tag


def test_live_step_shows_short_tool_args():
    assert qf('QF.liveStep({type:"tool_call", name:"describe_table", input:{name:"V$SQL"}})') == 'describe_table("V$SQL")'
    step = qf('QF.liveStep({type:"tool_call", name:"run_query", input:{sql:"SELECT " + "x".repeat(100)}})')
    assert step.startswith('run_query("SELECT') and len(step) <= len("run_query()") + 41


def test_assistant_text_uses_a_known_activity_kind():
    assert qf('QF.activityLine({type:"assistant_text", text:"Let me check V$SQL."})') == \
        {"kind": "think", "text": "Let me check V$SQL."}


def test_parse_sse_event_skips_malformed_json():
    assert qf("QF.parseSseEvent('data: {\"type\":\"answer\",\"text\":\"ok\"}')") == {"type": "answer", "text": "ok"}
    assert qf("QF.parseSseEvent('data: {not json')") is None
    assert qf("QF.parseSseEvent('data: 5')") is None


def test_to_csv_neutralises_spreadsheet_formulas():
    out = qf('QF.toCsv(["A"], [["=HYPERLINK(1)"], ["+1"], ["@x"], ["-cmd"], [-5], ["ok"]])')
    assert out == "A\r\n'=HYPERLINK(1)\r\n'+1\r\n'@x\r\n'-cmd\r\n-5\r\nok"


def test_dark_theme_is_applied_before_the_page_paints():
    html = page_html()
    head = html.split("<body", 1)[0]
    assert "qf.theme" in head and "dataset.theme" in head


def test_markdown_inline_bold_italic_code():
    assert qf('QF.parseInline("a **B** *c* `d_e` f")') == [
        {"t": "text", "v": "a "}, {"t": "b", "v": "B"}, {"t": "text", "v": " "},
        {"t": "i", "v": "c"}, {"t": "text", "v": " "}, {"t": "code", "v": "d_e"}, {"t": "text", "v": " f"},
    ]


def test_markdown_leaves_identifiers_and_stray_stars_alone():
    # Oracle names use underscores and $; a lone * (SELECT *) or a * b is not emphasis.
    assert qf('QF.parseInline("V$SQL_PLAN and SELECT * FROM t and 2 * 3")') == [
        {"t": "text", "v": "V$SQL_PLAN and SELECT * FROM t and 2 * 3"}
    ]


def test_markdown_numbered_list_from_the_screenshot():
    md = "The database contains two tables:\\n1. **DEPARTMENTS**: Company departments.\\n2. **EMPLOYEES**: Employees."
    blocks = qf(f'QF.parseMarkdown("{md}")')
    assert blocks[0] == {"type": "p", "lines": [[{"t": "text", "v": "The database contains two tables:"}]]}
    assert blocks[1]["type"] == "ol" and blocks[1]["start"] == 1 and len(blocks[1]["items"]) == 2
    assert blocks[1]["items"][0][0] == {"t": "b", "v": "DEPARTMENTS"}


def test_markdown_bullets_headings_paragraphs_and_fences():
    md = "## Top SQL\\n\\n- a\\n* b\\n\\nline one\\nline two\\n\\n```sql\\nSELECT *\\nFROM v$sql\\n```"
    blocks = qf(f'QF.parseMarkdown("{md}")')
    assert [b["type"] for b in blocks] == ["h", "ul", "p", "pre"]
    assert blocks[0]["lines"] == [[{"t": "text", "v": "Top SQL"}]]
    assert len(blocks[1]["items"]) == 2
    assert len(blocks[2]["lines"]) == 2
    assert blocks[3]["text"] == "SELECT *\nFROM v$sql"


def test_markdown_unclosed_fence_keeps_the_text():
    assert qf('QF.parseMarkdown("```\\nSELECT 1")') == [{"type": "pre", "text": "SELECT 1"}]
