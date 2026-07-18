"""Application configuration loaded from environment / .env.

No Anthropic API key is used — the agent runs Claude on Google Vertex AI and
authenticates via GCP Application Default Credentials (ADC). All secrets come
from the environment; nothing is hard-coded.
"""

from __future__ import annotations

import os
import tempfile
from functools import cached_property, lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PATH = Path(".env")
DB_TARGETS = ("cloud", "local")


class OracleProfile(BaseModel):
    """Connection parameters for one Oracle target (cloud ADB or a local DB).

    Both targets are the same engine on the same thin-mode driver; they differ
    only in how the connection is addressed and authenticated. A cloud ADB uses
    an mTLS wallet, a local DB a plain ``host:port/service`` DSN.
    """

    target: str
    user: str
    password: str
    dsn: str
    schema_name: str | None = None
    config_dir: str | None = None
    wallet_location: str | None = None
    wallet_password: str | None = None

    @property
    def uses_wallet(self) -> bool:
        """True when mTLS wallet parameters are configured (vs one-way TLS)."""
        return bool(self.wallet_location and self.config_dir)


class Settings(BaseSettings):
    """Typed settings sourced from environment variables / a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Vertex AI (model provider) ---
    gcp_project_id: str | None = Field(
        None,
        description="GCP project hosting Vertex AI. Optional when GEMINI_API_KEY is "
        "set (Gemini API-key mode needs no GCP project).",
    )
    vertex_region: str = Field(
        "us-east5",
        description="Vertex region, e.g. 'us-east5' or 'global'. Only used on the "
        "Vertex path (i.e. when GEMINI_API_KEY is empty).",
    )
    gemini_api_key: str | None = Field(
        None,
        description="Google AI Studio API key. If set, the Gemini provider uses the "
        "Developer API (no GCP/Vertex/ADC needed). Otherwise it uses Vertex via ADC.",
    )
    provider: str = Field(
        "gemini",
        description="Which model backs the agent: 'gemini' or 'claude'. "
        "Both run on Vertex AI via GCP ADC.",
    )
    model: str = Field(
        "claude-sonnet-4-6",
        description="Claude model id on Vertex (no 'anthropic.' prefix). "
        "Used when provider='claude'.",
    )
    gemini_model: str = Field(
        "gemini-3.1-flash-lite",
        description="Gemini model id. Used when provider='gemini'. Defaults to the "
        "rolling 'latest' alias so a fresh API key doesn't land on a retired model.",
    )

    # --- Oracle target selection ---
    db_target: str = Field(
        "cloud",
        description="Which database the agent queries: 'cloud' (Oracle Autonomous "
        "Database over an mTLS wallet) or 'local' (Oracle in Docker or installed on "
        "this machine). Both use the same thin-mode driver, so no OS-specific setup "
        "is needed either way.",
    )

    # --- cloud target: Oracle Autonomous Database (thin mode, mTLS wallet) ---
    # Each field falls back to the unprefixed legacy ORACLE_* variable below, so a
    # pre-existing single-target .env keeps working untouched.
    cloud_oracle_user: str | None = None
    cloud_oracle_password: str | None = None
    cloud_oracle_dsn: str | None = None
    cloud_oracle_schema: str | None = None
    cloud_oracle_config_dir: str | None = None
    cloud_oracle_wallet_location: str | None = None
    cloud_oracle_wallet_password: str | None = None

    # --- local target: Docker or a native install (no wallet) ---
    local_oracle_user: str | None = None
    local_oracle_password: str | None = None
    local_oracle_dsn: str | None = Field(
        None,
        description="Local connect string, e.g. 'localhost:1521/FREEPDB1'.",
    )
    local_oracle_schema: str | None = None

    # --- legacy single-target variables (fallbacks for the cloud profile) ---
    oracle_user: str | None = Field(None, description="Read-only Oracle username.")
    oracle_password: str | None = Field(
        None, description="Password for the read-only user."
    )
    oracle_dsn: str | None = Field(
        None,
        description="TNS alias from tnsnames.ora (e.g. 'mydb_low'), or a full "
        "connect descriptor for one-way TLS.",
    )
    oracle_schema: str | None = Field(
        None,
        description="Schema the agent reads (e.g. 'SHREYAS'). Set when connecting as a "
        "read-only user whose granted tables live in another schema. Defaults to the "
        "connecting user's own schema.",
    )
    oracle_config_dir: str | None = Field(
        None,
        description="Directory containing tnsnames.ora (mTLS only).",
    )
    oracle_wallet_location: str | None = Field(
        None,
        description="Directory containing ewallet.pem (mTLS only).",
    )
    oracle_wallet_password: str | None = Field(
        None,
        description="Wallet PEM password (mTLS only; NOT the DB password).",
    )

    # --- Query guardrails ---
    max_rows: int = Field(200, ge=1, description="Hard cap on rows returned per query.")
    query_timeout_s: int = Field(
        30, ge=1, description="Per-query wall-clock timeout (oracledb call_timeout)."
    )

    @cached_property
    def db(self) -> OracleProfile:
        """The connection profile for the selected ``DB_TARGET``.

        Validation is deliberately lazy — a profile is only built (and its
        required variables enforced) when something actually connects, so code
        paths that never touch the database run with no Oracle config at all.
        """
        target = self.db_target.strip().lower()
        if target == "cloud":
            fields = {
                "user": self.cloud_oracle_user or self.oracle_user,
                "password": self.cloud_oracle_password or self.oracle_password,
                "dsn": self.cloud_oracle_dsn or self.oracle_dsn,
                "schema_name": self.cloud_oracle_schema or self.oracle_schema,
                "config_dir": self.cloud_oracle_config_dir or self.oracle_config_dir,
                "wallet_location": self.cloud_oracle_wallet_location
                or self.oracle_wallet_location,
                "wallet_password": self.cloud_oracle_wallet_password
                or self.oracle_wallet_password,
            }
        elif target == "local":
            fields = {
                "user": self.local_oracle_user,
                "password": self.local_oracle_password,
                "dsn": self.local_oracle_dsn,
                "schema_name": self.local_oracle_schema,
            }
        else:
            raise ValueError(
                f"Unknown DB_TARGET '{self.db_target}' (use 'cloud' or 'local')."
            )

        missing = [k for k in ("user", "password", "dsn") if not fields.get(k)]
        if missing:
            names = ", ".join(f"{target.upper()}_ORACLE_{k.upper()}" for k in missing)
            raise ValueError(
                f"DB_TARGET is '{target}' but required settings are missing: {names}."
            )
        return OracleProfile(target=target, **fields)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (validated once per process)."""
    return Settings()  # type: ignore[call-arg]


