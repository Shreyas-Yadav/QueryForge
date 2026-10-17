# UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restyle and extend the single-page web UI into a responsive, light/dark "monitoring tool" with a saved-queries sidebar, collapsible Activity, and 14 UX improvements, without touching the backend.

**Architecture:** Everything stays in `src/queryforge/web/static/index.html` (one `<style>`, one `<script>`). The script is split into two layers. A **pure layer** is a `QF` object of side-effect-free helpers (formatting, sorting, CSV, bookmarks, activity lines); it is exported to Node with `module.exports` so pytest can unit-test it through `node`. A **DOM layer** holds the existing top-level functions (`showApp`, `handleEvent`, `ask`, …), now run only when `document` exists. Browser-level behavior is verified with Playwright at fixed widths, using fake SSE events fed through `handleEvent`.

**Tech Stack:** HTML/CSS/vanilla JS (no build step), IBM Plex via Google Fonts, pytest + Node 24 (`node` already used by `tests/test_web.py`), Playwright MCP for visual checks.

**Spec:** `docs/superpowers/specs/2026-10-17-ui-redesign-design.md`. The visual reference is `docs/superpowers/specs/2026-10-17-ui-redesign-mockup.html`; copy CSS tokens and component styles from it rather than re-inventing them.

**Deviations from spec (deliberate, small):**
- Click-to-copy applies to **any** column whose name matches `/(^|_)ID$/i` or `/PLAN_HASH/i`, not only the first column. ASH/AWR queries often put `SQL_ID` second.
- Copyable (ID / plan-hash) columns are **not** thousands-formatted. `PLAN_HASH_VALUE` arrives as a JSON number, and `3,912,874,411` would be wrong to paste.

## Global Constraints

- Only `src/queryforge/web/static/index.html` and tests change. No Python source changes, no new endpoints, no new event types.
- Exactly one `<script>` block (`tests/test_web.py::test_index_script_is_valid_javascript` reads the first one).
- Fonts: `IBM Plex Sans` (400/500/600) and `IBM Plex Mono` (400/500) from `fonts.googleapis.com`, with fallbacks `-apple-system, "Segoe UI", system-ui, sans-serif` / `ui-monospace, SFMono-Regular, Menlo, monospace`.
- Theme tokens on `:root` (light) and `[data-theme="dark"]` on `<html>`, values copied from the mockup.
- localStorage keys are prefixed `qf.`: `qf.theme` (`"light"|"dark"`), `qf.sidebar` (`"open"|"collapsed"`), `qf.bookmarks` (JSON array), `qf.lastQuestion` (string). Every access goes through `QF.store` (try/catch, falls back silently).
- Answer text and every server-provided string are inserted with `textContent`, never `innerHTML`.
- Breakpoints: ≥1100px full layout; 900–1099px sidebar starts collapsed and expands as an overlay; <900px Answer/SQL stack, header hides "Database"/"User" labels, sidebar becomes an off-canvas drawer with a backdrop; <600px 16px gutters, Run/Stop on their own row, header collapses to brand + connection dot + menu (theme, read-only tag, Log out), no horizontal page scroll.
- Touch targets ≥36px; under `@media (hover: none)` the bookmark × is always visible.
- Run tests with `uv run pytest`.

## Review Focus

