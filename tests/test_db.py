"""Tests for DB helpers.

Pure helpers (value coercion, type formatting) run everywhere. Live-database
checks are gated on Oracle credentials and skip otherwise.
"""

from __future__ import annotations

import datetime as dt
import decimal
import os

import pytest

from queryforge import db


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
