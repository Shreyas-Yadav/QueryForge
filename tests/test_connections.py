"""Tests for loading and validating databases.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from queryforge import connections
from queryforge.connections import DatabasesFileError, load_databases

VALID = {
    "name": "AIWORLD (local)",
    "host": "localhost",
    "port": 1539,
    "service_name": "AIWORLD",
    "schema": "SHREYAS",
    "users": ["qf_readonly", "shreyas"],
}
MISSING = object()


def entry(**changes) -> dict:
    """VALID with ``changes`` applied; a value of MISSING deletes the key."""
    out = dict(VALID)
    for key, value in changes.items():
        if value is MISSING:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def write(tmp_path: Path, data) -> Path:
    path = tmp_path / "databases.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_loads_valid_file(tmp_path):
    [db] = load_databases(write(tmp_path, [VALID]))

    assert db.name == "AIWORLD (local)"
    assert db.port == 1539
    assert db.schema_name == "SHREYAS"
    assert db.users == ["qf_readonly", "shreyas"]
    assert db.dsn == (
        "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=localhost)(PORT=1539))"
        "(CONNECT_DATA=(SERVICE_NAME=AIWORLD)))"
    )


def test_port_defaults_to_1521_and_schema_is_optional(tmp_path):
    [db] = load_databases(write(tmp_path, [entry(port=MISSING, schema=MISSING)]))
    assert db.port == 1521
    assert db.schema_name is None


@pytest.mark.parametrize(
    "bad, fragment",
    [
        (entry(port=0), "port"),
        (entry(port=70000), "port"),
        (entry(port="1539"), "port"),
        (entry(users=[]), "users"),
        (entry(users=["shreyas", "SHREYAS"]), "duplicate"),
        (entry(users=["  "]), "blank"),
        (entry(host=MISSING), "host"),
        (entry(service_name=MISSING), "service_name"),
        (entry(name="   "), "name"),
        (entry(servicename="AIWORLD"), "servicename"),
    ],
)
def test_rejects_invalid_entries(tmp_path, bad, fragment):
    path = write(tmp_path, [bad])
    with pytest.raises(DatabasesFileError) as exc:
        load_databases(path)
    assert fragment in str(exc.value)
    assert str(path) in str(exc.value)


def test_rejects_duplicate_names_case_insensitively(tmp_path):
    with pytest.raises(DatabasesFileError, match="duplicate database name"):
        load_databases(write(tmp_path, [VALID, entry(name="aiworld (LOCAL)")]))


def test_rejects_empty_list(tmp_path):
    with pytest.raises(DatabasesFileError, match="no databases"):
        load_databases(write(tmp_path, []))


def test_rejects_non_list(tmp_path):
    with pytest.raises(DatabasesFileError):
        load_databases(write(tmp_path, VALID))


def test_missing_file_names_the_path(tmp_path):
    path = tmp_path / "nope.json"
    with pytest.raises(DatabasesFileError) as exc:
        load_databases(path)
    assert str(path) in str(exc.value)
    assert "not found" in str(exc.value)


def test_invalid_json_names_the_path(tmp_path):
    path = write(tmp_path, "{not json")
    with pytest.raises(DatabasesFileError) as exc:
        load_databases(path)
    assert str(path) in str(exc.value)
    assert "not valid JSON" in str(exc.value)


def test_find_is_case_insensitive(tmp_path):
    path = write(tmp_path, [VALID])
    assert connections.find("  aiworld (LOCAL) ", path).name == "AIWORLD (local)"


def test_find_unknown_raises_keyerror(tmp_path):
    with pytest.raises(KeyError):
        connections.find("Nope", write(tmp_path, [VALID]))


def test_default_path_comes_from_settings(tmp_path, monkeypatch):
    from queryforge import config

    path = write(tmp_path, [VALID])
    monkeypatch.setenv("DATABASES_FILE", str(path))
    config.get_settings.cache_clear()
    try:
        assert load_databases()[0].name == "AIWORLD (local)"
    finally:
        config.get_settings.cache_clear()


def test_example_file_is_valid():
    example = Path(__file__).resolve().parents[1] / "databases.example.json"
    assert load_databases(example)
