# Database login — design

**Date:** 2026-10-15
**Status:** Draft, awaiting review

## Goal

Let the person running QueryForge pick which Oracle database and which Oracle user
the AI agent connects as, from a login screen in the web UI, typing the password at
runtime instead of storing it in `.env`.

## Context and constraints

- **Standalone, single user.** QueryForge runs locally (`uvicorn` on `127.0.0.1`,
  opened in a browser). It is not deployed to a shared server. One active login for
  the whole process is sufficient; there is no per-browser session isolation.
- **Local databases only for now.** No mTLS wallet / cloud ADB support in this change.
  `OracleProfile` keeps its wallet fields so cloud can be re-added later by extending
  the JSON schema.
- **Passwords are never persisted.** They live only in process memory (the active
  profile and the connection pool). Restarting the server requires logging in again.

## Current state (what changes)

Today credentials come from `.env` (`DB_TARGET` + `CLOUD_ORACLE_*` / `LOCAL_ORACLE_*`
/ legacy `ORACLE_*`), resolved by `Settings.db` in `config.py` into one
`OracleProfile`, which feeds a single process-wide pool in `db.py`. A header button
calls `POST /db-target`, which rewrites `DB_TARGET` in `.env` via `set_db_target`.

All of that database configuration is **removed** and replaced by `databases.json`
plus an in-memory login.

## Design

### 1. `databases.json` — the list of connectable databases

- Location: `./databases.json` by default; overridable with `DATABASES_FILE` in `.env`.
- `databases.json` is git-ignored. A `databases.example.json` is committed.
- Shape — a JSON array of entries:

```json
[
  {
    "name": "AIWORLD (local)",
    "host": "localhost",
    "port": 1539,
    "service_name": "AIWORLD",
    "schema": "SHREYAS",
    "users": ["qf_readonly", "shreyas"]
  }
]
```

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Display name in the dropdown; unique across entries (case-insensitive). |
| `host` | yes | Listener host. |
| `port` | no | Integer 1–65535; defaults to `1521`. |
| `service_name` | yes | Oracle service name (e.g. PDB name). |
| `schema` | no | Schema the agent reads; defaults to the connecting user. |
| `users` | yes | Non-empty list of Oracle usernames allowed for this database; unique within the entry. |

Unknown keys are rejected (catches typos such as `"servicename"`).

### 2. `connections.py` (new) — load and validate the file

- Pydantic models: `DatabaseEntry` (fields above) and a loader
  `load_databases(path: Path | None = None) -> list[DatabaseEntry]`.
- `DatabaseEntry.dsn` builds the connect string with
  `oracledb.makedsn(host, port, service_name=service_name)`, which yields
  `(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=…)(PORT=…))(CONNECT_DATA=(SERVICE_NAME=…)))`.
  TCP and dedicated server are Oracle's defaults, so they are not configurable.
- `find(name) -> DatabaseEntry` looks up an entry by name or raises `KeyError`.
- Errors (missing file, invalid JSON, validation failure) raise a single
  `DatabasesFileError` whose message includes the file path and the reason.
- The file is re-read on every `GET /databases` and `POST /login`, so edits take
  effect without a restart. It is small, so caching is unnecessary.

### 3. `session.py` (new) — the active login

- Module-level state guarded by a `threading.Lock`:
  the active `OracleProfile` (or `None`) and the display name of its database.
- `login(entry, user, password)`:
  1. Build an `OracleProfile(target=entry.name, user=user, password=password,
     dsn=entry.dsn, schema_name=entry.schema)`.
  2. Call `db.replace_pool(profile)` (see §4), which builds and pings a new pool.
  3. On failure: `replace_pool` closes the new pool and re-raises. **The previous
     session and pool, if any, are left untouched.**
  4. On success: `replace_pool` has swapped in the new pool and closed the old one
     (`force=True`); `session` stores the profile and calls
     `prompt.clear_schema_cache()`.
- `logout()`: close the pool, clear the profile, clear the schema cache.
- `active_profile() -> OracleProfile` raises `NotLoggedIn` when nobody is logged in.
- `status() -> dict`: `{logged_in, database, user}`.

### 4. `db.py` changes

- `get_pool()` and `_schema_name()` read from `session.active_profile()` instead of
  `get_settings().db`.
- `_pool_kwargs(profile)` is unchanged (still honours wallet fields if ever set).
- Pool ownership: `session.login` installs the pool via a small helper in `db.py`
  (e.g. `db.replace_pool(profile) -> None` that builds + pings the new pool and swaps
  it in under `_pool_lock`), so pool lifecycle stays in `db.py`. `get_pool()` raises
  `NotLoggedIn` if no pool is installed rather than lazily creating one.
- `close_pool()` is unchanged.

### 5. `config.py` changes

- Remove: `db_target`, all `cloud_oracle_*`, `local_oracle_*`, legacy `oracle_*`
  fields, the `Settings.db` property, `DB_TARGETS`, `ENV_PATH`, `_rewrite_db_target`,
  `set_db_target`.
- Add: `databases_file: Path = Path("databases.json")`.
- Keep: `OracleProfile`, provider/model settings, `max_rows`, `query_timeout_s`.

