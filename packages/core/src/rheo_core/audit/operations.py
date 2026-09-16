"""``core.audit.list``'s models and handler, beside the repository they read.

The declaration and the registration live in ``operations/core_ops.py``; the models
and the handler live **here**. That is the split ``work/operations.py`` established for
``core.work.failures`` and ``operations/operation_ops.py`` repeats for the three
``core.operation`` operations, and here it is an import-direction constraint rather
than a preference: ``rheo_core.operations``'s own ``__init__.py`` imports ``core_ops``
as its first statement, so **nothing in this module may import from
``rheo_core.operations``**. The reverse import would close a real cycle, and this
package is on the far side of one that already runs the other way —
``operations/dispatch.py`` imports ``rheo_core.audit``.

Nothing here needs one. ``dispatch()`` is what authorizes the call, validates the
input, seals the unit of work and turns a refusal into an outcome, and this read
handler raises nothing on its happy path.

**This is the supported read of the audit record** (AC 26 / criterion 14's third
sentence). A caller — and every test in this run — reads audit rows through this
operation and never by querying ``core.audit_record``, which is what makes "readable
through a supported operation rather than a raw table read" a property of the system
rather than of a convention.
"""

from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from rheo_contracts import WorkspaceContext

from rheo_core.audit.records import AuditRow, list_audit_records
from rheo_core.storage.backend import UnitOfWork

AUDIT_LIST: Final = "core.audit.list"

# See ``rheo_core.operations.core_ops`` for why ``ignore`` (the default) is stated.
_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")


class AuditListInput(BaseModel):
    """How many of the most recent audit records to return.

    ``limit`` is bounded rather than a bare ``int``, for the reason
    ``work/operations.py``'s ``FailureListInput`` gives: a bare ``int`` lets ``-1``
    reach the ``SELECT``, where Postgres raises *LIMIT must not be negative* and
    ``dispatch`` folds a driver class name into ``handler_failed``, when it is an
    input defect that belongs in the ``input_invalid`` refusal pydantic already
    produces. Nothing prunes this table in release one, so an unbounded ``limit``
    would be a read that grows for the life of the deployment.
    """

    model_config = _IGNORE_EXTRA

    limit: int = Field(default=50, ge=1, le=500)


class AuditRecord(BaseModel):
    """One audit row as this operation publishes it.

    :class:`~rheo_core.audit.records.AuditRow` field for field, with that row's ``id``
    published as ``audit_id`` — the renaming ``FailedJob`` and ``OperationRecord``
    already make for the same reason — and with ``request_digest`` published as its
    lowercase hex. The column is ``bytea``; a generated TypeScript client has no
    natural type for raw bytes, and hex is what a person comparing two digests by eye
    can actually compare.
    """

    model_config = ConfigDict(frozen=True)

    audit_id: UUID
    occurred_at: datetime
    actor_kind: str
    actor_id: UUID | None
    entry: str
    operation_name: str
    safety_class: str
    operation_id: UUID | None
    subject_ref: str | None
    request_digest: str
    outcome: str


class AuditList(BaseModel):
    """The workspace's most recent audit records, newest first.

    ``records`` is a named collection field rather than a bare list, mirroring
    ``FailureList`` and ``OperationList``: a later run adding a second collection
    beside it is then an additive change a reader may ignore rather than a change to
    the response's own type.
    """

    model_config = ConfigDict(frozen=True)

    records: list[AuditRecord]


def _published(row: AuditRow) -> AuditRecord:
    return AuditRecord(
        audit_id=row.id,
        occurred_at=row.occurred_at,
        actor_kind=row.actor_kind,
        actor_id=row.actor_id,
        entry=row.entry,
        operation_name=row.operation_name,
        safety_class=row.safety_class,
        operation_id=row.operation_id,
        subject_ref=row.subject_ref,
        request_digest=row.request_digest.hex(),
        outcome=row.outcome,
    )


def audit_list_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: AuditListInput
) -> AuditList:
    """The most recent audit records of the context's own workspace.

    Named ``audit_list_handler`` rather than ``list_handler`` because ``core_ops.py``
    already imports a ``list_handler`` from ``operation_ops.py``, and two handlers
    under one name in one importer is a rename waiting to bind the wrong function.

    The workspace is the context's: ``uow.connection`` is already routed to it, so no
    input field names one and none can.
    """
    return AuditList(
        records=[
            _published(row)
            for row in list_audit_records(uow.connection, limit=model_input.limit)
        ]
    )
