"""Tests for DB helpers.

Pure helpers (value coercion, type formatting) and target selection run
everywhere. Live-database checks are gated on Oracle credentials and skip
otherwise.
"""

from __future__ import annotations

import datetime as dt
import decimal
import os

import pytest

from queryforge import db
from queryforge.config import Settings

CLOUD_ENV = {
    "cloud_oracle_user": "qf_readonly",
    "cloud_oracle_password": "cloud-pw",
    "cloud_oracle_dsn": "mydb_low",
    "cloud_oracle_config_dir": "/wallet",
    "cloud_oracle_wallet_location": "/wallet",
    "cloud_oracle_wallet_password": "pem-pw",
}
LOCAL_ENV = {
    "local_oracle_user": "system",
    "local_oracle_password": "oracle",
    "local_oracle_dsn": "localhost:1521/FREEPDB1",
}


def make_settings(**overrides) -> Settings:
    """Build Settings from explicit values only, ignoring any real .env file."""
    return Settings(_env_file=None, **overrides)


def test_to_jsonable_scalars():
    assert db._to_jsonable(None) is None
    assert db._to_jsonable("x") == "x"
    assert db._to_jsonable(3) == 3
    assert db._to_jsonable(True) is True


def test_to_jsonable_decimal_int_vs_float():
    assert db._to_jsonable(decimal.Decimal("42")) == 42
    assert isinstance(db._to_jsonable(decimal.Decimal("42")), int)
    assert db._to_jsonable(decimal.Decimal("3.5")) == 3.5


def test_to_jsonable_datetime():
    assert db._to_jsonable(dt.date(2024, 1, 2)) == "2024-01-02"
    assert db._to_jsonable(dt.datetime(2024, 1, 2, 3, 4, 5)).startswith("2024-01-02T03:04:05")


def test_to_jsonable_bytes_base64():
    assert db._to_jsonable(b"\x00\x01") == "AAE="


def test_format_type():
    assert db._format_type("NUMBER", None, 10, 2) == "NUMBER(10,2)"
    assert db._format_type("NUMBER", None, 10, 0) == "NUMBER(10)"
    assert db._format_type("VARCHAR2", 100, None, None) == "VARCHAR2(100)"
    assert db._format_type("DATE", None, None, None) == "DATE"


def test_merge_listing_tags_object_type():
    tables = [{"table_name": "ORDERS", "comments": "Sales orders"}]
    synonyms = [
        {
            "synonym_name": "CUSTOMERS",
            "table_owner": "SALES",
            "table_name": "CUSTOMER_T",
            "comments": "Customer master",
        }
    ]
    merged = db._merge_listing(tables, synonyms)

    assert merged == [
        {"table_name": "ORDERS", "comments": "Sales orders", "object_type": "TABLE"},
        {
            "table_name": "CUSTOMERS",
            "comments": "Customer master",
            "object_type": "SYNONYM",
            "points_to": "SALES.CUSTOMER_T",
        },
    ]


def test_merge_listing_tables_come_first():
    tables = [{"table_name": "ZZZ", "comments": None}]
    synonyms = [
        {"synonym_name": "AAA", "table_owner": "S", "table_name": "T", "comments": None}
    ]
    merged = db._merge_listing(tables, synonyms)
    assert [m["table_name"] for m in merged] == ["ZZZ", "AAA"]


def test_merge_listing_table_shadows_synonym_of_same_name():
    tables = [{"table_name": "ORDERS", "comments": "real table"}]
    synonyms = [
        {
            "synonym_name": "ORDERS",
            "table_owner": "OTHER",
            "table_name": "ORDERS_T",
            "comments": "synonym",
        }
    ]
    merged = db._merge_listing(tables, synonyms)

    assert len(merged) == 1
    assert merged[0]["object_type"] == "TABLE"
    assert merged[0]["comments"] == "real table"


# --- target selection ---------------------------------------------------------


def test_local_target_selects_local_profile_without_wallet():
    profile = make_settings(db_target="local", **CLOUD_ENV, **LOCAL_ENV).db

    assert profile.target == "local"
    assert profile.user == "system"
    assert profile.dsn == "localhost:1521/FREEPDB1"
    assert profile.uses_wallet is False
    # Cloud credentials sit alongside but must not leak into the local profile.
    assert profile.wallet_location is None
    assert profile.password == "oracle"


