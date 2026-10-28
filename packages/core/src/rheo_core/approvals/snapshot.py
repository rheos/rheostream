"""The shared verifier of an approval's stored snapshot against its binding.

It answers one question of an approval row — is the stored snapshot the call that was
approved? — for exactly these call sites:

- ``core.approval.preview`` (``approvals/operations.py:preview_handler``);
- both decisions, ``core.approval.approve`` and ``.refuse``, under the row lock
  (``approvals/operations.py:_lock_for_decision``);
- inline execution, :func:`~rheo_core.approvals.gate.execute_approved`, which runs a
  DESTRUCTIVE call and the legacy unkeyed hold.

So the preview can never call "verified" a row the inline gate refuses, or the
reverse. **The staged EXTERNAL path does not call it yet.**
``operations/continuation.py:queue_approved_external``, which queues the continuation
when a staged action is approved, and the grant-bound continuation at its effect keep
their own copy of the body, validation and digest checks. The approval of such a call
has already passed this verifier under the decision's lock, so that copy can only
refuse more, never less; routing it through here is separate work. The window is
deliberately not part of it: the preview reports the window as a display status and
the gate compares it with ``binding.binding_detail`` at the instant of execution.

**What verified means**, every clause answering ``invalid_approval`` except the first:

- the operation is registered in this process (else ``operation_unknown``, the state
  the gate has always answered for a module dropped between hold and approval);
- the snapshot is still stored (the deletion cascade removes it);
- it validates against the declared input model;
- the canonical digest of the **validated input as it will execute** equals the row's
  digest. This is the comparison ``binding.py`` derives and the gate always made. The
  digest of the raw stored body is *not* compared: what is shown and what executes
  are both the validated input, and ``jsonb`` normalises numbers (``1e+16`` is read
  back as the integer ``10000000000000000``), so a raw-body comparison would refuse a
  legitimately stored row whose validated input still matches;
- the destination binding is exact: for a declaration with an ``ExternalSpec`` the row
  and the validated input both name exactly its declared destination, and for any
  other class the row names none (``confirmation-and-safety.md``: "a differing
  destination ... refuses with ``invalid_approval``");
- the declared subject field, when there is one, carries a ``RecordRef``;
- for a KEYED EXTERNAL or FINANCIAL declaration, the staged ``core.external_action``
  bound to this approval exists and names the same operation record, operation,
  destination and payload digest. Such a call is only ever executed by the grant-bound
  continuation; without its staged action and keyed reservation — which restore does
  not carry — approving it would run the operation inline, outside the at-most-once
  path.

Read-only, and it takes the caller's connection, so it judges the row the caller has
locked when the caller holds the lock.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ValidationError
from rheo_contracts import (
    Idempotency,
    OperationDeclaration,
    RecordRef,
    SafetyClass,
)
from sqlalchemy import Connection

from rheo_core.approvals.binding import (
    INVALID_APPROVAL,
    executable_payload,
    payload_digest,
)
from rheo_core.approvals.records import ApprovalRow, payload_body
from rheo_core.operations.external_actions import get_for_approval
from rheo_core.operations.refusals import OPERATION_UNKNOWN
from rheo_core.operations.registry import (
    REGISTRY,
    OperationRegistry,
    RegisteredOperation,
)

_STAGED_CLASSES: Final = frozenset({SafetyClass.EXTERNAL, SafetyClass.FINANCIAL})


@dataclass(frozen=True, slots=True)
class VerifiedSnapshot:
    """An approval's stored snapshot, proved to be the bound one.

    ``operation`` is the registry entry the verifier looked up, carried so a caller
    that runs it needs no second lookup. ``payload`` is the validated input as it will
    execute, the form whose digest the row carries. ``subject`` comes from that
    input's declared subject field, never from the stored ``subject_ref`` column.
    """

    operation: RegisteredOperation
    declaration: OperationDeclaration
    model_input: BaseModel
    payload: dict[str, object]
    subject: RecordRef | None
    destination_ref: str | None


@dataclass(frozen=True, slots=True)
class SnapshotRefusal:
    """Why a snapshot does not verify: the refusal state and a detail for the log.

    The state is ``operation_unknown`` or ``invalid_approval``; the detail names the
    clause, for the operator, and is never shown to a preview viewer. ``cause`` is
    the closed machine-readable reason callers use when one refusal has a narrower
    safe capability than the rest; callers never parse the detail text."""

    state: str
    cause: "SnapshotRefusalCause"
    detail: str


class SnapshotRefusalCause(StrEnum):
    """Closed causes for a snapshot-verification refusal."""

    OPERATION_UNKNOWN = "operation_unknown"
    PAYLOAD_ABSENT = "payload_absent"
    PAYLOAD_INVALID = "payload_invalid"
    PAYLOAD_DIGEST_MISMATCH = "payload_digest_mismatch"
    DESTINATION_MISMATCH = "destination_mismatch"
    SUBJECT_INVALID = "subject_invalid"
    STAGED_ACTION_ABSENT = "staged_action_absent"
    STAGED_ACTION_MISMATCH = "staged_action_mismatch"


def _refused(cause: SnapshotRefusalCause, detail: str) -> SnapshotRefusal:
    return SnapshotRefusal(INVALID_APPROVAL, cause, detail)


def verify_snapshot(
    conn: Connection, row: ApprovalRow, *, registry: OperationRegistry = REGISTRY
) -> VerifiedSnapshot | SnapshotRefusal:
    """The approval's verified snapshot, or why it does not verify. Read-only."""
    operation = registry.lookup(row.operation_name)
    if operation is None:
        return SnapshotRefusal(
            OPERATION_UNKNOWN,
            SnapshotRefusalCause.OPERATION_UNKNOWN,
            f"{row.operation_name} is approved but not registered in this process",
        )
    declaration = operation.declaration
    body = payload_body(conn, approval_id=row.id)
    if body is None:
        return _refused(
            SnapshotRefusalCause.PAYLOAD_ABSENT,
            f"the payload snapshot of approval {row.id} is no longer stored",
        )
    try:
        model_input = declaration.input_model.model_validate(body)
    except ValidationError:
        return _refused(
            SnapshotRefusalCause.PAYLOAD_INVALID,
            f"the stored payload of approval {row.id} no longer validates against "
            f"{declaration.input_model.__name__}",
        )
    payload = executable_payload(model_input)
    if payload_digest(payload) != row.payload_digest:
        return _refused(
            SnapshotRefusalCause.PAYLOAD_DIGEST_MISMATCH,
            "the payload differs from the one that was approved",
        )
    external = declaration.external
    if external is None:
        if row.destination_ref is not None:
            return _refused(
                SnapshotRefusalCause.DESTINATION_MISMATCH,
                f"approval {row.id} names a destination its operation has none for",
            )
    elif (
        row.destination_ref != external.destination_ref
        or getattr(model_input, external.destination_field, None)
        != external.destination_ref
    ):
        return _refused(
            SnapshotRefusalCause.DESTINATION_MISMATCH,
            f"approval {row.id} is not bound to the declared destination "
            f"{external.destination_ref!r}",
        )
    subject: RecordRef | None = None
    subject_field = (
        None if declaration.audit is None else declaration.audit.subject_field
    )
    if subject_field is not None:
        value = getattr(model_input, subject_field, None)
        if value is not None and not isinstance(value, RecordRef):
            return _refused(
                SnapshotRefusalCause.SUBJECT_INVALID,
                f"the subject field {subject_field!r} carries no reference",
            )
        subject = value
    if (
        declaration.safety_class in _STAGED_CLASSES
        and declaration.idempotency is Idempotency.KEYED
    ):
        staged = get_for_approval(conn, approval_id=row.id)
        if staged is None:
            return _refused(
                SnapshotRefusalCause.STAGED_ACTION_ABSENT,
                f"approval {row.id} has no staged external action bound to it",
            )
        if (
            staged.operation_id != row.operation_id
            or staged.operation_name != row.operation_name
            or staged.destination_ref != row.destination_ref
            or staged.approval_payload_digest != row.payload_digest
        ):
            return _refused(
                SnapshotRefusalCause.STAGED_ACTION_MISMATCH,
                f"approval {row.id} has a mismatched staged external action binding",
            )
    return VerifiedSnapshot(
        operation=operation,
        declaration=declaration,
        model_input=model_input,
        payload=payload,
        subject=subject,
        destination_ref=row.destination_ref,
    )
