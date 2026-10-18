"""Tests for DB helpers.

Pure helpers and login-state handling run everywhere. Live-database checks run
only when ``QF_LIVE_DATABASE``, ``QF_LIVE_USER`` and ``QF_LIVE_PASSWORD`` are set.
"""

from __future__ import annotations

import datetime as dt
import decimal
import os

import oracledb
import pytest

from queryforge import connections, db
from queryforge.config import OracleProfile, Settings

LOCAL = OracleProfile(
    target="AIWORLD (local)",
    user="shreyas",
    password="s3cret-pw",
    dsn="localhost:1539/AIWORLD",
)


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


def test_settings_load_without_any_oracle_config():
    """Constructing Settings must not require any database configuration."""
    assert Settings(_env_file=None).max_rows == 200


def test_profile_repr_hides_password():
    assert "s3cret-pw" not in repr(LOCAL)
    assert "s3cret-pw" not in str(LOCAL)


def test_pool_kwargs_omit_wallet_without_wallet():
    kwargs = db._pool_kwargs(LOCAL)

    assert kwargs["user"] == "shreyas"
    assert kwargs["dsn"] == "localhost:1539/AIWORLD"
    assert kwargs["homogeneous"] is True
    assert "wallet_location" not in kwargs
    assert "config_dir" not in kwargs


def test_pool_kwargs_include_wallet_when_configured():
    profile = LOCAL.model_copy(
        update={"config_dir": "/wallet", "wallet_location": "/wallet", "wallet_password": "pem-pw"}
    )
    kwargs = db._pool_kwargs(profile)

    assert kwargs["config_dir"] == "/wallet"
    assert kwargs["wallet_location"] == "/wallet"
    assert kwargs["wallet_password"] == "pem-pw"


def test_not_logged_in_by_default():
    with pytest.raises(db.NotLoggedIn):
        db.get_pool()
    with pytest.raises(db.NotLoggedIn):
        db.active_profile()
    with pytest.raises(db.NotLoggedIn):
        db.ping()


def test_replace_pool_installs_profile_and_pool(fake_pools):
    db.replace_pool(LOCAL)

    assert db.active_profile() is LOCAL
    assert db.get_pool() is fake_pools.created[0]


def test_failed_login_keeps_previous_login(fake_pools):
    db.replace_pool(LOCAL)
    fake_pools.next_error = oracledb.DatabaseError("ORA-01017: invalid username/password")
    other = LOCAL.model_copy(update={"user": "qf_readonly"})

    with pytest.raises(oracledb.DatabaseError):
        db.replace_pool(other)

    assert db.active_profile() is LOCAL
    assert len(fake_pools.created) == 1               # no pool built for the failed login
    assert fake_pools.created[0].closed is False      # the working one is untouched


def test_failed_login_tries_the_password_once(fake_pools):
    """A pool tries a bad password twice (seen on 19c: ORA-01017 counted +2),
    which halves the attempts left before Oracle locks the account."""
    fake_pools.next_error = oracledb.DatabaseError("ORA-01017: invalid username/password")

    with pytest.raises(oracledb.DatabaseError):
        db.replace_pool(LOCAL)

    assert fake_pools.login_attempts == 1
    assert fake_pools.created == []


def test_replace_pool_closes_the_old_pool(fake_pools):
    db.replace_pool(LOCAL)
    db.replace_pool(LOCAL.model_copy(update={"user": "qf_readonly"}))

    assert fake_pools.created[0].closed is True
    assert db.active_profile().user == "qf_readonly"


def test_close_pool_logs_out(fake_pools):
    db.replace_pool(LOCAL)
    db.close_pool()

    assert fake_pools.created[0].closed is True
    with pytest.raises(db.NotLoggedIn):
        db.active_profile()


def test_schema_name_prefers_profile_schema(fake_pools):
    db.replace_pool(LOCAL.model_copy(update={"schema_name": "app"}))
    assert db._schema_name() == "APP"


def test_schema_name_falls_back_to_user(fake_pools):
    db.replace_pool(LOCAL)
    assert db._schema_name() == "SHREYAS"


# --- live database ------------------------------------------------------------