1. **A result with 30+ columns or very long CLOB text** (e.g. `SQL_FULLTEXT`). Expected: only the table scrolls, the page never scrolls sideways, and cells don't wrap. Test: Task 5 `test_results_table_scrolls_inside_panel_at_390px` (Playwright check).
2. **Numeric ID columns** (`PLAN_HASH_VALUE`, `SQL_ID` not in first position). Expected: copyable and shown raw, not `3,912,874,411`. Test: Task 2 `test_format_cell_keeps_id_columns_raw`.
3. **A CSV export containing commas, quotes or newlines in SQL text.** Expected: opens correctly in Excel. Test: Task 2 `test_to_csv_quotes_special_characters`.
4. **localStorage unavailable** (private window / blocked) **or corrupted JSON in `qf.bookmarks`.** Expected: the page still works with an empty list. Test: Task 3 `test_load_bookmarks_survives_corrupt_json` and `test_store_falls_back_without_local_storage`.
5. **Pressing Stop, or a 401 mid-stream, then running again.** Expected: no stale timer, the title resets, Run is re-enabled, and no events from the old run leak into the new one. Test: Task 6 Playwright check `stop then rerun`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/queryforge/web/static/index.html` | Whole UI: tokens/styles, markup, `QF` pure helpers, DOM layer |
| `tests/test_ui_js.py` (new) | Runs `QF` helpers in Node and asserts results; static checks on the HTML |
| `tests/test_web.py` | Update the `localStorage`-forbidden assertion (the spec now requires localStorage) |

Test harness used by every `tests/test_ui_js.py` test (Task 1 creates it):

```python
def qf(expr: str):
    """Evaluate a JS expression with `QF` in scope (Node) and return it as Python."""
```

plus `page_html() -> str` (returns `TestClient(app).get("/").text`). `qf` writes the page's `<script>` to a temp `.js` file and runs `node -e "const QF = require(<file>); process.stdout.write(JSON.stringify(<expr>))"`. It skips when `node` is missing, like `test_web.py`.

---

### Task 1: Split the script into a testable `QF` layer

**Files:**
- Modify: `src/queryforge/web/static/index.html` (the `<script>` block)
- Create: `tests/test_ui_js.py`
- Modify: `tests/test_web.py:30-37`

**Interfaces:**
- Produces: global `QF` object; `module.exports = QF` when `typeof module !== "undefined"`; the DOM bootstrap runs only when `typeof document !== "undefined"`. Existing DOM functions (`showApp`, `showLogin`, `handleEvent`, `ask`, `loadSession`) stay top-level so Playwright can call them.
- Produces: `qf(expr)` pytest helper in `tests/test_ui_js.py`.

- [ ] **Step 1: Write the failing tests**

```python
def test_qf_is_exported_to_node():
    assert qf("typeof QF") == "object"

def test_script_loads_in_node_without_a_dom():
    assert qf("1 + 1") == 2   # requiring the script must not touch `document`
```

In `tests/test_web.py::test_index_has_login_form_and_no_target_switch`, delete `assert "localStorage" not in html`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ui_js.py -v`
Expected: FAIL (`ReferenceError: document is not defined` or `QF is not defined`).

- [ ] **Step 3: Restructure the script**

Add `const QF = {};` at the top. Wrap all code that touches `document`/`window` in `function boot() { … }` and call it via `if (typeof document !== "undefined") boot();`. Functions that Playwright or inline handlers call must remain reachable as globals: declare them at top level and only *invoke* DOM code inside `boot`. End with `if (typeof module !== "undefined") module.exports = QF;`.

- [ ] **Step 4: Run all tests**

Run: `uv run pytest tests/test_ui_js.py tests/test_web.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/queryforge/web/static/index.html tests/test_ui_js.py tests/test_web.py
git commit -m "Split UI script into a Node-testable QF layer"
```

---

### Task 2: Pure helpers for results and activity

**Files:**
- Modify: `src/queryforge/web/static/index.html` (`QF` section)
- Test: `tests/test_ui_js.py`

**Interfaces:**
- Consumes: `QF`, `qf()` from Task 1.
- Produces (all on `QF`):
  - `isCopyColumn(name: string) -> boolean`: `/(^|_)ID$/i` or `/PLAN_HASH/i`
  - `isElapsedColumn(name: string) -> boolean`: `/^ELAPSED/i`
  - `formatCell(value: any, column: string) -> string`: `null`/`undefined` → `"—"`; copy column → `String(value)`; number → `toLocaleString("en-US", {maximumFractionDigits: 3})`; otherwise `String(value)`
  - `sortRows(rows: any[][], col: number, dir: 1|-1) -> any[][]`: returns a new array; nulls always last; numbers compared numerically, others with `localeCompare`
  - `toCsv(columns: string[], rows: any[][]) -> string`: RFC 4180; quote only fields containing `,` `"` `\n` `\r`; double inner quotes; null → empty; `\r\n` line endings
  - `relativeTime(thenMs: number, nowMs: number) -> string`: `<60s` "just now"; `<60min` "N min ago"; `<24h` "N hr ago"; `<48h` "yesterday"; else "N days ago"
  - `clock(ms: number) -> string`: elapsed since run start as `mm:ss.s`, e.g. `3800` → `"00:03.8"`
  - `activityLine(ev: object) -> {kind: string, text: string, title?: string} | null`: kinds `status | think | text | tool | error`; `thinking` text cut at 200 chars with full text in `title`; `tool_call` → `name(args)` with args JSON cut at 160 chars; `result` → `{kind:"tool", text:"run_query → N rows"}`; `sql`, `answer`, `done` → `null`
  - `liveStep(ev: object) -> string | null`: `status` → message; `tool_call` → `name + "…"`; `thinking` → `"thinking…"`; others `null`

