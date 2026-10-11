# QueryForge

Ask questions in plain English; QueryForge uses an LLM (**Gemini** or **Claude**,
switchable) to write **Oracle SQL**, runs it **read-only** against your Oracle
database — cloud or local — and shows the result in a small web UI.

Set the model provider in `.env`:

```
PROVIDER=gemini    # default; GEMINI_MODEL=gemini-2.5-flash
# PROVIDER=claude  # uses MODEL=claude-sonnet-4-6 — runs on Vertex (needs Claude quota)
```

…and the database in the same file:

```
DB_TARGET=cloud    # Oracle Autonomous Database, over an mTLS wallet
# DB_TARGET=local  # Oracle in Docker or installed on this machine
```

Both targets' credentials live in `.env` at once under `CLOUD_ORACLE_*` and
`LOCAL_ORACLE_*` prefixes, so switching is one line — see
[Local Oracle](#local-oracle). The web UI also has a **switch button** in the
header that flips targets at runtime and writes the choice back to `.env`, with a
green/red dot showing whether the active database is reachable.

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
3. **Database:** either point at the shared cloud DB (`DB_TARGET=cloud`) — get the
   wallet folder and the `CLOUD_ORACLE_*` values from whoever runs it, sent
   out-of-band, never committed — or run your own ([Local Oracle](#local-oracle))
   and set `DB_TARGET=local`.
4. **Run:** `uv run uvicorn queryforge.web.app:app` → open http://127.0.0.1:8000.

> **Windows:** no extra native setup — `oracledb` runs in thin mode (pure Python,
> no Oracle Instant Client), and `uv` runs on Windows. Use your local wallet path
> in `.env` (e.g. `C:\Users\you\wallet`).

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
  config.py        # env/.env settings (pydantic-settings) + cloud/local DB profiles
  db.py            # python-oracledb thin-mode pool + read-only query helpers
  sql_guard.py     # read-only SQL validation + row cap
  prompt.py        # system prompt + schema overview + perf-view (AWR/ASH) catalog
  agent_core.py    # provider-neutral tool specs + tool execution
  agent.py         # Claude-on-Vertex tool-use loop (event generator)
  agent_gemini.py  # Gemini tool-use loop — same events, google-genai SDK
  runner.py        # picks the provider (PROVIDER env var) and dispatches
  web/app.py       # FastAPI: POST /query (SSE), GET /health, serves the UI
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
3. **Oracle wallet**: download the ADB wallet (mTLS) and unzip it somewhere. Note the
   directory (it contains `tnsnames.ora` and `ewallet.pem`) and a TNS alias (e.g. `mydb_low`).
4. **Read-only DB user** — this is the actual security boundary. Run as **ADMIN**
   (a schema-owner account usually can't `CREATE USER`). Grant `SELECT` only on the
   tables the agent may read; never grant write/DDL:
   ```sql
   CREATE USER qf_readonly IDENTIFIED BY "a-strong-password";
   GRANT CREATE SESSION TO qf_readonly;
   -- grant SELECT only on each table/view the agent may read, e.g. (owner = SHREYAS):
   GRANT SELECT ON SHREYAS.EMPLOYEE TO qf_readonly;
   -- (or build a read-only role and grant it)
   ```
   Then in `.env` set `CLOUD_ORACLE_USER=qf_readonly`, its password, and
   `CLOUD_ORACLE_SCHEMA=SHREYAS` so the agent reads/introspects the owner's schema.
   The app sets `CURRENT_SCHEMA`, so questions still use unqualified table names.
   The same DDL applies to a local database — run it there too rather than
   pointing the agent at `SYSTEM`.

## Local Oracle

Set `DB_TARGET=local` and fill in the `LOCAL_ORACLE_*` block. There is **no
OS-specific code** in QueryForge: `oracledb` runs in thin mode everywhere, so
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

```
DB_TARGET=local
LOCAL_ORACLE_USER=system
LOCAL_ORACLE_PASSWORD=oracle
LOCAL_ORACLE_DSN=localhost:1521/FREEPDB1
```

**Windows — native install:** run the Oracle Database 23ai Free installer (or XE
21c). The service starts automatically; the pluggable database is `FREEPDB1` on
23ai and `XEPDB1` on XE 21c, so use
`LOCAL_ORACLE_DSN=localhost:1521/FREEPDB1` (or `.../XEPDB1`). Verify with:

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

The DSN and `.env` block are the same as Docker's.

## Configure

```
cp .env.example .env
# edit .env: either GEMINI_API_KEY (no GCP) or GCP_PROJECT_ID/VERTEX_REGION,
# then DB_TARGET plus that target's CLOUD_ORACLE_* / LOCAL_ORACLE_* values
```

> If your ADB uses **one-way TLS** instead of mTLS, leave `CLOUD_ORACLE_CONFIG_DIR` /
> `CLOUD_ORACLE_WALLET_LOCATION` / `CLOUD_ORACLE_WALLET_PASSWORD` blank and put the
> full connect descriptor in `CLOUD_ORACLE_DSN`.

> Unprefixed `ORACLE_*` variables still work as fallbacks for the cloud target, so
> a `.env` written before `DB_TARGET` existed keeps connecting unchanged.

## Run

```
uv run queryforge                                    # console command
uv run uvicorn queryforge.web.app:app --reload       # dev, with auto-reload
```

`queryforge` binds to localhost:8000 by default (override with `QUERYFORGE_HOST` /
`QUERYFORGE_PORT`). Open http://127.0.0.1:8000 and ask a question. Check connectivity
any time at http://127.0.0.1:8000/health.

## Test

```
uv run pytest
```

The agent and web tests mock Vertex/Oracle and run offline. The live `test_db` check
runs only when `ORACLE_*` env vars are set.

## Out of scope (v1)

App-level auth / multi-user, write/DML support, cross-schema querying, and deployment —
this runs locally for a single user.
