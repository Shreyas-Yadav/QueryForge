# Setting up QueryForge on a new machine

A step-by-step guide for running QueryForge on your own computer. **Docker is not
required** — QueryForge is a plain Python app. You need three things:

| What | Required? | Notes |
|---|---|---|
| Python 3.12 + [`uv`](https://docs.astral.sh/uv/) | ✅ | Works on macOS, Linux and Windows. No Oracle Instant Client needed — the driver is pure Python. |
| An Oracle database | ✅ | Any Oracle 12.1+ you can reach: a native install, a company/cloud database, or Docker if you like. |
| A model provider | ✅ | Easiest: a free Gemini API key. No GCP account needed. |
| Arize Phoenix (tracing) | ❌ optional | Only if you want to see traces of each question. |

> **You can't use someone else's `localhost` database.** If a teammate runs Oracle
> on their laptop at `localhost:1539`, that address means *your own* machine when
> you use it. Run your own database (step 3) or use one reachable over the network.

---

## 1. Get the code

Install `uv` if you don't have it:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then:

```bash
git clone https://github.com/Shreyas-Yadav/QueryForge.git
cd QueryForge
uv sync            # installs Python 3.12 (if needed) and all dependencies
```

## 2. Model provider

```bash
cp .env.example .env
```

Get a free key at https://aistudio.google.com/apikey and set it in `.env`:

```
PROVIDER=gemini
GEMINI_API_KEY=<your key>
GCP_PROJECT_ID=
```

(Using Vertex AI or Claude instead? See the README's
[Prerequisites](README.md#prerequisites-one-time).)

## 3. An Oracle database

Pick **one**:

- **Native install (no Docker)** — Oracle Database 23ai Free (or XE 21c) on
  Windows or Linux. Follow the README's [Local Oracle](README.md#local-oracle)
  section. Its service name is `FREEPDB1` (23ai) or `XEPDB1` (XE), port `1521`.
- **A database you already have access to** — get its host, port, service name and
  a username/password from whoever runs it.
- **Docker (optional)** — if you have Docker:
  ```bash
  docker run -d --name oracle-free -p 1521:1521 -e ORACLE_PASSWORD=oracle \
    gvenzl/oracle-free:23-slim
  ```

### Optional: load the sample data

To get the same users and test tables used during development, connect to your database **as an admin user in the pluggable
database** (e.g. `SYSTEM` on `localhost:1521/FREEPDB1`) with SQL\*Plus, SQL
Developer or any client, and run the script below. Replace the two passwords.

```sql
CREATE USER shreyas IDENTIFIED BY "<choose-a-password>" QUOTA UNLIMITED ON users;
GRANT CREATE SESSION, CREATE TABLE, CREATE SYNONYM TO shreyas;
-- Required: QueryForge only reads Oracle's performance and dictionary views
-- (V$SQL, V$SQLAREA, ASH, AWR…), and this role is what grants access to them.
GRANT SELECT_CATALOG_ROLE TO shreyas;

CREATE USER qf_readonly IDENTIFIED BY "<choose-another-password>";
GRANT CREATE SESSION, SELECT_CATALOG_ROLE TO qf_readonly;

CREATE TABLE shreyas.departments (
  dept_id   NUMBER(4) PRIMARY KEY,
  dept_name VARCHAR2(50) NOT NULL,
  location  VARCHAR2(50)
);
COMMENT ON TABLE shreyas.departments IS 'Company departments';

CREATE TABLE shreyas.employees (
  emp_id    NUMBER(6) PRIMARY KEY,
  full_name VARCHAR2(100) NOT NULL,
  dept_id   NUMBER(4) REFERENCES shreyas.departments(dept_id),
  salary    NUMBER(10,2),
  hire_date DATE
);
COMMENT ON TABLE shreyas.employees IS 'Employees and their salaries';

INSERT INTO shreyas.departments VALUES (10, 'Engineering', 'San Francisco');
INSERT INTO shreyas.departments VALUES (20, 'Sales', 'New York');
INSERT INTO shreyas.departments VALUES (30, 'Finance', 'Chicago');
INSERT INTO shreyas.employees VALUES (1, 'Ada Lovelace',   10, 185000, DATE '2021-03-01');
INSERT INTO shreyas.employees VALUES (2, 'Alan Turing',    10, 172000, DATE '2022-07-15');
INSERT INTO shreyas.employees VALUES (3, 'Grace Hopper',   10, 199000, DATE '2019-11-04');
INSERT INTO shreyas.employees VALUES (4, 'Don Draper',     20, 140000, DATE '2020-01-20');
INSERT INTO shreyas.employees VALUES (5, 'Peggy Olson',    20, 118000, DATE '2023-05-08');
INSERT INTO shreyas.employees VALUES (6, 'Warren Buffett', 30, 155000, DATE '2018-09-30');
COMMIT;

GRANT SELECT ON shreyas.departments TO qf_readonly;
GRANT SELECT ON shreyas.employees   TO qf_readonly;
```

## 4. Tell QueryForge about your database

```bash
cp databases.example.json databases.json
```

Edit `databases.json` (it is git-ignored) to point at **your** database, e.g. for
a native 23ai Free install with the sample data:

```json
[
  {
    "name": "My local Oracle",
    "host": "localhost",
    "port": 1521,
    "service_name": "FREEPDB1",
    "schema": "SHREYAS",
    "users": ["shreyas", "qf_readonly"]
  }
]
```

| Field | Meaning |
|---|---|
| `name` | Shown in the login dropdown |
| `host`, `port`, `service_name` | Where the database listens |
| `schema` | Schema the agent reads (omit to use the logged-in user's own) |
| `users` | Oracle users offered on the login screen |

Passwords are **never** stored here — you type them on the login screen.

## 5. Run it

```bash
uv run queryforge
```

Open http://127.0.0.1:8000, choose your database and user, type the password, and
ask a performance question such as *"Top 10 SQL by elapsed time"*.

QueryForge only queries Oracle's data dictionary and performance views (`V$`,
`DBA_HIST_`, `DBA_`…). Application tables — including the sample `EMPLOYEES` and
`DEPARTMENTS` above — are refused, and so are requests to list schemas or users.

Keep it on `127.0.0.1` — there is one shared login per running app and no
cookies, so don't expose it with `--host 0.0.0.0`.

## 6. Optional: tracing with Phoenix

Leave `PHOENIX_COLLECTOR_ENDPOINT` blank in `.env` and tracing stays off — the app
works the same. To see a trace of every question:

```bash
# without Docker
uv tool install arize-phoenix
phoenix serve

# or with Docker
docker run -d --name qf-phoenix -p 6006:6006 \
  -e PHOENIX_WORKING_DIR=/mnt/data -v qf-phoenix-data:/mnt/data \
  arizephoenix/phoenix:version-20.19.0
```

Then set `PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006` in `.env`, restart
QueryForge, and open http://localhost:6006 → project **queryforge**. More in the
README's [Observability](README.md#observability) section.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Login says *Could not connect … DPY-6005 … Connection refused* | Database not running, or wrong `host`/`port` in `databases.json` |
| *ORA-12514* | Wrong `service_name` (e.g. `FREEPDB1` vs `XEPDB1`) |
| *Invalid username or password* | Wrong password, or the user doesn't exist in that database |
| Login screen shows a `databases.json` error | File missing or invalid JSON — the message names the problem |
| Agent says a view doesn't exist / *ORA-00942* | Grant `SELECT_CATALOG_ROLE` to that user |
| *Only Oracle data dictionary and performance views … may be queried* | Expected: the question touched an application table |
| `DBA_HIST_*` (AWR) questions return nothing | AWR needs Enterprise Edition + Diagnostics Pack; XE/Free have no AWR history |
| *No GEMINI_API_KEY set …* | Set `GEMINI_API_KEY` in `.env` and restart |