- [ ] **Step 1: Write the failing tests**

```python
def test_format_cell_null_and_numbers():
    assert qf('QF.formatCell(null, "X")') == "—"
    assert qf('QF.formatCell(14882310, "BUFFER_GETS")') == "14,882,310"
    assert qf('QF.formatCell(412.6, "ELAPSED_S")') == "412.6"

def test_format_cell_keeps_id_columns_raw():
    assert qf('QF.formatCell(3912874411, "PLAN_HASH_VALUE")') == "3912874411"
    assert qf('QF.isCopyColumn("SQL_ID") && QF.isCopyColumn("plan_hash_value")') is True
    assert qf('QF.isCopyColumn("SID")') is False

def test_sort_rows_numeric_desc_nulls_last():
    assert qf('QF.sortRows([[2],[null],[10],[1]], 0, -1)') == [[10],[2],[1],[None]]
    assert qf('QF.sortRows([[2],[null],[10]], 0, 1)') == [[2],[10],[None]]

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
    assert qf('QF.liveStep({type:"tool_call", name:"describe_table", input:{}})') == "describe_table…"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ui_js.py -v`
Expected: FAIL (`QF.formatCell is not a function`, …).

- [ ] **Step 3: Implement the helpers listed under Interfaces on `QF`.**

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ui_js.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/queryforge/web/static/index.html tests/test_ui_js.py
git commit -m "Add pure UI helpers for formatting, sorting, CSV and activity"
```

---

### Task 3: Storage and saved-query helpers

**Files:**
- Modify: `src/queryforge/web/static/index.html` (`QF` section)
- Test: `tests/test_ui_js.py`

**Interfaces:**
- Produces (on `QF`):
  - `store.get(key: string, fallback: any) -> any` / `store.set(key: string, value: any) -> void`: JSON-encode, use `globalThis.localStorage`, swallow every exception (get returns `fallback`)
  - Bookmark type: `{question: string, group: string, lastRun: number | null}`
  - `parseBookmarks(raw: any) -> Bookmark[]`: accepts the stored value; drops anything malformed; returns `[]` for non-arrays
  - `addBookmark(list, question: string, group: string) -> Bookmark[]`: trims both; empty group → `"Saved"`; replaces an existing item with the same question (exact match after trim)
  - `removeBookmark(list, question: string) -> Bookmark[]`
  - `touchBookmark(list, question: string, nowMs: number) -> Bookmark[]`: sets `lastRun` on an exact match; unchanged list otherwise
  - `groupBookmarks(list, filter: string) -> {group: string, items: Bookmark[]}[]`: case-insensitive substring filter on the question; groups ordered by first appearance; empty groups omitted
  - All list helpers return new arrays (no mutation)

- [ ] **Step 1: Write the failing tests**

```python
def test_store_falls_back_without_local_storage():
    assert qf('QF.store.get("qf.theme", "light")') == "light"   # Node has no localStorage
    assert qf('(QF.store.set("qf.x", 1), "ok")') == "ok"        # must not throw

def test_load_bookmarks_survives_corrupt_json():
    assert qf('QF.parseBookmarks("not json")') == []
    assert qf('QF.parseBookmarks([{question:"q", group:"G", lastRun:null}, {nope:1}, 5])') == \
        [{"question": "q", "group": "G", "lastRun": None}]

