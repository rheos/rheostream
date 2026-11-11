"""The ledger database: role privileges, the version table, the chain's fallback DSN
and the writer lock.

The privilege tests use the session's ``ws_*`` database through the conftest
fixtures, whose connections run as ``finance_owner`` or ``finance_reader``. The
standalone runtime's tests (``finance db up``, ``db init``, ``db locked``, the compose
pins and the native PostgreSQL 17 dump and restore seam) went with that runtime, which
was not imported into rheoStream.
"""

from __future__ import annotations

import contextvars
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from rheo_finance import db
from tests.finance_support import DatabaseUnderTest

# --- role privileges -----------------------------------------------------------------


def _expect_denied(engine: Engine, statement: str) -> None:
    with engine.connect() as connection:
        with pytest.raises(DBAPIError) as caught:
            connection.execute(text(statement))
        connection.rollback()
    assert isinstance(caught.value.orig, psycopg.errors.InsufficientPrivilege), (
        statement
    )


def test_reader_can_connect_with_usage_only(
    reader_engine: Engine, test_database: DatabaseUnderTest
) -> None:
    with reader_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT current_user, current_database(),"
                " has_schema_privilege('finance', 'USAGE'),"
                " has_schema_privilege('finance', 'CREATE')"
            )
        ).one()
    assert tuple(row) == (
        "finance_reader",
        test_database.reader_url.database,
        True,
        False,
    )


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE finance.reader_probe (id integer)",
        "CREATE VIEW finance.reader_probe AS SELECT 1 AS one",
        "CREATE TABLE public.reader_probe (id integer)",
        "CREATE TEMPORARY TABLE reader_probe (id integer)",
        "CREATE SCHEMA reader_probe",
    ],
)
def test_reader_cannot_run_ddl(reader_engine: Engine, statement: str) -> None:
    _expect_denied(reader_engine, statement)


def test_reader_cannot_read_or_write_any_finance_table(
    owner_conn: Connection, reader_engine: Engine
) -> None:
    tables = owner_conn.execute(
        text(
            "SELECT c.relname, a.attname FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = 1"
            " WHERE n.nspname = 'finance' AND c.relkind IN ('r', 'p')"
            " ORDER BY c.relname"
        )
    ).all()
    assert tables, "the migrated schema has no table to probe"
    for table, column in tables:
        name = f'finance."{table}"'
        # DEFAULT is valid for GENERATED ALWAYS columns too, so the probe
        # reaches permission checking rather than a column-assignment refusal.
        for statement in (
            f"INSERT INTO {name} DEFAULT VALUES",
            f'UPDATE {name} SET "{column}" = DEFAULT',
            f"DELETE FROM {name}",
            f"TRUNCATE {name}",
            f"SELECT * FROM {name}",
        ):
            _expect_denied(reader_engine, statement)


def test_reader_cannot_call_a_function_the_owner_creates(
    owner_engine: Engine, reader_engine: Engine
) -> None:
    # The harness applied the owner's default privileges when it set the database up.
    name = f"finance.exec_probe_{secrets.token_hex(4)}"
    with owner_engine.begin() as connection:
        connection.execute(
            text(f"CREATE FUNCTION {name}() RETURNS integer LANGUAGE sql AS 'SELECT 1'")
        )
    try:
        with owner_engine.connect() as connection:
            assert connection.execute(text(f"SELECT {name}()")).scalar_one() == 1
        _expect_denied(reader_engine, f"SELECT {name}()")
    finally:
        with owner_engine.begin() as connection:
            connection.execute(text(f"DROP FUNCTION IF EXISTS {name}()"))


