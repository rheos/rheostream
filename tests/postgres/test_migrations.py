"""B16, the migration half, and the orchestrator's refusals.

Seams: ``env.py``'s no-connection ``RuntimeError``; ``run_chain``'s
``current_database()`` assertion and its transaction requirement; the advisory lock
(two concurrent orchestrators serialise, the second observed waiting in ``pg_locks``);
``schema_ahead``; a failing core revision leaving ``unavailable`` with the error while
startup completes; the exact table sets each chain creates (the hard 0c boundary); and
``tests/conftest.py``'s own fail-fast contract, proved in a child pytest.
"""

import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import psycopg.errors
import pytest
from alembic import command
from conftest import REMEDY, ClusterSession, MakeWorkspace, run_pytest_in_subprocess
from rheo_core.migrations.orchestrator import (
    ADVISORY_LOCK_KEY,
    CHAINS,
    CONTROL_CHAIN,
    CORE_CHAIN,
    MigrationResult,
    build_config,
    known_revisions,
    migrate_active_workspaces,
    migrate_control,
    migrate_workspace,
    recorded_revisions,
    run_chain,
    script_location,
)
from rheo_core.refs import uuid7
from rheo_core.storage import control_tables
from rheo_core.storage.backend import (
    DATABASE_MISMATCH,
    SCHEMA_AHEAD,
    WORKSPACE_MISSING,
    WORKSPACE_UNAVAILABLE,
    StorageRefusal,
)
from rheo_core.storage.control_tables import WORKSPACE_STATES, WorkspaceState
from rheo_core.storage.postgres import advisory_lock
from rheo_core.storage.provisioning import repair
from rheo_core.storage.routing import active_workspace
from sqlalchemy import Engine, text, update
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres

_REPO_ROOT = Path(__file__).resolve().parents[2]

CONTROL_TABLES = {
    # 0001_control_plane
    "account",
    "identity",
    "workspace",
    "membership",
    "session",
    "session_secret",
    "session_grant",
    "access_token",
    "access_token_operation",
    "identity_provider",
    # 0002_work_index
    "workspace_work_due",
}
CORE_TABLES = {
    # 0001_core_schema
    "workspace_composition",
    "module_state",
    "module_schema_version",
    "workspace_setting",
    "member_setting",
    "member_credential",
    # 0002_durable_work
    "outbox_event",
    "event_delivery",
    "consumer_processed",
    "job",
    "schedule",
    "operation",
    "audit_record",
}
# The approval family, owned by a later chain step; their presence here would
# contaminate the trial. The seven durable-work tables left this set when
# 0002_durable_work created them and joined CORE_TABLES above — that move is the
# whole of the change, and this set is narrowed by name rather than by a comparison
# operator so that a table slipping back in still fails.
ZERO_C_TABLES = {
    "approval",
    "approval_payload",
    "standing_grant",
    "standing_grant_operation",
    "external_action",
}


def tables_in(engine: Engine, schema: str) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema"
            ),
            {"schema": schema},
        ).scalars()
        return {str(name) for name in rows}


def workspace_engine(cluster: ClusterSession, workspace_id: UUID) -> tuple[str, Engine]:
    row = cluster.registry_row(workspace_id)
    return row.database_name, cluster.backend.pools.engine_for(row.database_name)


