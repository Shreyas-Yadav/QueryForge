# QueryForge

Ask questions in plain English; QueryForge uses an LLM **on Google Vertex AI**
(**Gemini** or **Claude**, switchable) to write **Oracle SQL**, runs it **read-only**
against your Oracle Autonomous Database, and shows the result in a small web UI.

Set the model provider in `.env`:

```
PROVIDER=gemini    # works on any Vertex project (default); GEMINI_MODEL=gemini-2.5-flash
# PROVIDER=claude  # uses MODEL=claude-sonnet-4-6 — needs Claude Vertex quota provisioned
```

## How it works

A Claude tool-use agent is given three read-only tools — `list_tables`,
`describe_table`, and `run_query`. It inspects the schema as needed, writes a SELECT,
runs it, and self-corrects if the query errors. Progress streams to the browser over SSE.

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
  db.py            # python-oracledb thin-mode pool + read-only query helpers
  sql_guard.py     # read-only SQL validation + row cap
  agent.py         # Claude-on-Vertex tool-use loop (event generator)
  web/app.py       # FastAPI: POST /query (SSE), GET /health, serves the UI
  web/static/index.html
tests/             # sql_guard, agent (mocked), db helpers, web smoke tests
```

## Prerequisites (one-time)

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
   Then in `.env` set `ORACLE_USER=qf_readonly`, its password, and
   `ORACLE_SCHEMA=SHREYAS` so the agent reads/introspects the owner's schema.
   The app sets `CURRENT_SCHEMA`, so questions still use unqualified table names.

## Configure

```
cp .env.example .env
# edit .env: GCP_PROJECT_ID, VERTEX_REGION, MODEL, and the ORACLE_* values
```

> If your ADB uses **one-way TLS** instead of mTLS, leave `ORACLE_CONFIG_DIR` /
> `ORACLE_WALLET_LOCATION` / `ORACLE_WALLET_PASSWORD` blank and put the full connect
> descriptor in `ORACLE_DSN`.

## Run

```
uv run uvicorn queryforge.web.app:app --reload
```

Open http://127.0.0.1:8000 and ask a question. Check connectivity any time at
http://127.0.0.1:8000/health.

## Test

```
uv run pytest
```

The agent and web tests mock Vertex/Oracle and run offline. The live `test_db` check
runs only when `ORACLE_*` env vars are set.

## Out of scope (v1)

App-level auth / multi-user, write/DML support, cross-schema querying, and deployment —
this runs locally for a single user.
