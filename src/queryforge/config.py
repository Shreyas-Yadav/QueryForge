"""Application configuration loaded from environment / .env.

Model-provider settings and query guardrails live here. Database credentials do
not: the databases on offer come from ``databases.json`` (see
:mod:`queryforge.connections`) and the password is typed at login, so nothing
secret about the database is stored on disk.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OracleProfile(BaseModel):
    """Connection parameters for the active login.

    Built from a ``databases.json`` entry plus the user and password typed at
    login. ``target`` is the entry's display name.
    """

    target: str
    user: str
    password: str = Field(repr=False)
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

    # --- Databases offered on the login screen ---
    databases_file: Path = Field(
        Path("databases.json"),
        description="JSON list of databases (host, port, service, users) shown on "
        "the login screen. Passwords are never stored here — they are typed at login.",
    )

    # --- Query guardrails ---
    max_rows: int = Field(200, ge=1, description="Hard cap on rows returned per query.")
    query_timeout_s: int = Field(
        30, ge=1, description="Per-query wall-clock timeout (oracledb call_timeout)."
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (validated once per process)."""
    return Settings()  # type: ignore[call-arg]
