"""Criterion 21: complete export bytes, digest comparison, and safe restore.

And A12's source snapshot, which every one of those bytes is now read through: the
read-only repeatable-read transaction ``collect_export_snapshot`` opens, the
concurrent commit it cannot see, the pool-capacity refusal that precedes it, the
cleanup when collection fails, and the export-record completion that belongs to the
worker's own lease-conditional finish rather than to the collector.
"""

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.registry import (
    FIXTURE_ACT,
    NOTE_WRITE,
    act_payload,
    enable_harness_module,
    recorded_acts,
    register_harness,
)
from pydantic import BaseModel
from rheo_contracts import Role, WorkspaceContext
from rheo_core.approvals.records import mark_approved
from rheo_core.approvals.tables import approval
from rheo_core.boundary import context_for_harness
from rheo_core.events.consumers import ConsumerRegistry
from rheo_core.exports import (
    EXPORT_JOB_KIND,
    EXPORT_RESOURCE_UNAVAILABLE,
    RESTORE_JOB_KIND,
    WORKSPACE_DIGEST,
    WORKSPACE_EXPORT,
    WORKSPACE_RESTORE,
    ArtifactRefused,
    ExportJobPayload,
    RestoreJobPayload,
    collect_export_snapshot,
    read_archive,
    run_export_job,
    run_restore_job,
    serialised_categories,
    write_archive,
)
from rheo_core.exports import artifact as artifact_module
from rheo_core.exports import operations as export_operations
from rheo_core.exports import tables as export_tables
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.refusals import OperationRefused
from rheo_core.storage import control_tables, core_tables, work_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import insert_account
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.pools import EnginePool
from rheo_core.storage.provisioning import core_version
from rheo_core.storage.repositories import insert_module_schema_version
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from sqlalchemy import Engine, delete, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import QueuePool

pytestmark = pytest.mark.postgres

OWNER = "export-restore-test"
SECRET_REFERENCE = "secret://env/RHEO_EXPORT_TEST_SECRET"
SECRET_VALUE = "resolved-secret-must-not-leak"
LATE_SETTING = "test.committed_after_snapshot_at"


class _CollectionFailed(Exception):
    """Raised by the cleanup tests, standing in for a cancellation or a checkout
    that could not be served — its identity is what proves the failure propagated
    rather than being swallowed by the collector's own teardown."""


@pytest.fixture(autouse=True)
def registrations(harness_keys: None, monkeypatch: pytest.MonkeyPatch) -> None:
    register_core_operations()
    register_harness()
    monkeypatch.setenv("RHEO_EXPORT_TEST_SECRET", SECRET_VALUE)