def test_owner_writes_inside_its_rolled_back_transaction(
    owner_conn: Connection, owner_engine: Engine
) -> None:
    owner_conn.execute(text("CREATE TABLE finance.rollback_probe (id integer)"))
    owner_conn.execute(text("INSERT INTO finance.rollback_probe VALUES (1)"))
    count = owner_conn.execute(text("SELECT count(*) FROM finance.rollback_probe"))
    assert count.scalar_one() == 1
    with owner_engine.connect() as other:
        seen = other.execute(text("SELECT to_regclass('finance.rollback_probe')"))
        assert seen.scalar_one() is None


def test_the_previous_test_left_nothing_behind(owner_engine: Engine) -> None:
    # Runs after the test above (file order): its table must be gone.
    with owner_engine.connect() as connection:
        seen = connection.execute(text("SELECT to_regclass('finance.rollback_probe')"))
        assert seen.scalar_one() is None


def test_owner_cannot_create_outside_schema_finance(owner_engine: Engine) -> None:
    _expect_denied(owner_engine, "CREATE TABLE public.owner_probe (id integer)")
    _expect_denied(owner_engine, "CREATE SCHEMA owner_probe")


# --- the migration chain -----------------------------------------------------------


def test_version_table_is_finance_alembic_version_finance(
    owner_conn: Connection, test_database: DatabaseUnderTest
) -> None:
    schemas = owner_conn.execute(
        text(
            "SELECT schemaname FROM pg_tables WHERE tablename = :name"
            " ORDER BY schemaname"
        ),
        {"name": db.VERSION_TABLE},
    ).scalars()
    assert list(schemas) == ["finance"]
    version = owner_conn.execute(
        text("SELECT version_num FROM finance.alembic_version_finance")
    ).scalar_one()
    assert version == test_database.head == db.head_revision()


def test_every_owner_relation_is_in_schema_finance(owner_conn: Connection) -> None:
    outside = owner_conn.execute(
        text(
            "SELECT n.nspname || '.' || c.relname FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE pg_get_userbyid(c.relowner) = 'finance_owner'"
            " AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')"
            " AND n.nspname <> 'finance'"
        )
    ).scalars()
    assert list(outside) == []
    in_public = owner_conn.execute(
        text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
    )
    assert in_public.scalar_one() == 0


def test_env_py_falls_back_to_the_owner_dsn_in_db_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_database: DatabaseUnderTest,
) -> None:
    dsn = test_database.owner_url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    env_file = tmp_path / db.ENV_FILE_NAME
    env_file.write_text(f"{db.OWNER_DSN_KEY}={dsn}\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))

    config = db.alembic_config()
    assert "connection" not in config.attributes
    command.upgrade(config, "head")  # no connection passed: env.py opens its own


# --- the writer lock ---------------------------------------------------------------


def _wait_marker(marker: Path, parent: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 60
    while not marker.exists() and parent.poll() is None:
        assert time.monotonic() < deadline, "process marker deadline"
        time.sleep(0.01)
    assert marker.exists(), f"parent exited before {marker.name}: {parent.poll()}"


def test_copied_writer_context_cannot_bypass_another_process(tmp_path: Path) -> None:
    def enter() -> None:
        with db.writer_lock(tmp_path, wait_seconds=0):
            pass

    with db.writer_lock(tmp_path, wait_seconds=0):
        saved = contextvars.copy_context()
        saved.run(enter)  # Real ownership still permits ordinary nested calls.
    script = r"""
import sys, time
from pathlib import Path
from rheo_finance import db
root = Path(sys.argv[1])
with db.writer_lock(root, wait_seconds=0):
    (root/'holding').touch()
    while not (root/'release').exists():
        time.sleep(0.01)
"""
    other = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)], start_new_session=True, text=True
    )
    try:
        _wait_marker(tmp_path / "holding", other)
        with pytest.raises(db.WriterBusy):
            enter()
        with pytest.raises(db.WriterBusy):
            saved.run(enter)
    finally:
        (tmp_path / "release").touch()
        try:
            other.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(other.pid, signal.SIGKILL)
            other.wait(timeout=8)
    assert other.returncode == 0
    saved.run(enter)