def _rewrite_db_target(text: str, target: str) -> str:
    """Return ``text`` with its ``DB_TARGET`` assignment set to ``target``.

    Only the assignment itself is touched — comments, ordering, blank lines and
    every other variable survive byte-for-byte, because this file holds secrets a
    human also edits by hand. Commented-out ``# DB_TARGET=`` lines are left alone.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("DB_TARGET="):
            newline = "\n" if line.endswith("\n") else ""
            lines[i] = f"DB_TARGET={target}{newline}"
            return "".join(lines)
    # Not present — append, keeping a trailing newline on the previous last line.
    prefix = "" if not text or text.endswith("\n") else "\n"
    return f"{text}{prefix}DB_TARGET={target}\n"


def set_db_target(target: str, env_path: Path | None = None) -> str:
    """Persist ``DB_TARGET`` to .env and drop the cached Settings.

    The write is atomic (temp file + replace) so an interrupted switch can never
    truncate a file holding database credentials. ``os.environ`` is updated too:
    a real environment variable outranks .env in pydantic-settings, so leaving it
    stale would silently ignore the value just written.

    Callers must still reset anything derived from the old database — see
    :func:`queryforge.db.close_pool` and :func:`queryforge.prompt.clear_schema_cache`.
    """
    target = target.strip().lower()
    if target not in DB_TARGETS:
        raise ValueError(f"Unknown target '{target}' (use {' or '.join(DB_TARGETS)}).")

    path = env_path or ENV_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — copy .env.example to .env before switching targets."
        )

    original = path.read_text(encoding="utf-8")
    updated = _rewrite_db_target(original, target)

    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=path.parent or ".", prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(updated)
        os.chmod(tmp, mode)  # mkstemp is 0600; keep whatever .env had
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    os.environ["DB_TARGET"] = target
    get_settings.cache_clear()
    return target
