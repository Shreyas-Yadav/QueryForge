# QueryForge

Ask questions in plain English; QueryForge uses an LLM (**Gemini** or **Claude**,
switchable) to write **Oracle SQL**, runs it **read-only** against your Oracle
database and shows the result in a small web UI.

Set the model provider in `.env`:

```
PROVIDER=gemini    # default; GEMINI_MODEL=gemini-2.5-flash
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
   database (see [Connecting](#connecting)).
4. **Run:** `uv run uvicorn queryforge.web.app:app` → open http://127.0.0.1:8000 and log in.

> **Windows:** no extra native setup — `oracledb` runs in thin mode (pure Python,
> no Oracle Instant Client), and `uv` runs on Windows.

## How it works

A Claude tool-use agent is given three read-only tools — `list_tables`,
`describe_table`, and `run_query`. It inspects the schema as needed, writes a SELECT,
runs it, and self-corrects if the query errors. Progress streams to the browser over SSE.
`list_tables` surfaces both base tables and synonyms (private plus business PUBLIC
synonyms — Oracle's system synonyms are filtered out), and `describe_table` follows a
synonym to its underlying table or view.

**Safety is layered:**
1. A **read-only Oracle user** (`GRANT SELECT` only) is the real boundary.
2. A **SQL guard** (`sqlglot`, Oracle dialect) rejects anything that isn't a single
   read-only SELECT — DDL, DML, PL/SQL, multi-statement payloads, `SELECT ... INTO`.
3. A **row cap** and a **per-query timeout** bound runaway queries.

The model only ever sees a small *preview* of query results; the full (capped) result set
goes to the UI separately, so a broad query can't blow up context or cost.

## Project layout

```
src/queryforge/
  config.py        # env/.env settings (pydantic-settings)
  connections.py   # databases.json loader (login-screen choices)
  session.py       # the active login: pool + schema cache, swapped together
  db.py            # python-oracledb thin-mode pool + read-only query helpers
  sql_guard.py     # read-only SQL validation + row cap
  prompt.py        # system prompt + schema overview + perf-view (AWR/ASH) catalog
  agent_core.py    # provider-neutral tool specs + tool execution
  agent.py         # Claude-on-Vertex tool-use loop (event generator)
  agent_gemini.py  # Gemini tool-use loop — same events, google-genai SDK
  runner.py        # picks the provider (PROVIDER env var) and dispatches
  web/app.py       # FastAPI: /login, /logout, /session, /databases, POST /query (SSE), /health
  web/static/index.html
tests/             # sql_guard, agent + agent_gemini (mocked), db helpers, web smoke tests
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
3. **Read-only DB user** — this is the actual security boundary. Run as **ADMIN**
   (a schema-owner account usually can't `CREATE USER`). Grant `SELECT` only on the
   tables the agent may read; never grant write/DDL:
   ```sql
   CREATE USER qf_readonly IDENTIFIED BY "a-strong-password";
   GRANT CREATE SESSION TO qf_readonly;
   -- grant SELECT only on each table/view the agent may read, e.g. (owner = SHREYAS):
   GRANT SELECT ON SHREYAS.EMPLOYEE TO qf_readonly;
   -- (or build a read-only role and grant it)
   ```
   Then add `qf_readonly` to that database's `users` in `databases.json` and set
   `"schema": "SHREYAS"` so the agent reads the owner's schema. The app sets
   `CURRENT_SCHEMA`, so questions still use unqualified table names.
   The same DDL applies to a local database — run it there too rather than
   pointing the agent at `SYSTEM`.

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

Then add it to `databases.json` and log in as `system` with password `oracle`:

```json
[{ "name": "Oracle Free (Docker)", "host": "localhost", "port": 1521,
   "service_name": "FREEPDB1", "users": ["system"] }]
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
| `schema` | no | Schema the agent reads (default: the logged-in user) |
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

The agent and web tests mock Vertex/Oracle and run offline. The live `test_db` checks
run only when `QF_LIVE_DATABASE` (an entry name in `databases.json`), `QF_LIVE_USER`
and `QF_LIVE_PASSWORD` are set.

## Out of scope (v1)

App-level auth / multi-user, write/DML support, cross-schema querying, and deployment —
this runs locally for a single user.