def wait_until(condition: Callable[[], bool], *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


# --- no Alembic by hand ---------------------------------------------------------------


@pytest.mark.parametrize("chain", CHAINS)
def test_env_refuses_without_the_orchestrator_connection(
    cluster: ClusterSession, chain: str
) -> None:
    config = build_config(chain)
    assert config.get_main_option("sqlalchemy.url") is None
    # The attack: a hand-written ini carrying a real cluster URL. The environment
    # never reads it, so it must still refuse, and nothing may be upgraded.
    real_url = cluster.backend.pools.cluster_url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", real_url)
    with pytest.raises(RuntimeError, match="orchestrator"):
        command.upgrade(config, "head")
    with cluster.backend.maintenance_connection() as connection:
        for qualified in (
            "control.alembic_version_control",
            "core.alembic_version_core",
        ):
            present = connection.execute(
                text("SELECT to_regclass(:name)"), {"name": qualified}
            ).scalar_one()
            assert present is None, f"{qualified} appeared in the maintenance database"


def test_no_alembic_ini_is_tracked_and_each_chain_knows_its_revisions() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.split("\0")
    assert not [p for p in tracked if p.endswith("alembic.ini")]
    for chain in CHAINS:
        assert not (script_location(chain) / "alembic.ini").exists()
    # Every revision the script directory holds, not the one the database records:
    # each chain now ships two files, so both of these are both ids.
    assert known_revisions(CONTROL_CHAIN) == {"0001_control_plane", "0002_work_index"}
    assert known_revisions(CORE_CHAIN) == {"0001_core_schema", "0002_durable_work"}


# --- the two chains, exactly ----------------------------------------------------------


def test_control_chain_creates_exactly_the_eleven_tables(
    cluster: ClusterSession,
) -> None:
    engine = cluster.backend.control_engine
    assert tables_in(engine, "control") == CONTROL_TABLES | {"alembic_version_control"}
    with engine.connect() as connection:
        # The version table holds one row on a linear chain: the head, not every
        # revision the code carries.
        assert recorded_revisions(connection, CONTROL_CHAIN) == {"0002_work_index"}


def test_core_chain_creates_exactly_the_thirteen_tables(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = workspace_engine(cluster, workspace)
    names = tables_in(engine, "core")
    assert names == CORE_TABLES | {"alembic_version_core"}
    assert not names & ZERO_C_TABLES
    with engine.connect() as connection:
        # The version table holds one row on a linear chain: the head, not every
        # revision the code carries.
        assert recorded_revisions(connection, CORE_CHAIN) == {"0002_durable_work"}


def test_migrate_control_is_idempotent_and_survives_an_existing_database(
    cluster: ClusterSession,
) -> None:
    # The session already created the control database: the duplicate is success.
    assert cluster.backend.ensure_control_database() is False
    first = migrate_control(cluster.backend)
    second = migrate_control(cluster.backend)
    assert (
        first
        == second
        == MigrationResult(None, cluster.control_database, CONTROL_CHAIN, True, None)
    )


# --- the orchestrator's refusals ------------------------------------------------------


def test_current_database_assertion_refuses_a_wrong_database(
    cluster: ClusterSession, workspace: UUID
) -> None:
    database_name, engine = workspace_engine(cluster, workspace)
    with engine.begin() as connection:
        with pytest.raises(StorageRefusal) as excinfo:
            run_chain(
                connection, CORE_CHAIN, expected_database=cluster.control_database
            )
    assert excinfo.value.state == DATABASE_MISMATCH
    assert database_name in excinfo.value.detail
    # Refused before anything ran: the workspace is untouched and still active.
    assert tables_in(engine, "core") == CORE_TABLES | {"alembic_version_core"}
    assert cluster.registry_row(workspace).state is WorkspaceState.ACTIVE


def test_run_chain_refuses_a_connection_outside_a_transaction(
    cluster: ClusterSession, workspace: UUID
) -> None:
    database_name, engine = workspace_engine(cluster, workspace)
    with engine.connect() as connection:
        with pytest.raises(ValueError, match="transaction"):
            run_chain(connection, CORE_CHAIN, expected_database=database_name)


def test_migrate_workspace_refuses_an_unknown_id_and_module_chains(
    cluster: ClusterSession, workspace: UUID
) -> None:
    with pytest.raises(StorageRefusal) as excinfo:
        migrate_workspace(cluster.backend, UUID(int=0))
    assert excinfo.value.state == WORKSPACE_MISSING
    with pytest.raises(NotImplementedError, match="phase 2"):
        migrate_workspace(cluster.backend, workspace, chains=("recallatron",))
    with pytest.raises(ValueError, match="control chain"):
        migrate_workspace(cluster.backend, workspace, chains=(CONTROL_CHAIN,))
    database_name, engine = workspace_engine(cluster, workspace)
    with engine.begin() as connection:
        with pytest.raises(ValueError, match="unknown migration chain"):
            run_chain(connection, "nope", expected_database=database_name)
    # Every refusal above happened before any write: the workspace is untouched.
    assert cluster.registry_row(workspace).state is WorkspaceState.ACTIVE


# --- failure handling ----------------------------------------------------------------


def test_failing_core_revision_marks_unavailable_and_startup_completes(
    cluster: ClusterSession, make_workspace: MakeWorkspace
) -> None:
    broken = make_workspace()
    healthy = make_workspace()
    # Forget the applied revision: the chain re-runs 0001 against tables that exist,
    # and the revision fails (the tables are created with checkfirst=False).
    broken_name, broken_engine = workspace_engine(cluster, broken)
    with broken_engine.begin() as connection:
        connection.execute(text("DELETE FROM core.alembic_version_core"))

    results = migrate_active_workspaces(cluster.backend)

    by_id = {result.workspace_id: result for result in results}
    assert by_id[broken].ok is False
    assert by_id[broken].detail is not None
    assert "already exists" in by_id[broken].detail
    assert by_id[healthy].ok is True
    # The loop continued past the failure: the broken workspace ran first.
    order = [result.workspace_id for result in results]
    assert order.index(broken) < order.index(healthy)

    row = cluster.registry_row(broken)
    assert row.state is WorkspaceState.UNAVAILABLE
    assert row.state_detail is not None and "already exists" in row.state_detail
    assert cluster.registry_row(healthy).state is WorkspaceState.ACTIVE

    # Refused at routing, and the failed transaction rolled back cleanly.
    with pytest.raises(StorageRefusal) as excinfo:
        active_workspace(broken)
    assert excinfo.value.state == WORKSPACE_UNAVAILABLE
    assert excinfo.value.workspace_state == "unavailable"
    assert tables_in(broken_engine, "core") == CORE_TABLES | {"alembic_version_core"}

    # Restoring the version row and repairing brings the workspace back. The row has
    # to name the chain's head, because that is what the database physically holds:
    # this workspace was provisioned against head, so naming an earlier revision would
    # send repair's upgrade back through 0002's create_all against tables that already
    # exist.
    with broken_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO core.alembic_version_core (version_num) VALUES (:v)"),
            {"v": "0002_durable_work"},
        )
    repair(broken)
    assert cluster.registry_row(broken).state is WorkspaceState.ACTIVE


def test_schema_ahead_marks_unavailable_and_refuses_repair(
    cluster: ClusterSession, workspace: UUID
) -> None:
    database_name, engine = workspace_engine(cluster, workspace)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE core.alembic_version_core SET version_num = :v"),
            {"v": "9999_from_the_future"},
        )

    result = migrate_workspace(cluster.backend, workspace)
    assert result == MigrationResult(
        workspace, database_name, CORE_CHAIN, False, SCHEMA_AHEAD
    )
    row = cluster.registry_row(workspace)
    assert row.state is WorkspaceState.UNAVAILABLE
    assert row.state_detail == "schema_ahead"
    with pytest.raises(StorageRefusal) as excinfo:
        active_workspace(workspace)
    assert excinfo.value.state == WORKSPACE_UNAVAILABLE

    # Repair cannot serve a database the code does not know.
    with pytest.raises(StorageRefusal) as excinfo:
        repair(workspace)
    assert excinfo.value.state == SCHEMA_AHEAD
    assert cluster.registry_row(workspace).state is WorkspaceState.UNAVAILABLE

    # Once the recorded revision is the head the code carries — the one matching what
    # the database physically holds — repair completes with nothing left to upgrade.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE core.alembic_version_core SET version_num = :v"),
            {"v": "0002_durable_work"},
        )
    repair(workspace)
    assert cluster.registry_row(workspace).state is WorkspaceState.ACTIVE