### 6. HTTP API (`web/app.py`)

| Method & path | Body | Success | Errors |
|---|---|---|---|
| `GET /databases` | — | `{databases: [{name, host, port, service_name, users}]}` | 500 `{detail}` if `databases.json` is missing/invalid |
| `POST /login` | `{database, user, password}` | 200, same body as `GET /session` | see below |
| `POST /logout` | — | 200 `{logged_in: false}` | — |
| `GET /session` | — | `{logged_in, database, user, reachable, detail}` | — |
| `POST /query` | `{question}` | SSE stream (unchanged) | 401 if not logged in |
| `GET /health` | — | unchanged; reports `degraded` when not logged in | — |

`POST /login` error mapping:

| Cause | Status | `detail` |
|---|---|---|
| `databases.json` missing/invalid | 500 | file path + validation message |
| Unknown database name | 400 | `Unknown database '<name>'` |
| User not in that entry's `users` | 400 | `User '<user>' is not configured for '<name>'` |
| Empty password | 400 | `Password is required` |
| ORA-01017 (invalid credentials) | 401 | `Invalid username or password` |
| Any other connect error (DPY-6005, ORA-12514, …) | 502 | `Can't reach <host>:<port>/<service>: <driver message>` |

- Removed: `GET /db-target`, `POST /db-target`, `_switch_lock`, `_target_status`.
  Login/logout are serialised by the lock in `session.py`.
- `/query` checks `session.active_profile()` **before** creating the
  `StreamingResponse`, so a logged-out request gets a real 401 instead of an SSE
  error event.
- Audit log (`queryforge_audit.log`): one line per login attempt with database,
  user and outcome (`success` / `invalid credentials` / `unreachable` / `rejected`),
  and one per logout. The password is never logged; the login request model's
  `password` field is excluded from any repr/logging.

### 7. Web UI (`web/static/index.html`)

- On load: `GET /session`. If logged out, show the login card and hide the
  question form and results; if logged in, show the query UI.
- **Login card** (centred, existing dark styling):
  - *Database* `<select>` from `GET /databases`; option text
    `"<name> — <host>:<port>/<service_name>"`.
  - *User* `<select>` repopulated when the database changes; preselected when the
    entry has exactly one user.
  - *Password* `<input type=password autocomplete="current-password">`.
  - *Connect* button, disabled while the request is in flight.
  - Inline error area for the `detail` from a failed login. If `GET /databases`
    fails, show its error and disable Connect.
- The password input is cleared after every attempt (success or failure) and is
  never stored in JS variables beyond the request or in `localStorage`.
- **Header:** the `#db-switch` toggle is replaced by
  `● <user> @ <database>` plus a **Log out** button. The status dot keeps its
  green/red meaning from `/session.reachable`.
- Any `401` from `/query` returns the UI to the login card.

### 8. Security notes

- No cookies or tokens: anything that can reach the server shares the active login.
  This is acceptable only because the app binds to `127.0.0.1`. The README states
  this explicitly and warns against `--host 0.0.0.0`.
- The read-only guarantee is unchanged: the SQL guard still applies to every query
  regardless of which user logs in. The README continues to recommend a read-only
  Oracle user.
- The `users` allow-list is a convenience and a guard against arbitrary usernames
  in hand-crafted requests; Oracle authentication remains the real check.

### 9. Docs and config files

- `README.md`: replace the `DB_TARGET` / cloud-local / switch-button material with a
  "Connecting" section covering `databases.json`, the login screen, and the localhost
  note. Cloud/wallet instructions are removed (to be reinstated when cloud returns).
- `.env.example`: drop the database blocks; add `DATABASES_FILE=databases.json`.
- `.gitignore`: add `databases.json`.
- New `databases.example.json` with one local entry.

## Testing

No live database or model is required; pool creation and `ping` are monkeypatched.

- **`tests/test_connections.py`**
  - Valid file loads; `port` defaults to 1521; `dsn` equals `oracledb.makedsn(...)`.
  - Rejected: duplicate names, empty `users`, duplicate users, port out of range or
    non-integer, missing `host`/`service_name`, unknown key, invalid JSON, missing file.
  - Error messages include the file path.
- **`tests/test_auth.py`** (FastAPI `TestClient`)
  - `/session` reports logged out initially; `/query` returns 401.
  - Successful login: `/session` shows database + user; schema cache is cleared.
  - ORA-01017 → 401; unreachable → 502; unknown DB / unlisted user / empty password → 400.
  - Failed login while already logged in leaves the previous session active.
  - Logout clears the session, closes the pool, and clears the schema cache.
  - Password never appears in captured log output.
- **Removed:** `tests/test_db_target.py` (its subject, `set_db_target`, is deleted).
- **Kept:** `tests/test_web.py`, `tests/test_db.py`, `tests/test_sql_guard.py`, and the
  agent tests, edited only where they reference `Settings.db` or `/db-target`.

## Out of scope

- Cloud ADB / mTLS wallet entries (planned later).
- SID-based connections, non-TCP protocols, shared-server mode.
- Adding or editing databases from the UI.
- Multi-user or deployed-server operation (per-session pools, auth tokens).
- Packaging as a desktop executable.
