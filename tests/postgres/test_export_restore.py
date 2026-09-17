"""Criterion 21: complete export bytes, digest comparison, and safe restore."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
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
from rheo_contracts import Role, WorkspaceContext
from rheo_core.approvals.records import mark_approved
from rheo_core.approvals.tables import approval
from rheo_core.boundary import context_for_harness
from rheo_core.events.consumers import ConsumerRegistry
from rheo_core.exports import (
    EXPORT_JOB_KIND,
    RESTORE_JOB_KIND,
    WORKSPACE_DIGEST,
    WORKSPACE_EXPORT,
    WORKSPACE_RESTORE,
    ArtifactRefused,
    ExportJobPayload,
    RestoreJobPayload,
    read_archive,
    run_export_job,
    run_restore_job,
    write_archive,
)
from rheo_core.exports import artifact as artifact_module
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.storage import control_tables, core_tables, work_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import insert_account
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.provisioning import core_version
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from sqlalchemy import delete, insert, select

pytestmark = pytest.mark.postgres

OWNER = "export-restore-test"
SECRET_REFERENCE = "secret://env/RHEO_EXPORT_TEST_SECRET"
SECRET_VALUE = "resolved-secret-must-not-leak"


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


def _visit(cluster: ClusterSession, workspace_id: UUID) -> None:
    visit_workspace(
        DueWorkspace(workspace_id=workspace_id, observed_due_at=None),
        kinds=_kinds(),
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        owner=OWNER,
        clock=lambda: datetime.now(UTC) + timedelta(seconds=1),
        jitter=None,
    )


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
        uow.connection.execute(
            insert(core_tables.module_schema_version).values(
                module_id="harness",
                schema_version="0001_harness",
                applied_at=datetime.now(UTC),
                core_version_at_apply=core_version(),
            )
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
