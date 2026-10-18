# QueryForge

An Oracle **performance-analysis** assistant. Ask questions in plain English
(*"top 10 SQL by elapsed time"*, *"top wait events in the last hour"*);
QueryForge uses an LLM (**Gemini** or **Claude**, switchable) to write **Oracle
SQL** over Oracle's data dictionary and performance views (`V$`, ASH, AWR), runs it
**read-only**, and shows the result in a web UI built like a monitoring console.

QueryForge reads **only Oracle-maintained objects**. Application tables (anything
in a user-created schema) are refused, and so are requests to list schemas or
users. See [How it works](#how-it-works).

New machine? **[SETUP.md](SETUP.md)** is a step-by-step guide (Windows included).

Set the model provider in `.env`:

```
PROVIDER=gemini    # default; GEMINI_MODEL=gemini-3.1-flash-lite
# PROVIDER=claude  # uses MODEL=claude-sonnet-4-6 — runs on Vertex (needs Claude quota)
```

The database is chosen at runtime: QueryForge opens on a **login screen** where
you pick a database and an Oracle user from `databases.json` and type the
password. See [Connecting](#connecting).

**Two ways to reach Gemini** (pick one):

- **API key (easiest, no GCP).** Get a free key at
  [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and set
  `GEMINI_API_KEY` in `.env`. No gcloud, no Vertex, no ADC — works anywhere,
  Windows included. See [Quick start (no GCP)](#quick-start-no-gcp).
- **Vertex AI.** Leave `GEMINI_API_KEY` blank and authenticate with GCP ADC
  (`gcloud auth application-default login`). `PROVIDER=claude` always uses Vertex.

## Quick start (no GCP)

For running on a machine with **no GCP project** (e.g. a teammate cloning the repo):

1. **Clone + install:**
   ```
   git clone <repo-url> && cd QueryForge
   uv sync
   ```
2. **Gemini API key:** copy `cp .env.example .env`, then set `PROVIDER=gemini` and
   `GEMINI_API_KEY=<your free key from aistudio.google.com/apikey>`. Leave
   `GCP_PROJECT_ID` blank.
3. **Database:** `cp databases.example.json databases.json` and edit it for your
   database (see [Connecting](#connecting)). The Oracle user you log in with needs
   `SELECT_CATALOG_ROLE` (see [Prerequisites](#prerequisites-one-time)).
4. **Run:** `uv run uvicorn queryforge.web.app:app` → open http://127.0.0.1:8000 and log in.

> **Windows:** no extra native setup — `oracledb` runs in thin mode (pure Python,
> no Oracle Instant Client), and `uv` runs on Windows.

## How it works

A tool-use agent (Gemini or Claude) is given three read-only tools:

- `list_tables` — a catalog of the key performance views (`V$SQLAREA`, `V$SESSION`,
  `V$ACTIVE_SESSION_HISTORY`, `DBA_HIST_*`, `DBMS_XPLAN`, …). No application tables.
- `describe_table` — columns of one dictionary or performance view; follows synonyms
  (`V$SQLAREA` → `SYS.V_$SQLAREA`).
- `run_query` — runs one SELECT.

It writes Oracle SQL, runs it, and self-corrects if the query errors. Progress streams
to the browser over SSE.

**Only Oracle-maintained objects.** Before a query runs (and before a view is
described), every table, view and function it references is resolved the way
Oracle would — explicit owner, else current schema, else PUBLIC synonym, following
synonym chains — and allowed only if the final owner is an Oracle-maintained schema
(`ALL_USERS.ORACLE_MAINTAINED = 'Y'`, e.g. `SYS`). Deciding by owner rather than by
name means a user table called `DBA_FAKE`, or a PUBLIC synonym onto an application
table, is still refused. Package calls are limited to `DBMS_XPLAN` (some Oracle
packages, like `DBMS_XMLGEN`, run SQL text passed to them). Requests to list
schemas or users are refused by the system prompt.

**Safety is layered:**
1. The **Oracle user** is the real boundary. Use one that owns no tables and holds
   only `CREATE SESSION` + `SELECT_CATALOG_ROLE` (see [Prerequisites](#prerequisites-one-time)).
2. A **SQL guard** (`sqlglot`, Oracle dialect) rejects anything that isn't a single
   read-only SELECT — DDL, DML, PL/SQL, multi-statement payloads, `SELECT ... INTO`.
3. The **object policy** above rejects anything that isn't Oracle-maintained.
4. A **row cap** and a **per-query timeout** bound runaway queries.

Layers 2–3 are application checks: an Oracle user that owns tables can still read
them from any other client, which is why layer 1 matters.

The model only ever sees a small *preview* of query results; the full (capped) result set
goes to the UI separately, so a broad query can't blow up context or cost.

## Project layout

```
src/queryforge/
  config.py        # env/.env settings (pydantic-settings)
  connections.py   # databases.json loader (login-screen choices)
  session.py       # the active login (connection pool), swapped under a lock
  observability.py # OpenTelemetry tracing → Arize Phoenix
  db.py            # python-oracledb thin-mode pool + read-only query helpers
  sql_guard.py     # read-only SQL validation + row cap
  object_policy.py # Oracle-maintained-objects-only check (tables + functions)
  prompt.py        # system prompt: scope rules + perf-view (AWR/ASH) catalog
  agent_core.py    # provider-neutral tool specs + tool execution
  agent.py         # Claude-on-Vertex tool-use loop (event generator)
  agent_gemini.py  # Gemini tool-use loop — same events, google-genai SDK
  runner.py        # picks the provider (PROVIDER env var) and dispatches
  web/app.py       # FastAPI: /login, /logout, /session, /databases, POST /query (SSE), /health
  web/static/index.html
tests/             # sql_guard, object_policy, agents (mocked), db helpers, web + UI (Node) tests
```

## Prerequisites (one-time)

> Steps 1–2 (GCP/Vertex) are **only for the Vertex path**. If you're using a Gemini
> API key ([Quick start](#quick-start-no-gcp)), skip them and start at step 3.

1. **Vertex AI + Claude model**: in your GCP project, enable the Vertex AI API and enable
   your chosen Claude model (e.g. `claude-sonnet-4-6` or `claude-opus-4-8`) in
   **Vertex AI → Model Garden**.
2. **GCP auth (ADC)** — no API key is used:
   ```
   gcloud auth application-default login
   ```
3. **Read-only DB user** — this is the actual security boundary. Run as an admin
   in the pluggable database. The user should own no tables and get only the
   dictionary / performance views; never grant write/DDL or `SELECT ANY TABLE`:
   ```sql
   CREATE USER qf_readonly IDENTIFIED BY "a-strong-password";
   GRANT CREATE SESSION TO qf_readonly;
   GRANT SELECT_CATALOG_ROLE TO qf_readonly;   -- V$ / DBA_ / DBA_HIST_ views
   ```
   Then add `qf_readonly` to that database's `users` in `databases.json`. Without
   `SELECT_CATALOG_ROLE`, performance questions fail with *ORA-00942*.
   AWR history (`DBA_HIST_*`) needs Enterprise Edition with the Diagnostics Pack
   license; on XE / Free those views are empty or missing, while `V$` and ASH work.

## Local Oracle

There is **no OS-specific code** in QueryForge: `oracledb` runs in thin mode everywhere, so
Windows, Linux and macOS all connect with the same pure-Python driver and the
same DSN. The only difference is how you start the database.

> **Pin a modern version (12.1+, ideally 23ai or 21c).** Thin mode requires
> Oracle 12.1 or newer; an old 11g image would force thick mode plus an Instant
> Client install, which this project deliberately avoids.

**Docker — recommended, and identical on all three OSes** (Docker Desktop on
Windows/macOS, Docker Engine on Linux):

```
docker run -d --name oracle-free -p 1521:1521 -e ORACLE_PASSWORD=oracle \
  gvenzl/oracle-free:23-slim
docker logs -f oracle-free      # wait for "DATABASE IS READY TO USE!"
```

Then add it to `databases.json`. You can log in as `system` (password `oracle`)
to try it, but create `qf_readonly` as in [Prerequisites](#prerequisites-one-time)
for regular use:

```json
[{ "name": "Oracle Free (Docker)", "host": "localhost", "port": 1521,
   "service_name": "FREEPDB1", "users": ["system", "qf_readonly"] }]
```

**Windows — native install:** run the Oracle Database 23ai Free installer (or XE
21c). The service starts automatically; the pluggable database is `FREEPDB1` on
23ai and `XEPDB1` on XE 21c, so use
`"service_name": "FREEPDB1"` (or `"XEPDB1"`) in `databases.json`. Verify with:

```
sqlplus system/<your-password>@localhost:1521/FREEPDB1
```

**Linux — native install:** install the RPM (`oracle-database-free-23ai`), then
configure and start it:

```
sudo /etc/init.d/oracle-free-23ai configure
sudo systemctl start oracle-free-23ai
sqlplus system/<your-password>@localhost:1521/FREEPDB1
```

The `databases.json` entry is the same as Docker's.

## Connecting

```
cp .env.example .env                       # model provider: GEMINI_API_KEY or GCP_PROJECT_ID
cp databases.example.json databases.json   # the databases on the login screen
```

`databases.json` is a list; each entry is one database:

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Shown in the dropdown; must be unique |
| `host` | yes | Listener host, e.g. `localhost` |
| `port` | no | Listener port (default `1521`) |
| `service_name` | yes | Oracle service, e.g. `AIWORLD` or `FREEPDB1` |
| `schema` | no | Session `CURRENT_SCHEMA` (default: the logged-in user). Objects in it are still refused unless Oracle-maintained |
| `users` | yes | Oracle users offered for this database |

A connect string such as
`(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=localhost)(PORT=1539))(CONNECT_DATA=(SERVICE_NAME=AIWORLD)))`
maps to `"host": "localhost", "port": 1539, "service_name": "AIWORLD"`.

On the login screen pick the database and user and type the password. The
password is held **only in memory** — never written to disk or logged — so you
log in again after restarting the server. Edits to `databases.json` show up the
next time the login screen loads.

> **Keep the server on localhost.** There is one login for the whole process and
> no cookies, so anyone who can reach the port uses your database session. Don't
> run it with `--host 0.0.0.0`.

## Observability

Every question can be recorded as a trace — the question, each model call (prompt,
response, tokens, latency) and each tool call (SQL, result preview, errors) — and
viewed in [Arize Phoenix](https://github.com/Arize-ai/phoenix), running locally.

```
docker run -d --name qf-phoenix -p 6006:6006 \
  -e PHOENIX_WORKING_DIR=/mnt/data -v qf-phoenix-data:/mnt/data \
  arizephoenix/phoenix:version-20.19.0
```

Then in `.env` set `PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006`, restart
QueryForge, ask a question, and open http://localhost:6006 → project
**queryforge**. Each trace is one question: an `AGENT` span (user, database,
outcome — `answered`, `gave_up`, `error` or `cancelled`) containing `LLM` and
`TOOL` spans. Failed runs are red.

- Tracing is off when `PHOENIX_COLLECTOR_ENDPOINT` is blank.
- If Phoenix is down, questions are unaffected; spans are dropped.
- `TRACE_HIDE_CONTENT=true` keeps structure, timings, tokens and errors but no
  prompts, answers or query results.
- Traces stay on your machine (in the `qf-phoenix-data` Docker volume).

## Run

```
uv run queryforge                                    # console command
uv run uvicorn queryforge.web.app:app --reload       # dev, with auto-reload
```

`queryforge` binds to localhost:8000 by default (override with `QUERYFORGE_HOST` /
`QUERYFORGE_PORT`). Open http://127.0.0.1:8000, log in, and ask a question. Check connectivity
any time at http://127.0.0.1:8000/health.

## Test

```
uv run pytest
```

The agent and web tests mock Vertex/Oracle and run offline; the UI tests need
`node` on the PATH. The live `test_db` checks (describe `V$SQLAREA`, refuse an
application table) run only when `QF_LIVE_DATABASE` (an entry name in
`databases.json`), `QF_LIVE_USER` and `QF_LIVE_PASSWORD` are set:

```
QF_LIVE_DATABASE="AIWORLD (local)" QF_LIVE_USER=shreyas QF_LIVE_PASSWORD=... uv run pytest tests/test_db.py -k live
```

## Out of scope (v1)

App-level auth / multi-user, write/DML support, querying application data, and
deployment — this runs locally for a single user. Saved queries live in the
browser (`localStorage`); server-side storage is future work.
