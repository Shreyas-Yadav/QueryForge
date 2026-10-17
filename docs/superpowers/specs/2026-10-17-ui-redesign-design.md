# QueryForge UI redesign (front end only)

**Status:** design approved in brainstorming, 2026-10-17
**Scope:** `src/queryforge/web/static/index.html` only. No backend or API changes.
**Reference mockup:** `docs/superpowers/specs/2026-10-17-ui-redesign-mockup.html`. Open it in a browser; it's interactive. Its "Preview states" buttons show the running, error and disconnected states. The mockup is the visual source of truth for spacing, colors and type. This document is the source of truth for behavior.

## Why

The current page looks generic, like a default AI chat demo. QueryForge is built for Oracle **performance teams**. They ask about V$SQL, ASH, AWR, waits and plans, and the rows that come back *are* the analysis. The UI should feel like a professional monitoring tool (Datadog / Atlassian-ops style, light by default), not a chatbot.

## Goals

- Professional, dense, monitoring-tool look ("Classic / Light ops" from the mockup).
- Keep the existing sections and data flow: header + connection, question box, Answer, Generated SQL, Results, Activity.
- Thinking/Activity is **collapsible and collapsed by default**.
- Light and dark themes, switchable by the user.
- A collapsible left sidebar of **saved queries** (bookmarks), personal to each user.
- The 14 UX improvements listed below.

## Non-goals

- No backend changes. That includes server-side saved queries, new event types, query timing from the server, and history.
- No new content sections beyond the sidebar. In particular: no KPI tiles, no schema diagram, no query history.
- No change to login flow logic or the SSE protocol.

## Anti-goals (it fails even if it "works" if…)

- It looks like a chat app or an AI demo: gradient accents, purple "thinking" text, chat bubbles.
- Results are hard to scan: unaligned numbers, wrapped cells, or wide tables that break the layout.
- The thinking log is in the way by default.

## Constraints

- Single static file (`index.html`), plain HTML/CSS/vanilla JS, as today. The server only serves `/` → `index.html`; there is no static mount, so no extra asset files.
- Event types the page receives (unchanged, see `agent.py` docstring): `status`, `thinking`, `assistant_text`, `tool_call`, `sql`, `result` (`columns`, `rows`, `row_count`, `truncated`), `tool_error`, `answer`, `error`, `done`. The Gemini path emits no `thinking` events, so the design must not depend on them.
- Existing endpoints: `GET /databases`, `GET /session` (`logged_in`, `database`, `user`, `reachable`, `detail`), `POST /login`, `POST /logout`, `POST /query` (SSE; returns 401 when not logged in). The backend closes the agent run when the client disconnects, so aborting the fetch is a real cancel.

## Visual system (decided)

- **Fonts:** IBM Plex Sans for UI text and IBM Plex Mono for SQL, SQL_IDs, plan hashes, timestamps and the activity log, loaded from Google Fonts with system-font fallbacks.
- **Colors:** CSS custom properties on `:root` for light and `[data-theme="dark"]` for dark. Use the token values from the mockup. Semantic colors are the same in both themes: accent blue for links and IDs, red for elapsed bars and errors, green for connected, amber for read-only and warnings, star yellow for bookmarks.
- **Panels:** white surface, 1px border, 4px radius. Section labels are small uppercase (`ANSWER`, `GENERATED SQL`, `RESULTS`, `ACTIVITY`).
- **Numbers:** right-aligned with `tabular-nums`.

## Layout (decided)

```
┌ header: [☰] QueryForge │ Database ORCLPDB  User shreyas │ ●CONNECTED READ-ONLY [Light|Dark] Log out ┐
├ sidebar (240px ↔ 52px rail) ┬ main ─────────────────────────────────────────────────┤
│ Search saved…               │ [ question input            ⌘K ★ ] [Run] [Stop Esc]       │
│ GROUP                       │ ┌ Answer ─────────────┐ ┌ Generated SQL ─── Copy ┐        │
│ ★ saved question            │ └─────────────────────┘ └────────────────────────┘        │
│   last-run time             │ ┌ Results  5 rows · 13 columns · 1.2 s · Ran 09:42:11 on ORCLPDB | ↻ Re-run  Export CSV ┐ │
│ …                           │ │ (truncation warning strip, when truncated)            │ │
│                             │ │ scrollable table, sticky header + first column         │ │
│                             │ └────────────────────────────────────────────────────────┘ │
│                             │ ▸ ACTIVITY · <live step>                     5 steps · 6.2 s │
```