def test_two_concurrent_orchestrators_serialise_on_the_advisory_lock(
    cluster: ClusterSession, workspace: UUID
) -> None:
    database_name, engine = workspace_engine(cluster, workspace)
    key_high, key_low = ADVISORY_LOCK_KEY >> 32, ADVISORY_LOCK_KEY & 0xFFFFFFFF

    def waiters() -> int:
        with engine.connect() as connection:
            count = connection.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND database = (SELECT oid FROM pg_database WHERE datname = :db) "
                    "AND classid = CAST(:hi AS oid) AND objid = CAST(:lo AS oid) "
                    "AND NOT granted"
                ),
                {"db": database_name, "hi": key_high, "lo": key_low},
            ).scalar_one()
        return int(count)

    # The first orchestrator: hold the chain lock inside an open transaction.
    holder = engine.connect()
    holder.begin()
    advisory_lock(holder, ADVISORY_LOCK_KEY)
    assert waiters() == 0

    outcome: dict[str, object] = {}

    def second_orchestrator() -> None:
        try:
            outcome["result"] = migrate_workspace(cluster.backend, workspace)
        except BaseException as exc:  # surfaced through the assertions below
            outcome["error"] = exc

    thread = threading.Thread(target=second_orchestrator)
    thread.start()
    try:
        # The second observes the lock: it is queued, ungranted, on the same key.
        assert wait_until(lambda: waiters() == 1, timeout=10), (
            "the second orchestrator never queued on the advisory lock"
        )
        thread.join(0.5)
        assert thread.is_alive(), "the second orchestrator did not wait for the lock"
        assert "result" not in outcome
    finally:
        holder.rollback()
        holder.close()
    thread.join(30)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")
    result = outcome["result"]
    assert isinstance(result, MigrationResult) and result.ok
    assert waiters() == 0
    assert cluster.registry_row(workspace).state is WorkspaceState.ACTIVE


