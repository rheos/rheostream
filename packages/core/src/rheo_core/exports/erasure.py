"""Held artifact cleanup after committed erasure, with a durable retry job."""

from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from rheo_contracts import WorkspaceContext
from sqlalchemy import Connection, select
from sqlalchemy.exc import SQLAlchemyError

from rheo_core.exports.tables import export_record
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.work.cancellation import CancellationToken

SWEEP_JOB_KIND = "core.exports.sweep"


class SweepPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: UUID


def _sweep(connection: Connection, workspace_id: UUID) -> None:
    root = workspace_dir_for(workspace_id, Purpose.EXPORTS)
    for row in connection.execute(
        select(export_record.c.id, export_record.c.artifact_path).where(
            export_record.c.kind == "export",
            export_record.c.state == "removed_by_deletion",
        )
    ).mappings():
        expected = root / str(row["id"]) / f"{row['id']}.tar.zst"
        if row["artifact_path"] is None:
            continue
        if Path(
            row["artifact_path"]
        ) != expected or not expected.parent.resolve().is_relative_to(root.resolve()):
            raise RuntimeError("export_erasure_path_invalid")
        expected.unlink(missing_ok=True)


def sweep_after_commit(ctx: WorkspaceContext) -> None:
    """Best effort immediate cleanup; the already committed job retries failures."""
    try:
        with open_unit_of_work(ctx) as uow:
            _sweep(uow.connection, ctx.workspace_id)
    except (OSError, RuntimeError, SQLAlchemyError):
        return


def run_sweep_job(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    assert isinstance(payload, SweepPayload)
    # Validate the job's workspace against this actual worker transaction.
    from rheo_core.boundary.context import Refusal
    from rheo_core.boundary.factories import context_for_event_consumer
    from rheo_core.refs import uuid7

    ctx = context_for_event_consumer(payload.workspace_id, uow, request_id=uuid7())
    if isinstance(ctx, Refusal):
        raise RuntimeError("export_erasure_workspace_invalid")
    token.checkpoint()
    _sweep(uow.connection, payload.workspace_id)
    token.checkpoint()
