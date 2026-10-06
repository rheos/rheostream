"""Reference-bound core consequences of approved user erasure."""

from datetime import datetime
from uuid import UUID

from rheo_contracts import RecordRef, WorkspaceContext
from sqlalchemy import delete, exists, or_, select, update

from rheo_core.approvals.tables import approval, approval_payload
from rheo_core.exports.erasure import SWEEP_JOB_KIND
from rheo_core.exports.tables import export_record, export_record_ref
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.runtime_tables import runtime_request_context, runtime_transcript
from rheo_core.storage.work_tables import job, operation
from rheo_core.work.jobs import enqueue_job


def erase_dependents(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    ref: RecordRef,
    *,
    deletion_id: UUID,
    approval_id: UUID,
    now: datetime,
) -> tuple[int, int, int]:
    reference = ref.format()
    cancelled = list(
        uow.connection.execute(
            update(job)
            .where(
                job.c.depends_on_ref == reference, job.c.state.in_(("queued", "leased"))
            )
            .values(
                state="cancelled",
                cancel_requested=True,
                finished_at=now,
                lease_owner=None,
                lease_until=None,
                input={},
                last_error="source_deleted",
            )
            .returning(job.c.id, job.c.operation_id)
        ).mappings()
    )
    held = list(
        uow.connection.execute(
            select(approval.c.id, approval.c.operation_id)
            .where(
                or_(
                    approval.c.subject_ref == reference,
                    approval.c.destination_ref == reference,
                ),
                approval.c.id != approval_id,
                approval.c.state.in_(("pending", "approved", "requires_reapproval")),
            )
            .with_for_update()
        ).mappings()
    )
    held_ids = [r["id"] for r in held]
    if held_ids:
        uow.connection.execute(
            delete(approval_payload).where(approval_payload.c.approval_id.in_(held_ids))
        )
        uow.connection.execute(
            update(approval)
            .where(approval.c.id.in_(held_ids))
            .values(
                state="invalidated",
                invalidated_reason="source_deleted",
                payload_ref=None,
            )
        )
    operation_ids = [
        r["operation_id"] for r in [*cancelled, *held] if r["operation_id"] is not None
    ]
    if operation_ids:
        uow.connection.execute(
            update(operation)
            .where(
                operation.c.id.in_(operation_ids),
                operation.c.state.in_(("pending", "running", "approval_required")),
            )
            .values(
                state="cancelled",
                terminal_at=now,
                error_code="source_deleted",
                error_text="Source record erased",
            )
        )
    requests = select(runtime_request_context.c.request_id).where(
        runtime_request_context.c.ref == reference
    )
    uow.connection.execute(
        delete(runtime_transcript).where(runtime_transcript.c.request_id.in_(requests))
    )
    # Old artifacts have no reference index. Treat those as potentially containing
    # the erased record; all newly produced artifacts get an index at publication.
    indexed = exists(
        select(export_record_ref.c.export_id).where(
            export_record_ref.c.export_id == export_record.c.id
        )
    )
    affected = select(export_record_ref.c.export_id).where(
        export_record_ref.c.record_ref == reference
    )
    removed: list[UUID] = list(
        uow.connection.execute(
            update(export_record)
            .where(
                export_record.c.kind == "export",
                export_record.c.state.in_(("in_progress", "complete", "failed")),
                or_(export_record.c.id.in_(affected), ~indexed),
            )
            .values(
                state="removed_by_deletion",
                removed_at=now,
                deletion_record_id=deletion_id,
            )
            .returning(export_record.c.id)
        ).scalars()
    )
    if removed:
        enqueue_job(
            uow.connection,
            kind=SWEEP_JOB_KIND,
            payload={"workspace_id": str(ctx.workspace_id)},
            now=now,
            max_attempts=20,
        )
        if isinstance(uow, HandlerUnitOfWork):
            uow.request_due_mark()
    return len(cancelled), len(held), len(removed)