- The "Try:" example links row is **removed**.
- Answer and Generated SQL sit side by side; Results gets the full width below.
- The login screen stays a centered card, restyled with the same tokens, fonts and theme.

## Behavior

### Header
- Shows database and user (mono) and a connection tag:
  - `● CONNECTED` (green) when `/session` says `reachable`.
  - `● DISCONNECTED` (red) otherwise, with `detail` as the hover tooltip and a **Reconnect** link next to it. Reconnect re-calls `/session`: if reachable, go back to green; if `logged_in` is false, show login. While disconnected, Run is disabled with the tooltip "Reconnect to run queries".
  - A `/query` that returns 401 or fails at the network level also switches to the disconnected state (401 still routes to login, as today).
- `READ-ONLY` amber tag, always shown.
- `Light | Dark` segmented toggle.
- Log out (existing behavior).
- Sidebar toggle button left of the brand.

### Sidebar: saved queries
- **Storage:** `localStorage`, per browser. Each person keeps their own list. Server-side storage is future work.
- **Item:** the saved *question text* (not SQL), an optional group, and a last-run time.
- **Saving:** the ★ inside the question box. Filled means the current question text is saved; clicking toggles save/unsave.
- **Clicking an item** fills the question box with its text and focuses it. It does **not** run. The user presses Run or Enter, which sends the question to the agent as normal. Clicking also marks the item active.
- **Last run:** updated when a run starts whose question exactly matches a saved item. Shown as relative time ("2 min ago", "yesterday").
- **Search:** filters items by substring as you type.
- **Groups:** headings with counts (e.g. Top SQL, Waits, Plans), as in the mockup. Clicking an empty ★ opens a small popover under the star with a group field. You pick an existing group or type a new one; the default is "Saved". Then you click Save. Empty groups disappear.
- **Remove:** hovering an item shows a small × that removes it. Clicking a filled ★ in the question box also removes the matching item, with no popover.
- **Collapse:** the sidebar shrinks to a 52px rail of ★ icons, with tooltips showing the full question. Clicking a ★ in the rail behaves like clicking the item.
- **Empty state:** "No saved queries yet. Click ★ next to a question to save it."

### Question box and run lifecycle
- Placeholder: "Ask about SQL performance, waits, plans…". A `⌘K` hint shows when the box isn't focused.
- **Run button while running:** spinner + "Running · 4.1s" (counts up every 100ms), disabled. A **Stop** button (with an `Esc` hint) appears next to it.
- **Stop / Esc:** aborts the fetch with an `AbortController`. Answer shows a neutral "Run stopped" notice, Results hide, and Activity logs "stop · Cancelled by user".
- **Browser tab title:** "● Running – QueryForge" while running, otherwise "QueryForge".
- **While waiting:** Answer and Results show pulsing skeleton placeholders. Generated SQL fills in when the `sql` event arrives.

### Answer
- Renders `answer` text as today, as text, not HTML.
- **Errors:** an `error` event, or a run that ends with a `tool_error` and no `answer`, shows a red notice in the Answer panel: bold "Query failed" plus the message. Errors also still appear in Activity.

### Generated SQL
- Mono, light syntax coloring (keywords, numbers) is nice to have; plain text is acceptable.
- **Copy** button changes to "Copied ✓" for about 1.4s.