def test_cloud_target_selects_cloud_profile_with_wallet():
    profile = make_settings(db_target="cloud", **CLOUD_ENV, **LOCAL_ENV).db

    assert profile.target == "cloud"
    assert profile.user == "qf_readonly"
    assert profile.dsn == "mydb_low"
    assert profile.uses_wallet is True
    assert profile.wallet_password == "pem-pw"


def test_cloud_target_defaults_when_db_target_unset():
    assert make_settings(**CLOUD_ENV).db.target == "cloud"


def test_cloud_target_falls_back_to_legacy_oracle_vars():
    """A .env written before DB_TARGET existed must keep working untouched."""
    profile = make_settings(
        oracle_user="legacy_user",
        oracle_password="legacy-pw",
        oracle_dsn="legacy_low",
        oracle_schema="SHREYAS",
        oracle_config_dir="/wallet",
        oracle_wallet_location="/wallet",
        oracle_wallet_password="pem-pw",
    ).db

    assert profile.user == "legacy_user"
    assert profile.dsn == "legacy_low"
    assert profile.schema_name == "SHREYAS"
    assert profile.uses_wallet is True


def test_prefixed_cloud_vars_win_over_legacy():
    profile = make_settings(oracle_user="legacy_user", **CLOUD_ENV).db
    assert profile.user == "qf_readonly"


def test_missing_settings_for_active_target_name_the_variables():
    with pytest.raises(ValueError) as exc:
        make_settings(db_target="local", **CLOUD_ENV).db

    message = str(exc.value)
    assert "LOCAL_ORACLE_USER" in message
    assert "LOCAL_ORACLE_PASSWORD" in message
    assert "LOCAL_ORACLE_DSN" in message


def test_unknown_target_rejected():
    with pytest.raises(ValueError, match="Unknown DB_TARGET"):
        make_settings(db_target="staging", **CLOUD_ENV).db


def test_settings_load_without_any_oracle_config():
    """Code paths that never connect must not require Oracle settings."""
    cfg = make_settings()
    assert cfg.max_rows == 200  # constructing Settings alone raises nothing


def test_pool_kwargs_omit_wallet_for_local():
    kwargs = db._pool_kwargs(make_settings(db_target="local", **LOCAL_ENV).db)

    assert kwargs["user"] == "system"
    assert kwargs["dsn"] == "localhost:1521/FREEPDB1"
    assert kwargs["homogeneous"] is True
    assert "wallet_location" not in kwargs
    assert "config_dir" not in kwargs


def test_pool_kwargs_include_wallet_for_cloud():
    kwargs = db._pool_kwargs(make_settings(db_target="cloud", **CLOUD_ENV).db)

    assert kwargs["config_dir"] == "/wallet"
    assert kwargs["wallet_location"] == "/wallet"
    assert kwargs["wallet_password"] == "pem-pw"


def test_schema_name_prefers_target_schema(monkeypatch):
    cfg = make_settings(db_target="local", local_oracle_schema="APP", **LOCAL_ENV)
    monkeypatch.setattr(db, "get_settings", lambda: cfg)
    assert db._schema_name() == "APP"


def test_schema_name_falls_back_to_user(monkeypatch):
    cfg = make_settings(db_target="local", **LOCAL_ENV)
    monkeypatch.setattr(db, "get_settings", lambda: cfg)
    assert db._schema_name() == "SYSTEM"


# --- live database ------------------------------------------------------------


@pytest.mark.skipif(
    not os.getenv("ORACLE_USER"),
    reason="Live Oracle credentials not configured (set ORACLE_* in .env).",
)
def test_live_list_tables():
    tables = db.list_tables()
    assert isinstance(tables, list)
    assert all(item["object_type"] in ("TABLE", "SYNONYM") for item in tables)
    assert all(
        "points_to" in item for item in tables if item["object_type"] == "SYNONYM"
    )


@pytest.mark.skipif(
    not os.getenv("ORACLE_USER"),
    reason="Live Oracle credentials not configured (set ORACLE_* in .env).",
)
def test_live_describe_synonym_resolves():
    synonyms = [t for t in db.list_tables() if t["object_type"] == "SYNONYM"]
    if not synonyms:
        pytest.skip("No synonyms in the live schema to resolve.")
    described = db.describe_table(synonyms[0]["table_name"])
    assert described["columns"], "synonym should resolve to an object with columns"
    assert described["resolved_to"] == synonyms[0]["points_to"]
