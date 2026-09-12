"""B1's storage half, with direct repository calls (C4 re-proves it through the
registry): two provisions are two rows and two ``ws_`` + 32-hex databases in
``pg_database``; a ``harness.note`` row written in A is absent from B; a
workspace-setting write in A leaves B's row count and resolved value unchanged.

Also the ``UnitOfWork`` contract driven from an engine: one transaction (uncommitted
work is discarded), the test-profile ``current_database()`` assertion against a
mismatched engine, the closed-after-commit surface, and the absence of any outbox or
audit attachment point. And the pool: engines keyed by ``database_name``, eviction
disposing the evicted engine.
"""

import re
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.records import ensure_note_table, get_note, list_notes, write_note
from harness.settings_keys import HARNESS_EXPLICIT
from rheo_core.settings import ValueType, resolve
from rheo_core.settings.storage_source import PostgresOverrideSource
from rheo_core.storage.backend import (
    DATABASE_MISMATCH,
    UNIT_OF_WORK_CLOSED,
    StorageRefusal,
    UnitOfWork,
)
from rheo_core.storage.control_plane import WorkspaceRow
from rheo_core.storage.pools import EnginePool
from rheo_core.storage.repositories import upsert_workspace_setting, workspace_settings
from sqlalchemy import Engine, func, select, text

pytestmark = pytest.mark.postgres

DATABASE_NAME = re.compile(r"ws_[0-9a-f]{32}")


def engine_for(cluster: ClusterSession, row: WorkspaceRow) -> Engine:
    return cluster.backend.pools.engine_for(row.database_name)


def setting_row_count(cluster: ClusterSession, row: WorkspaceRow) -> int:
    from rheo_core.storage.core_tables import workspace_setting

    with UnitOfWork(engine_for(cluster, row), row.database_name) as uow:
        count = uow.connection.execute(
            select(func.count()).select_from(workspace_setting)
        ).scalar_one()
    return int(count)


@pytest.fixture
def two_workspaces(
    cluster: ClusterSession, make_workspace: MakeWorkspace
) -> tuple[WorkspaceRow, WorkspaceRow]:
    return cluster.registry_row(make_workspace()), cluster.registry_row(
        make_workspace()
    )


# --- B1: databases --------------------------------------------------------------------


def test_two_provisions_yield_two_rows_and_two_databases(
    cluster: ClusterSession, two_workspaces: tuple[WorkspaceRow, WorkspaceRow]
) -> None:
    a, b = two_workspaces
    assert a.id != b.id
    assert a.database_name != b.database_name
    for row in (a, b):
        assert DATABASE_NAME.fullmatch(row.database_name)
        assert row.database_name == f"ws_{row.id.hex}"
    with cluster.backend.maintenance_connection() as connection:
        present = connection.execute(
            text("SELECT datname FROM pg_database WHERE datname IN (:a, :b)"),
            {"a": a.database_name, "b": b.database_name},
        ).scalars()
        assert {str(name) for name in present} == {a.database_name, b.database_name}


# --- B1: the harness half -------------------------------------------------------------


def test_a_note_written_in_a_is_absent_from_b(
    cluster: ClusterSession, two_workspaces: tuple[WorkspaceRow, WorkspaceRow]
) -> None:
    a, b = two_workspaces
    with UnitOfWork(engine_for(cluster, a), a.database_name) as uow:
        ensure_note_table(uow.connection)
        note = write_note(uow.connection, body="only in A")
        uow.commit()
    with UnitOfWork(engine_for(cluster, b), b.database_name) as uow:
        ensure_note_table(uow.connection)
        assert list_notes(uow.connection) == ()
        assert get_note(uow.connection, note.id) is None
        uow.commit()
    with UnitOfWork(engine_for(cluster, a), a.database_name) as uow:
        assert get_note(uow.connection, note.id) == note
        assert [row.body for row in list_notes(uow.connection)] == ["only in A"]


# --- B1: the settings half ------------------------------------------------------------


def test_a_workspace_setting_write_in_a_leaves_b_unchanged(
    cluster: ClusterSession, two_workspaces: tuple[WorkspaceRow, WorkspaceRow]
) -> None:
    a, b = two_workspaces
    source = PostgresOverrideSource()
    count_before = setting_row_count(cluster, b)
    value_before = resolve(workspace_id=b.id, source=source)[HARNESS_EXPLICIT]
    assert (count_before, value_before) == (1, "harness-package-default")

    with UnitOfWork(engine_for(cluster, a), a.database_name) as uow:
        upsert_workspace_setting(
            uow.connection,
            key=HARNESS_EXPLICIT,
            value="set-in-a",
            value_type=ValueType.STR,
            updated_by=None,
        )
        uow.commit()

    assert resolve(workspace_id=a.id, source=source)[HARNESS_EXPLICIT] == "set-in-a"
    assert setting_row_count(cluster, b) == count_before
    assert resolve(workspace_id=b.id, source=source)[HARNESS_EXPLICIT] == value_before
    with UnitOfWork(engine_for(cluster, b), b.database_name) as uow:
        assert workspace_settings(uow.connection) == {
            HARNESS_EXPLICIT: "harness-package-default"
        }


# --- the unit of work -----------------------------------------------------------------


