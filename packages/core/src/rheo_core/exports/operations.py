"""Registered export, digest and restore operation models and handlers."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from rheo_contracts import ActorKind, WorkspaceContext
from sqlalchemy import insert, select, update

from rheo_core.exports import tables
from rheo_core.exports.artifact import (
    ArtifactIdentity,
    ArtifactRefused,
    artifact_identity,
    create_artifact,
    digest_categories,
    restore_artifact,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.settings import resolve
from rheo_core.storage import control_tables
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.control_plane import get_workspace
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.postgres import get_backend
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.jobs import enqueue_job

WORKSPACE_EXPORT: Final = "core.workspace.export"
WORKSPACE_DIGEST: Final = "core.workspace.digest"
WORKSPACE_RESTORE: Final = "core.workspace.restore"
EXPORT_JOB_KIND: Final = "core.workspace.export"
RESTORE_JOB_KIND: Final = "core.workspace.restore"
_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")


class WorkspaceExportInput(BaseModel):
    model_config = _IGNORE_EXTRA


class WorkspaceRestoreInput(BaseModel):
    model_config = _IGNORE_EXTRA

    artifact_path: str


class WorkspaceDigestInput(BaseModel):
    model_config = _IGNORE_EXTRA


class ScheduledArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    export_id: UUID
    operation_id: UUID | None


class DigestEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    count: int
    digest: str


class WorkspaceDigest(BaseModel):
    model_config = ConfigDict(frozen=True)

    categories: dict[str, DigestEntry]


class ExportJobPayload(BaseModel):
    workspace_id: UUID
    slug: str
    owner_account_id: UUID
    export_id: UUID
    artifact_path: str
    operation_id: UUID | None


class RestoreJobPayload(BaseModel):
    artifact_path: str
    restore_id: UUID
    expected_workspace_id: UUID
    expected_owner_account_id: UUID
    source_digest: str


def _operation_id(uow: UnitOfWork) -> UUID | None:
    return uow.operation_id if isinstance(uow, HandlerUnitOfWork) else None


def _owner_account_id(ctx: WorkspaceContext) -> UUID:
    if ctx.actor.id is not None:
        return ctx.actor.id
    with get_backend().control_engine.connect() as connection:
        value = connection.execute(
            select(control_tables.membership.c.account_id)
            .where(
                control_tables.membership.c.workspace_id == ctx.workspace_id,
                control_tables.membership.c.role == "owner",
            )
            .order_by(control_tables.membership.c.created_at)
            .limit(1)
        ).scalar_one_or_none()
    if value is None:
        raise OperationRefused(
            "owner_missing", f"workspace {ctx.workspace_id} has no owner membership"
        )
    return UUID(str(value))


def _insert_record(
    uow: UnitOfWork,
    *,
    record_id: UUID,
    kind: str,
    created_by_id: UUID | None,
) -> None:
    uow.connection.execute(
        insert(tables.export_record).values(
            id=record_id,
            kind=kind,
            artifact_path=None,
            source_digest=None,
            created_at=datetime.now(UTC),
            created_by_id=created_by_id,
            state="in_progress",
            removed_at=None,
            deletion_record_id=None,
            byte_length=None,
        )
    )


def export_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, _input: WorkspaceExportInput
) -> ScheduledArtifact:
    operation_id = _operation_id(uow)
    owner_account_id = _owner_account_id(ctx)
    with get_backend().control_engine.connect() as control:
        workspace = get_workspace(control, ctx.workspace_id)
    if workspace is None:
        raise OperationRefused(
            "workspace_missing", f"workspace {ctx.workspace_id} has no registry row"
        )
    export_id = uuid7()
    destination = (
        workspace_dir_for(ctx.workspace_id, Purpose.EXPORTS)
        / str(export_id)
        / f"{export_id}.tar.zst"
    )
    _insert_record(uow, record_id=export_id, kind="export", created_by_id=ctx.actor.id)
    enqueue_job(
        uow.connection,
        kind=EXPORT_JOB_KIND,
        payload={
            "workspace_id": str(ctx.workspace_id),
            "slug": workspace.slug,
            "owner_account_id": str(owner_account_id),
            "export_id": str(export_id),
            "artifact_path": str(destination),
            "operation_id": None if operation_id is None else str(operation_id),
        },
        now=datetime.now(UTC),
        max_attempts=resolve().get_int("work.max_attempts"),
        operation_id=operation_id,
    )
    return ScheduledArtifact(export_id=export_id, operation_id=operation_id)


def digest_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, _input: WorkspaceDigestInput
) -> WorkspaceDigest:
    del ctx
    return WorkspaceDigest(
        categories={
            name: DigestEntry.model_validate(value)
            for name, value in digest_categories(uow.connection).items()
        }
    )


def restore_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: WorkspaceRestoreInput
) -> ScheduledArtifact:
    operation_id = _operation_id(uow)
    restore_id = uuid7()
    path = Path(model_input.artifact_path).expanduser().resolve()
    if not path.is_file():
        raise OperationRefused("artifact_missing", f"artifact {path} does not exist")
    try:
        identity = artifact_identity(path)
    except ArtifactRefused as exc:
        raise OperationRefused("artifact_invalid", str(exc)) from None
    if (
        ctx.actor.kind is ActorKind.ACCOUNT
        and ctx.actor.id != identity.owner_account_id
    ):
        raise OperationRefused(
            "restore_not_permitted",
            "the artifact owner does not match the authenticated account",
        )
    _insert_record(
        uow, record_id=restore_id, kind="restore", created_by_id=ctx.actor.id
    )
    enqueue_job(
        uow.connection,
        kind=RESTORE_JOB_KIND,
        payload={
            "artifact_path": str(path),
            "restore_id": str(restore_id),
            "expected_workspace_id": str(identity.workspace_id),
            "expected_owner_account_id": str(identity.owner_account_id),
            "source_digest": identity.source_digest.hex(),
        },
        now=datetime.now(UTC),
        max_attempts=resolve().get_int("work.max_attempts"),
        operation_id=operation_id,
    )
    return ScheduledArtifact(export_id=restore_id, operation_id=operation_id)


def run_export_job(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    assert isinstance(payload, ExportJobPayload)
    token.checkpoint()
    create_artifact(
        uow.connection,
        workspace_id=payload.workspace_id,
        slug=payload.slug,
        owner_account_id=payload.owner_account_id,
        export_id=payload.export_id,
        destination=Path(payload.artifact_path),
        skip_operation_id=payload.operation_id,
        created_at=datetime.now(UTC),
    )
    token.checkpoint()


def run_restore_job(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    assert isinstance(payload, RestoreJobPayload)
    token.checkpoint()
    expected_identity = ArtifactIdentity(
        workspace_id=payload.expected_workspace_id,
        owner_account_id=payload.expected_owner_account_id,
        source_digest=bytes.fromhex(payload.source_digest),
    )
    restore_artifact(
        Path(payload.artifact_path),
        restore_id=payload.restore_id,
        expected_identity=expected_identity,
    )
    uow.connection.execute(
        update(tables.export_record)
        .where(tables.export_record.c.id == payload.restore_id)
        .values(state="complete", source_digest=expected_identity.source_digest)
    )
    token.checkpoint()
