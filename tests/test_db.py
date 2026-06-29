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


@pytest.mark.skipif(
    not os.getenv("ORACLE_USER"),
    reason="Live Oracle credentials not configured (set ORACLE_* in .env).",
)
def test_live_list_tables():
    tables = db.list_tables()
    assert isinstance(tables, list)
