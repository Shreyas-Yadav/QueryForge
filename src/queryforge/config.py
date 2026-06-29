"""Application configuration loaded from environment / .env.

No Anthropic API key is used — the agent runs Claude on Google Vertex AI and
authenticates via GCP Application Default Credentials (ADC). All secrets come
from the environment; nothing is hard-coded.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed settings sourced from environment variables / a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Vertex AI (model provider) ---
    gcp_project_id: str = Field(..., description="GCP project hosting Vertex AI.")
    vertex_region: str = Field(
        "us-east5",
        description="Vertex region, e.g. 'us-east5' or 'global'.",
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
        "gemini-2.5-flash",
        description="Gemini model id on Vertex. Used when provider='gemini'.",
    )

    # --- Oracle Autonomous Database (thin mode, mTLS wallet) ---
    oracle_user: str = Field(..., description="Read-only Oracle username.")
    oracle_password: str = Field(..., description="Password for the read-only user.")
    oracle_dsn: str = Field(
        ...,
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

    @property
    def uses_wallet(self) -> bool:
        """True when mTLS wallet parameters are configured (vs one-way TLS)."""
        return bool(self.oracle_wallet_location and self.oracle_config_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (validated once per process)."""
    return Settings()  # type: ignore[call-arg]