def _context(
    cluster: ClusterSession,
    workspace_id: UUID,
    account_id: UUID,
    *,
    harness: bool = False,
) -> WorkspaceContext:
    row = cluster.registry_row(workspace_id)
    if harness:
        with UnitOfWork(
            cluster.backend.pools.engine_for(row.database_name), row.database_name
        ) as uow:
            enable_harness_module(uow.connection)
            uow.commit()
    ctx = context_for_harness(workspace_id, account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _kinds() -> JobKindRegistry:
    kinds = JobKindRegistry()
    kinds.register(EXPORT_JOB_KIND, ExportJobPayload, run_export_job)
    kinds.register(RESTORE_JOB_KIND, RestoreJobPayload, run_restore_job)
    return kinds


def _visit(
    cluster: ClusterSession, workspace_id: UUID, *, kinds: JobKindRegistry | None = None
) -> None:
    visit_workspace(
        DueWorkspace(workspace_id=workspace_id, observed_due_at=None),
        kinds=_kinds() if kinds is None else kinds,
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        owner=OWNER,
        clock=lambda: datetime.now(UTC) + timedelta(seconds=1),
        jitter=None,
    )


def _source_engine(cluster: ClusterSession, workspace_id: UUID) -> tuple[Engine, str]:
    row = cluster.registry_row(workspace_id)
    return cluster.backend.pools.engine_for(row.database_name), row.database_name


def _export(cluster: ClusterSession, ctx: WorkspaceContext) -> Path:
    outcome = dispatch(ctx, WORKSPACE_EXPORT, {})
    assert outcome.state == "pending", outcome
    assert outcome.operation_id is not None
    result: Any = outcome.result
    export_id = result.export_id
    _visit(cluster, ctx.workspace_id)
    path = (
        workspace_dir_for(ctx.workspace_id, Purpose.EXPORTS)
        / str(export_id)
        / f"{export_id}.tar.zst"
    )
    assert path.is_file()
    return path


def _digest(ctx: WorkspaceContext) -> dict[str, Any]:
    outcome = dispatch(ctx, WORKSPACE_DIGEST, {})
    assert outcome.ok, outcome
    result: Any = outcome.result
    return result.model_dump(mode="json")["categories"]


def _remove_workspace(cluster: ClusterSession, workspace_id: UUID) -> None:
    row = cluster.registry_row(workspace_id)
    cluster.backend.pools.dispose_all()
    with cluster.backend.control_engine.begin() as control:
        control.execute(
            delete(control_tables.membership).where(
                control_tables.membership.c.workspace_id == workspace_id
            )
        )
        control.execute(
            delete(control_tables.workspace).where(
                control_tables.workspace.c.id == workspace_id
            )
        )
    with cluster.backend.maintenance_connection() as maintenance:
        quoted = maintenance.dialect.identifier_preparer.quote(row.database_name)
        maintenance.exec_driver_sql(f"DROP DATABASE {quoted} WITH (FORCE)")


def _restore_from_host(
    cluster: ClusterSession, host: WorkspaceContext, artifact: Path
) -> None:
    outcome = dispatch(host, WORKSPACE_RESTORE, {"artifact_path": str(artifact)})
    assert outcome.state == "pending", outcome
    _visit(cluster, host.workspace_id)


# --- A12: the one source snapshot ------------------------------------------------


def test_the_source_snapshot_is_read_only_and_repeatable_read(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """The two settings the whole contract rests on, read off the snapshot itself.

    Asserted on the connection rather than inferred from the options that were
    passed, because what matters is the state PostgreSQL actually began the
    transaction in — and the write attempt at the end is the same claim made the only
    way that cannot be satisfied by a setting that was recorded but not applied.
    """
    engine, database = _source_engine(cluster, workspace)
    with UnitOfWork(engine, database) as worker:
        with collect_export_snapshot(
            workspace, worker_connection=worker.connection
        ) as snapshot:
            assert snapshot.connection is not worker.connection
            assert snapshot.database_name == database
            assert snapshot.workspace_id == workspace
            assert snapshot.snapshot_at.tzinfo is not None
            assert snapshot.connection.execute(
                text(
                    "SELECT current_setting('transaction_isolation'), "
                    "current_setting('transaction_read_only')"
                )
            ).one() == ("repeatable read", "on")
            # The worker's own transaction is untouched: A12 forbids changing its
            # isolation or making it the source connection.
            assert (
                worker.connection.execute(
                    text("SELECT current_setting('transaction_isolation')")
                ).scalar_one()
                == "read committed"
            )
            with pytest.raises(DBAPIError, match="read-only transaction"):
                snapshot.connection.execute(
                    insert(core_tables.workspace_setting).values(
                        key="test.write_from_the_snapshot",
                        value="refused",
                        value_type="str",
                        updated_by=owner_account_id,
                        updated_at=datetime.now(UTC),
                    )
                )


def test_a_commit_after_snapshot_at_is_invisible_to_the_source_snapshot(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """The MVCC guarantee itself, not the isolation level that is supposed to give it.

    A real second transaction commits between two reads of the same category on the
    open snapshot. Under repeatable read the second read is byte-identical to the
    first; under read committed it is not, which is what makes this test — rather
    than an assertion about ``current_setting`` — the one that bites when the
    isolation option is lost.
    """
    engine, database = _source_engine(cluster, workspace)
    with UnitOfWork(engine, database) as worker:
        with collect_export_snapshot(
            workspace, worker_connection=worker.connection
        ) as snapshot:
            before = serialised_categories(snapshot)["settings"]
            assert LATE_SETTING.encode() not in before

            with UnitOfWork(engine, database) as concurrent:
                concurrent.connection.execute(
                    insert(core_tables.workspace_setting).values(
                        key=LATE_SETTING,
                        value="committed after snapshot_at",
                        value_type="str",
                        updated_by=owner_account_id,
                        updated_at=datetime.now(UTC),
                    )
                )
                concurrent.commit()

            after = serialised_categories(snapshot)["settings"]
            assert after == before
            assert LATE_SETTING.encode() not in after

    # The row is genuinely there; only the earlier snapshot could not see it.
    with UnitOfWork(engine, database) as worker:
        with collect_export_snapshot(
            workspace, worker_connection=worker.connection
        ) as later:
            assert LATE_SETTING.encode() in serialised_categories(later)["settings"]


def test_a_pool_below_three_connections_refuses_before_any_source_query(
    cluster: ClusterSession, workspace: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary is exactly three, and two refuses without opening anything.

    ``cached() == ()`` is the "before any source query" half: the refusal happens
    before the pool is ever asked for the workspace's engine, so there is no
    connection to check out and nothing to clean up.
    """
    engine, database = _source_engine(cluster, workspace)
    with UnitOfWork(engine, database) as worker:
        for pool_size in (2, 3):
            small = EnginePool(
                cluster.backend.pools.cluster_url,
                cache_size=2,
                pool_size=pool_size,
                idle_close_seconds=300.0,
                reserved_connections=0,
            )
            monkeypatch.setattr(cluster.backend, "pools", small)
            try:
                if pool_size < 3:
                    with pytest.raises(OperationRefused) as excinfo:
                        with collect_export_snapshot(
                            workspace, worker_connection=worker.connection
                        ):
                            pass
                    assert excinfo.value.state == EXPORT_RESOURCE_UNAVAILABLE
                    assert small.cached() == ()
                else:
                    with collect_export_snapshot(
                        workspace, worker_connection=worker.connection
                    ) as snapshot:
                        assert snapshot.database_name == database
                    assert small.cached() == (database,)
            finally:
                small.dispose_all()


def test_a_failed_collection_leaves_no_pinned_engine_or_open_transaction(
    cluster: ClusterSession, workspace: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both failure shapes A12 names: a cancellation mid-collection, and a checkout
    that never produced a transaction at all.

    ``checkedout() == 0`` is the transaction half — the connection went back to the
    pool rather than sitting in an abandoned transaction — and ``close_idle()``
    returning the engine is the pin half, since ``_expire_idle`` skips any engine
    still pinned or checked out.
    """
    worker_engine, database = _source_engine(cluster, workspace)
    small = EnginePool(
        cluster.backend.pools.cluster_url,
        cache_size=2,
        pool_size=5,
        idle_close_seconds=0.01,
        reserved_connections=0,
    )
    monkeypatch.setattr(cluster.backend, "pools", small)
    try:
        with UnitOfWork(worker_engine, database) as worker:
            with pytest.raises(_CollectionFailed):
                with collect_export_snapshot(
                    workspace, worker_connection=worker.connection
                ):
                    raise _CollectionFailed("cancelled between categories")
            source = small.engine_for(database)
            assert isinstance(source.pool, QueuePool)
            assert source.pool.checkedout() == 0

            def _refuse_checkout(engine: Engine, expected_database: str) -> NoReturn:
                raise _CollectionFailed("the source connection was not served")

            monkeypatch.setattr(
                artifact_module, "ExportSnapshotUnitOfWork", _refuse_checkout
            )
            with pytest.raises(_CollectionFailed):
                with collect_export_snapshot(
                    workspace, worker_connection=worker.connection
                ):
                    pass
            assert source.pool.checkedout() == 0

        time.sleep(0.05)
        assert small.close_idle() == 1
    finally:
        small.dispose_all()


def test_export_record_completion_is_the_workers_write_not_the_collectors(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Which connection publishes the export, observed at the moment it could differ.

    The spy reads ``core.export_record`` on the **worker's own** connection the
    instant ``create_artifact`` returns. That transaction can see its own uncommitted
    writes, so a completion written from inside collection would read ``complete``
    there and one written afterwards by ``run_export_job`` reads ``in_progress`` —
    the two placements this assertion tells apart. The tail then proves the write
    does land, through the finish that commits it with the job's own ``succeeded``.
    """
    ctx = _context(cluster, workspace, owner_account_id)
    outcome = dispatch(ctx, WORKSPACE_EXPORT, {})
    assert outcome.state == "pending", outcome
    result: Any = outcome.result
    export_id = result.export_id

    worker_connections: list[Any] = []
    seen: dict[str, object] = {}
    real_create_artifact = export_operations.create_artifact

    def spying_handler(uow: Any, payload: BaseModel, token: CancellationToken) -> None:
        worker_connections.append(uow.connection)
        run_export_job(uow, payload, token)

    def spying_create_artifact(*args: Any, **kwargs: Any) -> int:
        byte_length = int(real_create_artifact(*args, **kwargs))
        seen["state_when_collection_returned"] = (
            worker_connections[0]
            .execute(
                select(export_tables.export_record.c.state).where(
                    export_tables.export_record.c.id == export_id
                )
            )
            .scalar_one()
        )
        return byte_length

    monkeypatch.setattr(export_operations, "create_artifact", spying_create_artifact)
    kinds = JobKindRegistry()
    kinds.register(EXPORT_JOB_KIND, ExportJobPayload, spying_handler)
    _visit(cluster, workspace, kinds=kinds)

    assert seen["state_when_collection_returned"] == "in_progress"

    engine, database = _source_engine(cluster, workspace)
    with UnitOfWork(engine, database) as uow:
        record = uow.connection.execute(
            select(
                export_tables.export_record.c.state,
                export_tables.export_record.c.artifact_path,
                export_tables.export_record.c.byte_length,
            ).where(export_tables.export_record.c.id == export_id)
        ).one()
    assert record.state == "complete"
    assert record.artifact_path is not None
    assert record.byte_length == Path(record.artifact_path).stat().st_size


# --- criterion 21 -----------------------------------------------------------------


def test_export_artifact_is_complete_and_secret_value_free(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    tmp_path: Path,
) -> None:
    ctx = _context(cluster, workspace, owner_account_id)
    row = cluster.registry_row(workspace)
    with UnitOfWork(
        cluster.backend.pools.engine_for(row.database_name), row.database_name
    ) as uow:
        uow.connection.execute(
            insert(core_tables.workspace_setting).values(
                key="test.secret_reference",
                value=SECRET_REFERENCE,
                value_type="str",
                updated_by=owner_account_id,
                updated_at=datetime.now(UTC),
            )
        )
        uow.commit()

    artifact = _export(cluster, ctx)
    entries = read_archive(artifact)

    assert set(entries) == {
        "manifest.json",
        "settings.jsonl",
        "core/approvals.jsonl",
        "core/operations.jsonl",
        "core/audit.jsonl",
        "core/deletions.jsonl",
    }
    assert entries["core/deletions.jsonl"] == b""
    unpacked = b"".join(entries.values())
    assert SECRET_REFERENCE.encode() in unpacked
    assert SECRET_VALUE.encode() not in unpacked

    invalid_entries = dict(entries)
    manifest = json.loads(invalid_entries["manifest.json"])
    manifest["modules"] = [
        {
            "module_id": "harness",
            "schema_version": None,
            "export_format_version": 1,
        }
    ]
    invalid_entries["manifest.json"] = json.dumps(manifest).encode()
    invalid = tmp_path / "invalid-module.tar.zst"
    write_archive(invalid_entries, invalid)
    with pytest.raises(ArtifactRefused, match="package_version"):
        artifact_module.artifact_identity(invalid)


def test_restore_matches_source_by_workspace_digest(
    cluster: ClusterSession,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
) -> None:
    source_id = make_workspace(slug="digest-source")
    host_id = make_workspace(slug="restore-host")
    source = _context(cluster, source_id, owner_account_id, harness=True)
    host = _context(cluster, host_id, owner_account_id)
    source_row = cluster.registry_row(source_id)
    with UnitOfWork(
        cluster.backend.pools.engine_for(source_row.database_name),
        source_row.database_name,
    ) as uow:
        # Through the repository writer, never a statement of this file's own:
        # ``repositories.py`` is the only code that inserts into this table, and
        # ``tests/test_module_state_writers.py`` scans ``tests/`` for exactly this.
        insert_module_schema_version(
            uow.connection,
            module_id="harness",
            schema_version="0001_harness",
            applied_at=datetime.now(UTC),
            core_version_at_apply=core_version(),
        )
        uow.commit()
    artifact = _export(cluster, source)
    source_digest = _digest(source)
    assert set(source_digest) == {
        "composition",
        "settings",
        "approvals",
        "operations",
        "audit",
        "deletions",
    }

    _remove_workspace(cluster, source_id)
    _restore_from_host(cluster, host, artifact)
    restored = _context(cluster, source_id, owner_account_id)

    assert _digest(restored) == source_digest


def test_archive_reader_enforces_compressed_and_member_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "bounded.tar.zst"
    write_archive({"one": b"payload"}, artifact)
    monkeypatch.setattr(
        artifact_module, "MAX_COMPRESSED_BYTES", artifact.stat().st_size - 1
    )
    with pytest.raises(ArtifactRefused, match="compressed bytes"):
        read_archive(artifact)
    monkeypatch.setattr(
        artifact_module, "MAX_COMPRESSED_BYTES", artifact.stat().st_size
    )
    monkeypatch.setattr(artifact_module, "MAX_MEMBER_BYTES", 1)
    with pytest.raises(ArtifactRefused, match="member 'one' exceeds"):
        read_archive(artifact)


def test_account_cannot_queue_restore_for_another_artifact_owner(
    cluster: ClusterSession,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
) -> None:
    source_id = make_workspace(slug="owned-export")
    source = _context(cluster, source_id, owner_account_id)
    artifact = _export(cluster, source)
    with cluster.backend.control_engine.begin() as control:
        other_account = insert_account(control, display_name="other restore owner").id
    host_id = make_workspace(slug="other-owner-host", owner=other_account)
    other = _context(cluster, host_id, other_account)

    outcome = dispatch(other, WORKSPACE_RESTORE, {"artifact_path": str(artifact)})

    assert outcome.state == "restore_not_permitted"


def test_approved_action_restore_requires_fresh_approval_and_executes_nothing(
    cluster: ClusterSession,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
    tmp_path: Path,
) -> None:
    source_id = make_workspace(slug="approval-source")
    host_id = make_workspace(slug="approval-host")
    source = _context(cluster, source_id, owner_account_id, harness=True)
    host = _context(cluster, host_id, owner_account_id)
    note = dispatch(source, NOTE_WRITE, {"body": "restore approval subject"})
    assert note.ok and note.result is not None
    note_result: Any = note.result
    note_ref = str(note_result.ref)
    held = dispatch(source, FIXTURE_ACT, act_payload(note_ref))
    assert held.state == "approval_required" and held.approval_id is not None
    source_row = cluster.registry_row(source_id)
    source_engine = cluster.backend.pools.engine_for(source_row.database_name)
    with UnitOfWork(source_engine, source_row.database_name) as uow:
        # The public approve operation executes synchronously in C7. The repository
        # transition stages the only representable approved-but-not-executed row so
        # export can observe the state AC 17 specifically requires.
        assert mark_approved(
            uow.connection,
            approval_id=held.approval_id,
            approved_by_kind=source.actor.kind.value,
            approved_by_id=source.actor.id,
            approved_entry=source.entry.value,
            now=datetime.now(UTC),
        )
        uow.commit()
    with UnitOfWork(source_engine, source_row.database_name) as uow:
        assert recorded_acts(uow.connection) == ()

    artifact = _export(cluster, source)
    entries = read_archive(artifact)
    approval_rows = [
        json.loads(line) for line in entries["core/approvals.jsonl"].splitlines()
    ]
    assert approval_rows and approval_rows[0]["state"] == "requires_reapproval"
    # Tamper the artifact to prove restore distrusts its state column independently.
    approval_rows[0]["state"] = "approved"
    entries["core/approvals.jsonl"] = b"".join(
        (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
        for row in approval_rows
    )
    tampered = tmp_path / "tampered.tar.zst"
    write_archive(entries, tampered)

    _remove_workspace(cluster, source_id)
    _restore_from_host(cluster, host, tampered)
    restored_row = cluster.registry_row(source_id)
    restored_engine = cluster.backend.pools.engine_for(restored_row.database_name)
    with UnitOfWork(restored_engine, restored_row.database_name) as uow:
        state = uow.connection.execute(
            select(approval.c.state).where(approval.c.id == held.approval_id)
        ).scalar_one()
        assert state == "requires_reapproval"
        operation_state = uow.connection.execute(
            select(work_tables.operation.c.state).where(
                work_tables.operation.c.id == held.operation_id
            )
        ).scalar_one()
        assert operation_state == "approval_required"
        assert recorded_acts(uow.connection) == ()