### Results
- **Header meta:** "N rows · M columns · X s · Ran HH:MM:SS on DB". X is client-measured from the `sql` event to the `result` event. Ran time is client clock at `result`. DB is the session database name.
- **↻ Re-run:** runs the current question box text again (same as pressing Run).
- **Export CSV:** downloads the current rows client-side. The button changes to "Downloaded ✓" briefly.
- **Truncation:** when `truncated`, show an amber strip above the table: "Showing the first N rows. The result was truncated; narrow the question to see the rest."
- **Table:**
  - Horizontal and vertical scrolling inside the panel (max-height around 420px).
  - Sticky header row and sticky first column.
  - No cell wrapping.
  - NULL shown as a muted "—".
  - Numbers formatted with thousands separators and right-aligned.
  - Click a column header to sort client-side (desc first, toggle). An arrow marks the sorted column; NULLs sort last.
  - Click to copy, with a toast "Copied <value>", for: the first column when its name looks like an ID (`SQL_ID`, `*_ID`), and any column named like `PLAN_HASH_VALUE`. These are styled in accent-blue mono.
  - **Elapsed bar:** a thin red bar before the value in a column whose name matches elapsed (e.g. `ELAPSED*`), scaled to the column max. If no such column exists, there is no bar.

### Activity (thinking)
- `<details>`, **collapsed by default**, on every run.
- **Summary while running:** "ACTIVITY · <live step>". Live step is the latest `status` message, or `tool_call` name + short args, or "thinking…".
- **Summary when done:** "N steps · X s" on the right.
- **What goes in the log:** `status`, `thinking` (first ~200 chars, full text in a tooltip), `assistant_text`, `tool_call` (name + args trimmed to ~160 chars), `tool_error`, `error`. `sql`, `result` and `answer` render in their own panels and only add a one-line note (e.g. "run_query → 5 rows").
- **Log lines:** mono, `[mm:ss.s] [kind] text`, relative to run start. Kinds: status, think, tool, error, stop. Colors per the mockup; thinking is muted italic, not purple.

### Responsive (required)
- **≥ 1100px:** full layout as in the mockup.
- **900–1099px:** sidebar starts collapsed to the rail (the user can still expand it, and it overlays the main area instead of pushing it). Answer and SQL stay side by side.
- **< 900px:** Answer and Generated SQL stack vertically. The header hides the "Database / User" labels and keeps the values. The sidebar becomes an off-canvas drawer opened by the ☰ button, with a backdrop; picking a saved query closes it.
- **< 600px (phone):**
  - 16px side padding and no horizontal page scroll. Only the results table scrolls sideways.
  - The question box takes the full width, with Run/Stop on their own row beneath it.
  - The header collapses to brand + connection dot + a menu holding theme, Log out and the read-only tag.
  - The Results header meta wraps to a second line.
  - Activity log lines wrap instead of using a fixed grid.
- **Interaction:**
  - Touch targets are at least 36px.
  - Hover-only affordances (× to remove a saved query, click-to-copy hints) have a tap equivalent: × is always visible on touch devices (`@media (hover: none)`).
- **Login card:** full width with 16px gutters on small screens.
- Verify at 1440, 1024, 768 and 390px widths in both themes.

### Keyboard
- `⌘K` / `Ctrl+K`: focus and select the question box.
- `Enter`: run.
- `↑` in the question box: recall the last asked question.
- `Esc`: stop a running query.

### Remembered per browser (localStorage)
- Theme, sidebar collapsed state, saved queries, last asked question.
- Every read and write is wrapped in try/catch; the page works without storage.

## Left to the builder

- Exact structure of the JS: one file, organized as you see fit, matching current vanilla style.
- How to name/structure the localStorage keys (prefix `qf.`).
- Relative-time formatting details.

## Calls made during brainstorming (by Claude, not the user)

- IBM Plex as the font pair.
- Theme and sidebar state remembered per browser.
- Small-caps section labels.
- Sticky first column, ~420px results height cap.
- Query time measured client-side, since the backend sends no timing.
- Reconnect re-checks `/session` rather than re-logging in.
- Click-to-copy and elapsed-bar columns chosen by column-name patterns.
- Responsive breakpoints: 1100 / 900 / 600px, as described above.
- Saving a query opens a group popover (default group "Saved"); hovering an item shows × to remove it.