def test_add_bookmark_defaults_group_and_dedupes():
    out = qf('QF.addBookmark(QF.addBookmark([], " Top SQL ", ""), "Top SQL", "Waits")')
    assert out == [{"question": "Top SQL", "group": "Waits", "lastRun": None}]

def test_remove_and_touch_bookmark():
    assert qf('QF.removeBookmark([{question:"a",group:"S",lastRun:null}], "a")') == []
    assert qf('QF.touchBookmark([{question:"a",group:"S",lastRun:null}], "a", 5)')[0]["lastRun"] == 5

def test_group_bookmarks_filters_and_drops_empty_groups():
    out = qf('QF.groupBookmarks([{question:"Top SQL by CPU",group:"Top SQL",lastRun:null},'
             '{question:"Blocking sessions",group:"Waits",lastRun:null}], "cpu")')
    assert out == [{"group": "Top SQL", "items": [{"question": "Top SQL by CPU", "group": "Top SQL", "lastRun": None}]}]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ui_js.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the helpers listed under Interfaces.**

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ui_js.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/queryforge/web/static/index.html tests/test_ui_js.py
git commit -m "Add saved-query and storage helpers"
```

---

### Task 4: Visual shell, themes and fonts

**Files:**
- Modify: `src/queryforge/web/static/index.html` (`<head>`, `<style>`, markup, DOM layer)
- Test: `tests/test_ui_js.py`

**Interfaces:**
- Consumes: `QF.store` (Task 3).
- Produces markup IDs later tasks use:
  - header: `#sidebar-toggle`, `#conn` (tag), `#reconnect`, `#theme-light`, `#theme-dark`, `#logout`
  - layout: `#sidebar`, `#main`
  - question box: `#q`, `#star`, `#ask` (Run), `#stop`
  - panels: `#answer`, `#sql`, `#copy-sql`, `#result-sec`, `#result-meta`, `#rerun`, `#csv`, `#trunc`, `#result`, `#activity-sec` (`<details>`), `#activity-live`, `#activity-meta`, `#activity`
  - feedback: `#toast`
  - Existing login IDs (`login-db`, `login-user`, `login-pw`, `login-btn`, `login-error`) are unchanged.
- Produces: `setTheme(theme: "light"|"dark") -> void`. It sets `document.documentElement.dataset.theme`, updates the toggle state and persists `qf.theme`. It runs on boot from the stored value, defaulting to `"light"`.

- [ ] **Step 1: Write the failing static tests**

```python
def test_page_uses_plex_fonts_and_theme_tokens():
    html = page_html()   # helper: TestClient(app).get("/").text
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ui_js.py -v`
Expected: FAIL.

- [ ] **Step 3: Build the shell**

Replace the style block and markup with the mockup's Classic layout: header, sidebar placeholder (filled in Task 7), question row, Answer and Generated SQL side by side, Results, and `<details id="activity-sec">` with no `open` attribute. Remove the "Try:" row and its click handler. Restyle the login card with the same tokens. Header connection display comes from `showApp(s)`: `#conn` is `● CONNECTED` with class `g` when `s.reachable`; otherwise use the disconnected styling from Task 6, or just set class `r` for now.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -v`
Expected: PASS.

- [ ] **Step 5: Visual check (Playwright)**

Start `uv run uvicorn queryforge.web.app:app --port 8765` (no DB needed). Open `http://127.0.0.1:8765`. Use evaluate to run `showApp({user:"shreyas", database:"ORCLPDB", reachable:true, detail:"connected"})`. Screenshot at 1440px in light, then call `setTheme("dark")` and screenshot again. Compare with the mockup. Expected: the same header/panels look, Activity collapsed, no console errors.

- [ ] **Step 6: Commit**

```bash
git add src/queryforge/web/static/index.html tests/test_ui_js.py
git commit -m "Restyle UI as light/dark monitoring console with IBM Plex"
```

---

### Task 5: Results, SQL and answer panels

**Files:**
- Modify: `src/queryforge/web/static/index.html` (DOM layer + styles)

