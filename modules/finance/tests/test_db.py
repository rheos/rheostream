"""The ledger database: role privileges, the version table, the roles ``env.py``
prepares, acting as a role, and the writer lock.

The privilege tests use the session's ``ws_*`` database through the conftest
fixtures, whose connections run as ``finance_owner`` or ``finance_reader``. The
role-plan, ``acting_as``, ``env.py`` refusal and wait-bound tests use recording fakes
and need no database. The standalone runtime's tests (``finance db up``, ``db init``,
``db locked``, the compose pins and the native PostgreSQL 17 dump and restore seam)
went with that runtime, which was not imported into rheoStream.
"""

from __future__ import annotations

import itertools
import secrets
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.runtime.environment import EnvironmentContext
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


# --- recording fakes (no database) ---------------------------------------------------


class _Result:
    def __init__(self, value: Any = None, rows: tuple[Any, ...] = ()) -> None:
        self._value = value
        self._rows = rows

    def scalar_one(self) -> Any:
        return self._value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalars(self) -> Iterator[Any]:
        return iter(self._rows)


class Recording:
    """A connection that records each statement; each answer is the next scripted
    value, else None."""

    def __init__(self, *answers: Any) -> None:
        self.statements: list[str] = []
        self._answers = list(answers)

    def execute(self, statement: Any, parameters: Any = None) -> _Result:
        self.statements.append(str(statement))
        return _Result(self._answers.pop(0) if self._answers else None)


class Catalog(Recording):
    """A connection that answers ``prepare_roles``' catalogue reads."""

    def __init__(
        self,
        *,
        roles: tuple[str, ...],
        superuser: bool,
        can_set: bool,
        schema_owner: str | None,
    ) -> None:
        super().__init__()
        self._roles = roles
        self._superuser = superuser
        self._can_set = can_set
        self._schema_owner = schema_owner

    def execute(self, statement: Any, parameters: Any = None) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        if "rolname IN" in sql:
            return _Result(rows=self._roles)
        if "rolsuper" in sql:
            return _Result(self._superuser)
        if "pg_has_role" in sql:
            return _Result(self._can_set)
        if "nspowner" in sql:
            return _Result(self._schema_owner)
        return _Result()


def _as_connection(fake: Recording) -> Connection:
    return fake  # type: ignore[return-value]


# --- acting as a role (L4) ---------------------------------------------------------


def test_acting_as_sets_the_role_locally_and_resets_it_on_return() -> None:
    fake = Recording()
    with db.acting_as(_as_connection(fake), db.OWNER_ROLE):
        fake.execute(text("SELECT 1"))
    assert fake.statements == ["SET LOCAL ROLE finance_owner", "SELECT 1", "RESET ROLE"]


def test_acting_as_leaves_the_reset_to_the_rollback_after_an_error() -> None:
    fake = Recording()
    with pytest.raises(RuntimeError, match="synthetic"):
        with db.acting_as(_as_connection(fake), db.READER_ROLE):
            raise RuntimeError("synthetic")
    assert fake.statements == ["SET LOCAL ROLE finance_reader"]


def test_acting_as_refuses_any_other_role_before_any_sql() -> None:
    fake = Recording()
    with pytest.raises(db.DbError):
        with db.acting_as(_as_connection(fake), "postgres"):
            pytest.fail("entered as another role")
    assert fake.statements == []


# --- the role plan (L7) ------------------------------------------------------------

PLANS = {
    (True, True, True): "reuse",
    (True, True, False): "reuse",
    (True, False, True): "reuse",
    (True, False, False): "refuse",
    (False, True, True): "create",
    (False, True, False): "refuse",
    (False, False, True): "create",
    (False, False, False): "refuse",
}


@pytest.mark.parametrize(
    ("roles_exist", "can_set_both", "is_superuser"),
    list(itertools.product((True, False), repeat=3)),
)
def test_role_plan_covers_every_input(
    roles_exist: bool, can_set_both: bool, is_superuser: bool
) -> None:
    plan = db.role_plan(
        roles_exist=roles_exist, can_set_both=can_set_both, is_superuser=is_superuser
    )
    assert plan == PLANS[(roles_exist, can_set_both, is_superuser)]