# --- the harness's own contract -------------------------------------------------------


def test_suite_fails_fast_when_the_cluster_is_unreachable() -> None:
    """A child pytest pointed at a closed port exits naming the remedy; nothing is
    skipped and nothing passes."""
    unreachable = (
        "postgresql://rheo:rheo_dev_only@127.0.0.1:1/postgres?connect_timeout=2"
    )
    completed = run_pytest_in_subprocess(
        "tests/test_contracts.py",
        extra_env={"RHEO_TEST_CLUSTER_DSN": unreachable},
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, output
    assert REMEDY in output, output
    assert "unreachable" in output, output
    assert " passed" not in output, output
    assert "skipped" not in output, output


# --- the lock is taken before any DDL -------------------------------------------------


def test_run_chain_takes_the_lock_before_creating_the_schema(
    cluster: ClusterSession,
) -> None:
    """``CREATE SCHEMA IF NOT EXISTS`` is not race-safe; it must run under the lock.

    A bare database (no registry row, no ``core`` schema) has one migrator queued on
    the advisory lock. While queued its transaction must have written nothing, so no
    transaction id is assigned yet; in the racy order ``CREATE SCHEMA`` would already
    have assigned one.
    """
    name = cluster.record(f"ws_{uuid7().hex}")
    assert cluster.backend.ensure_database(
        name, template=cluster.backend.template_database
    )
    engine = cluster.backend.pools.engine_for(name)
    key_high, key_low = ADVISORY_LOCK_KEY >> 32, ADVISORY_LOCK_KEY & 0xFFFFFFFF

    def waiter_pid() -> int | None:
        with engine.connect() as connection:
            found = connection.execute(
                text(
                    "SELECT pid FROM pg_locks WHERE locktype = 'advisory' "
                    "AND database = (SELECT oid FROM pg_database WHERE datname = :db) "
                    "AND classid = CAST(:hi AS oid) AND objid = CAST(:lo AS oid) "
                    "AND NOT granted"
                ),
                {"db": name, "hi": key_high, "lo": key_low},
            ).scalar()
        return None if found is None else int(found)

    def has_written(pid: int) -> bool:
        # A transaction that has done no write holds only a virtual xid; the first
        # catalog insert (``CREATE SCHEMA``) assigns a real one.
        with engine.connect() as connection:
            assigned = connection.execute(
                text(
                    "SELECT backend_xid IS NOT NULL FROM pg_stat_activity "
                    "WHERE pid = :pid"
                ),
                {"pid": pid},
            ).scalar_one()
        return bool(assigned)

    holder = engine.connect()
    holder.begin()
    advisory_lock(holder, ADVISORY_LOCK_KEY)
    outcome: dict[str, object] = {}

    def second_migrator() -> None:
        try:
            with engine.begin() as connection:
                run_chain(connection, CORE_CHAIN, expected_database=name)
            outcome["ok"] = True
        except BaseException as exc:  # surfaced through the assertions below
            outcome["error"] = exc

    thread = threading.Thread(target=second_migrator)
    thread.start()
    try:
        assert wait_until(lambda: waiter_pid() is not None, timeout=10)
        pid = waiter_pid()
        assert pid is not None
        assert not has_written(pid), "CREATE SCHEMA ran before the lock"
    finally:
        holder.rollback()
        holder.close()
    thread.join(30)
    assert not thread.is_alive()
    assert outcome.get("ok") is True, outcome.get("error")
    assert tables_in(engine, "core") == CORE_TABLES | {"alembic_version_core"}


def test_workspace_state_constraint_agrees_with_the_enum(
    cluster: ClusterSession, workspace: UUID
) -> None:
    """The check constraint is built from ``WorkspaceState``: every member is
    accepted by the database and nothing outside the enum is."""
    assert WORKSPACE_STATES == tuple(state.value for state in WorkspaceState)
    engine = cluster.backend.control_engine
    row = control_tables.workspace
    for state in WorkspaceState:
        with engine.connect() as connection:
            connection.execute(
                update(row).where(row.c.id == workspace).values(state=state.value)
            )
            connection.rollback()  # probed, never persisted
    with engine.connect() as connection:
        with pytest.raises(IntegrityError) as excinfo:
            connection.execute(
                update(row).where(row.c.id == workspace).values(state="archived")
            )
        assert isinstance(excinfo.value.orig, psycopg.errors.CheckViolation)
        connection.rollback()
    assert cluster.registry_row(workspace).state is WorkspaceState.ACTIVE