**Interfaces:**
- Consumes: `QF.formatCell`, `QF.isCopyColumn`, `QF.isElapsedColumn`, `QF.sortRows`, `QF.toCsv` (Task 2); panel IDs (Task 4).
- Produces:
  - `renderTable(columns: string[], rows: any[][]) -> void`. It keeps `{columns, rows, sortCol, sortDir}` in module state so headers can re-sort; a new result resets the sort.
  - `toast(message: string) -> void`. Shown about 1.4s.
  - `flashButton(btn: HTMLElement, label: string) -> void`. Swaps the label for about 1.4s.
  - `currentRun` state object, shared with Task 6: `{startedAt: number, sqlAt: number|null, controller: AbortController|null, timer: number|null}`.

- [ ] **Step 1: Implement the table**

The table sits in a `.tablewrap` (overflow auto, max-height 420px). It has a sticky `<thead>` and a sticky first column, cells use `white-space: nowrap`, and numeric cells are right-aligned with `tabular-nums`. NULL cells show "—" with the muted class. Clicking a header calls `QF.sortRows` (desc first, toggle on repeat; arrow on the active column). Copy columns use accent mono styling; clicking one copies the value with `navigator.clipboard.writeText` (in try/catch) and calls `toast("Copied " + v)`. Elapsed columns get a bar of width `round(v / colMax * 80)`, at least 4px.

- [ ] **Step 2: Wire the `result` event**

- `#result-meta` shows `N rows · M columns · X s · Ran HH:MM:SS on <db>`:
  - X is `(now - currentRun.sqlAt) / 1000` with one decimal, or `(now - startedAt)` if no `sql` event arrived.
  - `<db>` comes from the session database name.
- `#trunc` shows "Showing the first N rows. The result was truncated; narrow the question to see the rest." when `ev.truncated`, and is hidden otherwise.

- [ ] **Step 3: Wire the buttons**

- `#copy-sql`: copies the SQL text, then `flashButton(btn, "Copied ✓")`.
- `#csv`: downloads `queryforge-results.csv` built with `QF.toCsv` from the *currently sorted* rows, then `flashButton(btn, "Downloaded ✓")`.
- `#rerun`: calls `ask(q.value.trim())`.

- [ ] **Step 4: Answer panel**

`answer` → `#answer.textContent`. An `error` event, or a finished run that had a `tool_error` but no `answer`, renders the red notice. It's built with DOM nodes: a bold "Query failed" line plus the message as text.

- [ ] **Step 5: Playwright check, `test_results_table_scrolls_inside_panel_at_390px`**

Call `showApp(...)`, then `handleEvent` with `{type:"sql", sql:"SELECT …"}` and a `result` that has 30 columns (one named `PLAN_HASH_VALUE` with value 3912874411, one named `SQL_FULLTEXT` with 2,000 characters, one `ELAPSED_S`, plus some `null`s) and `truncated:true`. Check:
- At 1440px and at 390px, `document.documentElement.scrollWidth <= innerWidth`.
- `.tablewrap` has `scrollWidth > clientWidth`.
- The plan hash text is `3912874411`.
- Clicking the `ELAPSED_S` header puts the largest value first.
- The truncation strip is visible.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest -v`. Expected: PASS.

```bash
git add src/queryforge/web/static/index.html
git commit -m "Scrollable sortable results with copy, CSV, re-run and truncation notice"
```

---

### Task 6: Run lifecycle, Activity, errors, connection and keyboard

**Files:**
- Modify: `src/queryforge/web/static/index.html` (DOM layer)

**Interfaces:**
- Consumes: `QF.activityLine`, `QF.liveStep`, `QF.clock`, `QF.store` (Tasks 2–3); `currentRun`, `toast` (Task 5).
- Produces: `ask(question: string) -> Promise<void>`, `stopRun() -> void`, `setRunning(on: boolean) -> void`, `setConnection(reachable: boolean, detail: string) -> void`. Task 7 calls `onRunStart(question)`, a hook defined here that does nothing until Task 7 fills it in.

- [ ] **Step 1: `ask()` lifecycle**

- **Starting a run:**
  - If a previous run is still going, abort it first.
  - Create an `AbortController` and pass its `signal` to `fetch`.
  - Store the question as `qf.lastQuestion` and call `onRunStart(question)`.
  - Clear `#activity`, set `#activity-sec.open = false`, and show skeletons in `#answer` and `#result`.
  - `setRunning(true)`.