LIVE = {k: os.getenv(k) for k in ("QF_LIVE_DATABASE", "QF_LIVE_USER", "QF_LIVE_PASSWORD")}
live = pytest.mark.skipif(
    not all(LIVE.values()),
    reason="Set QF_LIVE_DATABASE, QF_LIVE_USER and QF_LIVE_PASSWORD to run live checks.",
)


@pytest.fixture
def live_login():
    entry = connections.find(LIVE["QF_LIVE_DATABASE"])
    db.replace_pool(
        OracleProfile(
            target=entry.name,
            user=LIVE["QF_LIVE_USER"],
            password=LIVE["QF_LIVE_PASSWORD"],
            dsn=entry.dsn,
            schema_name=entry.schema_name,
        )
    )
    yield
    db.close_pool()


@live
def test_live_describe_performance_view(live_login):
    described = db.describe_table("V$SQLAREA")
    assert described["columns"]
    assert described["resolved_to"] == "SYS.V_$SQLAREA"


@live
def test_live_rejects_application_table(live_login):
    from queryforge.sql_guard import SqlGuardError

    with pytest.raises(SqlGuardError):
        db.run_select("SELECT * FROM employee")


# --- internal-objects-only policy (dictionary lookups faked) --------------------

class _DictCursor:
    """Answers the resolver's ALL_SYNONYMS / ALL_OBJECTS / ALL_USERS lookups from
    small in-memory tables and records every other statement it is asked to run."""

    SYNONYMS = {
        ("PUBLIC", "V$SQLAREA"): ("SYS", "V_$SQLAREA"),
        ("PUBLIC", "EMP_PUB"): ("SHREYAS", "EMPLOYEE"),
    }
    OBJECTS = {("SHREYAS", "EMPLOYEE"), ("SYS", "V_$SQLAREA"), ("SYS", "DUAL")}
    INTERNAL = {"SYS", "SYSTEM"}

    def __init__(self):
        self.executed: list[str] = []
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, **binds):
        low = " ".join(sql.lower().split())
        if "from all_synonyms" in low and "synonym_name = :name" in low:
            hit = self.SYNONYMS.get((binds["owner"], binds["name"]))
            self._row = (*hit, None) if hit else None
        elif "from all_objects" in low:
            hit = (binds["owner"], binds["name"]) in self.OBJECTS
            self._row = (1,) if hit else None
        elif "from all_users" in low:
            self._row = (1,) if binds["owner"] in self.INTERNAL else None
        else:
            self.executed.append(sql)
            self._row = None

    def fetchone(self):
        return self._row


@pytest.fixture
def dict_db(fake_pools, monkeypatch):
    db.replace_pool(LOCAL)
    cur = _DictCursor()

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return cur

    monkeypatch.setattr(db, "_acquire", lambda: _Conn())
    return cur


def test_resolve_object_follows_public_synonym(dict_db):
    assert db._resolve_object(dict_db, "SHREYAS", None, "V$SQLAREA") == ("SYS", "V_$SQLAREA")


def test_resolve_object_prefers_current_schema(dict_db):
    assert db._resolve_object(dict_db, "SHREYAS", None, "EMPLOYEE") == ("SHREYAS", "EMPLOYEE")


def test_resolve_object_public_synonym_onto_user_table(dict_db):
    assert db._resolve_object(dict_db, "SHREYAS", None, "EMP_PUB") == ("SHREYAS", "EMPLOYEE")


def test_resolve_object_unknown_is_none(dict_db):
    assert db._resolve_object(dict_db, "SHREYAS", None, "NOPE") is None


def test_describe_user_table_is_refused(dict_db):
    """Scenario (a): describing an application table is not allowed."""
    with pytest.raises(ValueError, match="SHREYAS"):
        db.describe_table("employee")
    assert dict_db.executed == []  # no column metadata was read


def test_run_select_on_user_table_is_refused_before_execution(dict_db):
    """Scenario (b): SELECT * FROM employee never reaches the database."""
    from queryforge.sql_guard import SqlGuardError

    with pytest.raises(SqlGuardError, match="EMPLOYEE"):
        db.run_select("select * from employee")
    assert dict_db.executed == []