def test_prepare_roles_creates_both_roles_and_takes_the_schema_as_superuser() -> None:
    fake = Catalog(roles=(), superuser=True, can_set=False, schema_owner="postgres")
    assert db.prepare_roles(_as_connection(fake)) == "create"
    created = [each for each in fake.statements if "CREATE ROLE" in each]
    assert [each.split("CREATE ROLE ")[1].split()[0] for each in created] == [
        "finance_owner",
        "finance_reader",
    ]
    assert all("NOLOGIN NOSUPERUSER" in each for each in created)
    assert all("duplicate_object OR unique_violation" in each for each in created)
    assert "ALTER SCHEMA finance OWNER TO finance_owner" in fake.statements
    assert fake.statements[-1] == (
        "ALTER DEFAULT PRIVILEGES FOR ROLE finance_owner"
        " REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    )
    assert not any("pg_has_role" in each for each in fake.statements)


def test_prepare_roles_reuses_roles_a_member_may_set_and_leaves_an_owned_schema() -> (
    None
):
    fake = Catalog(
        roles=(db.OWNER_ROLE, db.READER_ROLE),
        superuser=False,
        can_set=True,
        schema_owner=db.OWNER_ROLE,
    )
    assert db.prepare_roles(_as_connection(fake)) == "reuse"
    assert not any("CREATE ROLE" in each for each in fake.statements)
    assert not any("ALTER SCHEMA" in each for each in fake.statements)


def test_prepare_roles_refuses_without_changing_anything() -> None:
    fake = Catalog(roles=(), superuser=False, can_set=False, schema_owner=None)
    with pytest.raises(db.DbError, match="finance_roles_unavailable"):
        db.prepare_roles(_as_connection(fake))
    assert not any(
        word in each
        for each in fake.statements
        for word in ("CREATE", "ALTER", "GRANT")
    )


# --- env.py (L8) ---------------------------------------------------------------------


def test_env_py_refuses_to_run_without_the_orchestrator_connection() -> None:
    config = db.alembic_config()
    assert "connection" not in config.attributes
    with pytest.raises(RuntimeError, match="require the orchestrator connection"):
        command.upgrade(config, "head")


class _Stop(Exception):
    pass


def test_env_py_prepares_roles_then_configures_as_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    prepared: list[object] = []

    def configure(_self: EnvironmentContext, **kwargs: Any) -> None:
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(EnvironmentContext, "configure", configure)
    monkeypatch.setattr(db, "prepare_roles", prepared.append)
    fake = Recording()
    config = db.alembic_config()
    config.attributes["connection"] = fake
    with pytest.raises(_Stop):
        command.upgrade(config, "head")
    assert prepared == [fake]
    assert seen["connection"] is fake
    assert (seen["version_table_schema"], seen["version_table"]) == (
        "finance",
        "alembic_version_finance",
    )
    # Configured as the owner; no reset after the error, the caller's rollback has it.
    assert fake.statements == ["SET LOCAL ROLE finance_owner"]


# --- the writer lock ---------------------------------------------------------------


@pytest.mark.parametrize("wait", [-1.0, -0.001, 300.001, 301.0])
def test_writer_lock_refuses_a_wait_out_of_bounds_before_any_sql(wait: float) -> None:
    fake = Recording()
    with pytest.raises(db.DbError, match="between 0 and 300"):
        with db.writer_lock(_as_connection(fake), wait_seconds=wait):
            pytest.fail("entered with an out-of-bounds wait")
    assert fake.statements == []


def test_writer_lock_polls_the_transaction_lock_until_it_is_granted() -> None:
    fake = Recording(False, True)
    with db.writer_lock(_as_connection(fake), wait_seconds=5):
        pass
    assert fake.statements == ["SELECT pg_try_advisory_xact_lock(:key)"] * 2


def test_writer_lock_raises_writer_busy_once_the_wait_runs_out() -> None:
    fake = Recording(False, False, False)
    with pytest.raises(db.WriterBusy, match="writer_busy"):
        with db.writer_lock(_as_connection(fake), wait_seconds=0):
            pytest.fail("entered while another writer held the lock")
    assert len(fake.statements) == 1


def test_another_connection_holding_the_lock_refuses_a_writer(
    owner_engine: Engine,
) -> None:
    with owner_engine.connect() as holder:
        holder.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": db.WRITER_LOCK_KEY}
        )
        with owner_engine.connect() as writer, writer.begin():
            with pytest.raises(db.WriterBusy):
                with db.writer_lock(writer, wait_seconds=0):
                    pytest.fail("entered while another connection held the lock")


def test_the_lock_stacks_on_its_connection_and_ends_with_the_transaction(
    owner_engine: Engine,
) -> None:
    with owner_engine.connect() as first, owner_engine.connect() as second:
        with first.begin():
            with db.writer_lock(first, wait_seconds=0):
                with db.writer_lock(first, wait_seconds=0):
                    pass
            # Still held: the lock ends with the transaction, not the block.
            with second.begin(), pytest.raises(db.WriterBusy):
                with db.writer_lock(second, wait_seconds=0):
                    pytest.fail("entered before the first transaction ended")
        with second.begin(), db.writer_lock(second, wait_seconds=0):
            pass