- **While running:**
  - A 100ms interval sets `#ask` to a spinner + `Running · X.Xs`.
  - `document.title` is `● Running – QueryForge`.
  - `#stop` is visible.
- **Each event:**
  - Ignore it if its run's controller is no longer `currentRun.controller`, so stale events don't leak into a new run.
  - Append `QF.activityLine(ev)` as `[clock] kind text` (when not null).
  - Set `#activity-live` to `· ` + `QF.liveStep(ev)` (when not null).
- **On `done` or a stream end:**
  - `setRunning(false)`: clear the interval, restore "Run", reset the title, hide Stop.
  - Clear `#activity-live` and set `#activity-meta` to `N steps · X.X s`.

- [ ] **Step 2: Stop**

`stopRun()` aborts the controller and calls `setRunning(false)`. `#answer` shows a neutral "Run stopped" notice and Results hide. Activity gets the line `[clock] stop Cancelled by user`. The `AbortError` from `fetch` must not be reported as an error.

- [ ] **Step 3: Connection state**

- `setConnection(reachable, detail)`:
  - Toggles `#conn` between `● CONNECTED` (`g`) and `● DISCONNECTED` (`r`).
  - Puts `detail` in the `title` tooltip.
  - Shows or hides `#reconnect`.
  - Disables `#ask` while disconnected, with the title "Reconnect to run queries".
- `showApp` calls it.
- A network failure in `ask` (not an abort) calls `setConnection(false, String(err))`.
- A 401 keeps today's behavior (show login + message).
- `#reconnect` re-fetches `/session`:
  - `logged_in: false` → `showLogin()`.
  - Otherwise → `setConnection(s.reachable, s.detail)`.

- [ ] **Step 4: Keyboard**

- ⌘K / Ctrl+K: focus and select `#q`.
- Enter: submits the form (already the case).
- ↑ in `#q`: puts in `QF.store.get("qf.lastQuestion", "")`.
- Esc while running: `stopRun()`.
- `#q` shows a `⌘K` hint when blurred and `↵` when focused.

- [ ] **Step 5: Playwright checks**

Drive the UI by stubbing `window.fetch` for `/query` with a `ReadableStream` that emits SSE events on timers.

- **During a run:** while it's running, the title starts with `● Running`, Stop is visible, `#activity-sec` is not open, and `#activity-live` shows the latest step.
- **Stop then rerun:** start a run, press Esc, start a second run, and let it finish. Expected:
  - The title is `QueryForge`.
  - `#ask` is enabled with the text `Run`.
  - `#activity` contains only the second run's lines.
- **Error:** an `error` event shows the red "Query failed" notice in `#answer`.
- **Disconnected:** `setConnection(false, "ORA-03113 …")` shows the red tag with that tooltip, the Reconnect link and a disabled Run.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest -v`. Expected: PASS.

```bash
git add src/queryforge/web/static/index.html
git commit -m "Live run status, stop/abort, collapsible activity, error and connection states, shortcuts"
```

---

### Task 7: Saved-queries sidebar

**Files:**
- Modify: `src/queryforge/web/static/index.html` (sidebar markup, styles, DOM layer)

**Interfaces:**
- Consumes: `QF.parseBookmarks`, `addBookmark`, `removeBookmark`, `touchBookmark`, `groupBookmarks`, `relativeTime`, `store` (Tasks 2–3); `onRunStart` hook (Task 6); `#sidebar`, `#sidebar-toggle`, `#star`, `#q` (Task 4).
- Produces: `renderSidebar() -> void`, `setSidebar(state: "open"|"collapsed") -> void` (persists `qf.sidebar`).

- [ ] **Step 1: Render the list**