def test_unit_of_work_refuses_an_engine_for_another_database(
    cluster: ClusterSession, two_workspaces: tuple[WorkspaceRow, WorkspaceRow]
) -> None:
    a, b = two_workspaces
    with pytest.raises(StorageRefusal) as excinfo:
        with UnitOfWork(engine_for(cluster, a), b.database_name):
            pass
    assert excinfo.value.state == DATABASE_MISMATCH
    assert a.database_name in excinfo.value.detail
    assert b.database_name in excinfo.value.detail
    # The right pairing opens, on the right database.
    with UnitOfWork(engine_for(cluster, a), a.database_name) as uow:
        actual = uow.connection.execute(text("SELECT current_database()")).scalar_one()
        assert actual == a.database_name


def test_unit_of_work_is_one_transaction_with_commit_and_rollback_only(
    cluster: ClusterSession, workspace: UUID
) -> None:
    row = cluster.registry_row(workspace)
    engine = engine_for(cluster, row)
    with UnitOfWork(engine, row.database_name) as uow:
        ensure_note_table(uow.connection)
        uow.commit()

    # Leaving without commit discards the work.
    with UnitOfWork(engine, row.database_name) as uow:
        write_note(uow.connection, body="discarded")
    with UnitOfWork(engine, row.database_name) as uow:
        assert list_notes(uow.connection) == ()

    # An explicit rollback discards it and closes the unit of work.
    with UnitOfWork(engine, row.database_name) as uow:
        write_note(uow.connection, body="rolled back")
        uow.rollback()
        with pytest.raises(StorageRefusal) as excinfo:
            _ = uow.connection
        assert excinfo.value.state == UNIT_OF_WORK_CLOSED
    with UnitOfWork(engine, row.database_name) as uow:
        assert list_notes(uow.connection) == ()

    # Commit persists it; a second commit is refused (one transaction).
    with UnitOfWork(engine, row.database_name) as uow:
        write_note(uow.connection, body="kept")
        uow.commit()
        with pytest.raises(StorageRefusal):
            uow.commit()
    with UnitOfWork(engine, row.database_name) as uow:
        assert [note.body for note in list_notes(uow.connection)] == ["kept"]

    # Entering twice is refused; the constructor needs the expected name.
    with pytest.raises(StorageRefusal):
        with UnitOfWork(engine, row.database_name) as uow:
            uow.__enter__()
    with pytest.raises(TypeError):
        UnitOfWork(engine, "")


def test_unit_of_work_has_no_outbox_or_audit_attachment_point() -> None:
    public = {name for name in dir(UnitOfWork) if not name.startswith("_")}
    # ``verify_database`` is a class-level bool flag, not an attachment point.
    assert public == {"commit", "connection", "rollback", "verify_database"}
    assert isinstance(UnitOfWork.verify_database, bool)
    slots = set(UnitOfWork.__slots__)
    assert slots == {"_connection", "_engine", "_expected_database", "_transaction"}
    assert not any("outbox" in slot or "audit" in slot for slot in slots)


# --- the pool -------------------------------------------------------------------------


def test_pool_keys_engines_by_database_name_and_evicts_the_least_recent(
    cluster: ClusterSession, two_workspaces: tuple[WorkspaceRow, WorkspaceRow]
) -> None:
    a, b = two_workspaces
    small = EnginePool(
        cluster.backend.pools.cluster_url,
        cache_size=1,
        pool_size=1,
        idle_close_seconds=300.0,
    )
    try:
        engine_a = small.engine_for(a.database_name)
        assert engine_a.url.database == a.database_name
        assert small.engine_for(a.database_name) is engine_a
        pool_a = engine_a.pool
        with engine_a.connect() as connection:
            assert (
                connection.execute(text("SELECT current_database()")).scalar_one()
                == a.database_name
            )

        engine_b = small.engine_for(b.database_name)
        assert engine_b is not engine_a
        assert engine_b.url.database == b.database_name
        # A was evicted and disposed: its pool object was replaced.
        assert small.cached() == (b.database_name,)
        assert engine_a.pool is not pool_a
        # A comes back as a fresh engine, still keyed by its own name.
        engine_a2 = small.engine_for(a.database_name)
        assert engine_a2 is not engine_a
        assert engine_a2.url.database == a.database_name
        assert small.cached() == (a.database_name,)
    finally:
        small.dispose_all()
    with pytest.raises(ValueError, match="identifiers"):
        small.engine_for("ws_not-valid")


# --- the assertion flag is resolved once ----------------------------------------------


def test_database_assertion_is_resolved_once_not_per_unit_of_work(
    cluster: ClusterSession,
    two_workspaces: tuple[WorkspaceRow, WorkspaceRow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = two_workspaces
    assert cluster.backend.verify_database is True
    assert UnitOfWork.verify_database is True
    # A profile change after startup changes nothing: there is no per-transaction
    # profile read, so the mismatch is still refused.
    monkeypatch.setenv("RHEO_PROFILE", "production")
    with pytest.raises(StorageRefusal) as excinfo:
        with UnitOfWork(engine_for(cluster, a), b.database_name):
            pass
    assert excinfo.value.state == DATABASE_MISMATCH
    # With the flag off (as a production backend sets it) the probe is skipped.
    monkeypatch.setattr(UnitOfWork, "verify_database", False)
    with UnitOfWork(engine_for(cluster, a), b.database_name) as uow:
        actual = uow.connection.execute(text("SELECT current_database()")).scalar_one()
        assert actual == a.database_name