- Bookmarks load once from `QF.parseBookmarks(QF.store.get("qf.bookmarks", []))`.
- `renderSidebar` draws a search box, then `groupBookmarks(list, filter)` as group headings with counts. Each item shows ★, the question (ellipsized, full text in `title`), the `relativeTime(lastRun, Date.now())` meta when it has a last run, and a × button.
- **Empty state:** "No saved queries yet. Click ★ next to a question to save it."
- The search box re-renders as you type.

- [ ] **Step 2: Interactions**

- **Clicking an item** sets `#q.value` to its question, focuses `#q` and marks the item active. It does **not** run. Under 900px it also closes the drawer.
- **×** calls `removeBookmark`, persists, and re-renders.
- **`#star`** is filled when `#q`'s trimmed value matches a bookmark, and updates on `input`.
  - Clicking a filled star removes the matching bookmark.
  - Clicking an empty star opens a popover under the star. It has a group `<input list>` with a datalist of existing groups and defaults to "Saved". Save adds the bookmark; Esc or a click outside closes the popover.
- **`onRunStart(question)`** calls `touchBookmark(list, question, Date.now())`, persists, and re-renders.

- [ ] **Step 3: Collapse**

`#sidebar-toggle` calls `setSidebar`. Collapsed means a 52px rail of ★ buttons, each with the question in `title`; clicking one behaves like clicking the item. Boot restores `qf.sidebar` (default `"open"`).

- [ ] **Step 4: Playwright check**

1. Save "Top SQL by CPU" into group "Top SQL" through the popover.
2. Reload the page and call `showApp`. Expected: the item is still there under "Top SQL".
3. Click the item. Expected: `#q` has its text and no `/query` fetch happened (spy on `fetch`).
4. Collapse the sidebar and reload. Expected: it's still collapsed.
5. Set `localStorage["qf.bookmarks"] = "garbage"` and reload. Expected: the empty state shows and there are no console errors.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest -v`. Expected: PASS.

```bash
git add src/queryforge/web/static/index.html
git commit -m "Add collapsible saved-queries sidebar stored per browser"
```

---

### Task 8: Responsive layout

**Files:**
- Modify: `src/queryforge/web/static/index.html` (styles, small DOM additions)

**Interfaces:**
- Consumes: `setSidebar` (Task 7), header IDs (Task 4).
- Produces: `#sidebar-backdrop`, `#header-menu` (button + panel holding the theme toggle, read-only tag and Log out under 600px).

- [ ] **Step 1: Implement the breakpoints from Global Constraints**

- **900–1099px:** on boot, the sidebar starts collapsed unless `qf.sidebar` was explicitly set. When expanded, it overlays the main area (`position: absolute`, shadow) instead of pushing it.
- **Under 900px:**
  - The sidebar becomes an off-canvas drawer, toggled by `#sidebar-toggle`, with `#sidebar-backdrop`. Clicking the backdrop or pressing Esc closes it.
  - The drawer state is **not** persisted.
  - `.two` becomes one column, and the header hides its "Database"/"User" labels.
- **Under 600px:**
  - 16px gutters.
  - The question row wraps so Run/Stop sit on a full-width row beneath it.
  - The header shows brand, connection dot and `#header-menu`.
  - The results meta wraps.
  - Activity lines wrap.
  - The login card is full width.
- **Touch:** all buttons are at least 36px tall. `@media (hover: none)` keeps the bookmark × visible.

- [ ] **Step 2: Playwright check at 1440, 1024, 768 and 390px in both themes**

At each width, with the app shown and a 13-column result rendered:
- `document.documentElement.scrollWidth <= window.innerWidth`.
- Every `button` has `getBoundingClientRect().height >= 36` (390px only).
- The sidebar state matches the breakpoint (open, rail or overlay, drawer closed).
- Save a screenshot for each width × theme for the reviewer.
- Open and close the drawer at 768px using the toggle, the backdrop and Esc.

- [ ] **Step 3: Run tests and commit**

Run: `uv run pytest -v`. Expected: PASS.

```bash
git add src/queryforge/web/static/index.html
git commit -m "Make UI responsive: overlay rail, off-canvas drawer, phone header menu"
```
