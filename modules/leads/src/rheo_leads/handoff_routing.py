"""Routing a handoff to Current through core's approved external action.

Leads never writes Current and never imports it. A handoff to Current is staged with
core's ``stage_external_in_transaction`` under the requesting person's own context,
in the same transaction as the handoff row, as one attempt of the public KEYED
``current.item.create_external``. The attempt's core key is
``<handoff-ref>#<attempt>``; the request's ``source_ref`` is the handoff ref on every
attempt, so Current converges every attempt of one handoff on one item.

Core's action is the authority for what happened. Leads copies it through the public
``core.external_action.get`` read; it never queries core's tables.
"""

from datetime import datetime
from typing import Any, Final
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.operations.refusals import (
    MODULE_DISABLED,
    OPERATION_UNKNOWN,
    OperationRefused,
)
from rheo_core.operations.transaction import (
    DESTINATION_GUARD_REFUSED,
    call_in_transaction,
    stage_external_in_transaction,
)
from rheo_core.refs.resolver import NOT_FOUND, UnitOfWork
from rheo_core.work.schedules import ensure_module_schedule
from sqlalchemy import update

from rheo_leads.configuration import MODULE_ID
from rheo_leads.references import ref
from rheo_leads.storage import pipeline as p

CURRENT: Final = "current"
CREATE_EXTERNAL: Final = "current.item.create_external"
EXTERNAL_ACTION_GET: Final = "core.external_action.get"
RESERVED: Final = frozenset({"destination_ref", "source_ref", "idempotency_key"})
"""Request fields Leads sets itself; a handoff payload may not name them."""
UNAVAILABLE: Final = frozenset(
    {
        OPERATION_UNKNOWN,
        MODULE_DISABLED,
        "module_unavailable",
        DESTINATION_GUARD_REFUSED,
    }
)
"""Staging refusals that mean Current is absent, disabled, not ready or unavailable,
as opposed to a refusal of the request or of the person making it."""

NO_DESTINATION: Final = "no_destination"
DESTINATION_UNAVAILABLE: Final = "destination_unavailable"
EXTERNAL_ACTION_MISSING: Final = "external_action_missing"
RESTORED_UNRESOLVED: Final = "restored_unresolved"
NOT_RESTAGEABLE: Final = "handoff_not_restageable"
RECONCILE_SCHEDULE: Final = "current_links_reconcile"

CORE_STATES: Final = {
    "pending": "pending",
    "executing": "pending",
    "succeeded": "succeeded",
    "failed": "failed",
    "refused": "refused",
    "cancelled": "cancelled",
    "unresolved": "unresolved",
}
"""Core's action states as the handoff records them: both open states are pending."""


def attempt_key(handoff_ref: str, attempt: int) -> str:
    """The core idempotency key of one staged attempt."""
    if attempt < 1:
        raise ValueError("attempts are numbered from one")
    return f"{handoff_ref}#{attempt}"


def current_request(
    handoff_ref: str, attempt: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """The ``current.item.create_external`` input for one attempt of a handoff."""
    named = RESERVED & set(payload)
    if named:
        raise OperationRefused(
            "input_invalid",
            "a handoff payload may not set destination_ref, source_ref or "
            "idempotency_key",
        )
    return {
        **payload,
        "destination_ref": CURRENT,
        "source_ref": handoff_ref,
        "idempotency_key": attempt_key(handoff_ref, attempt),
    }


def restage_allowed(
    state: str, detail: str | None, destination_ref: str | None
) -> bool:
    """The closed restage matrix.

    Only four classes may be staged again, each under a new approval: a handoff
    Current was unavailable for at request time, a core refusal before any effect
    because the destination was unavailable, and a ``failed`` action, which core
    records only with proof of no effect (an exhausted retryable no-effect, or a
    person's resolution to failed). Pending, succeeded, permanently refused,
    cancelled and unresolved handoffs never restage; an outcome-unknown effect is
    unresolved, so it never restages either.
    """
    if destination_ref != CURRENT:
        return False
    if state == "unavailable":
        return detail == DESTINATION_UNAVAILABLE
    if state == "refused":
        return detail == "destination_unavailable_no_effect"
    return state == "failed"


def stage(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    handoff: dict[str, Any],
    payload: dict[str, Any],
    *,
    attempt: int,
    now: datetime,
) -> dict[str, Any] | None:
    """Hold one attempt for approval; the handoff's new values, or ``None`` when
    Current is unavailable, in which case core wrote nothing.

    Any other refusal (the person's role or token, an invalid request) propagates
    and refuses the whole request.
    """
    reference = ref("handoff", handoff["id"])
    body = current_request(reference, attempt, payload)
    try:
        staged = stage_external_in_transaction(
            ctx, uow, CREATE_EXTERNAL, body, now=now, source_ref=reference
        )
    except OperationRefused as refused:
        if refused.state in UNAVAILABLE:
            return None
        raise
    # The lifecycle lock is held, which orders this against module enable.
    ensure_module_schedule(
        uow.connection,
        module_id=MODULE_ID,
        name=RECONCILE_SCHEDULE,
        enabled=True,
        due_at=now,
    )
    return dict(
        state=CORE_STATES.get(staged.state, "pending"),
        attempt_count=attempt,
        approval_id=staged.approval_id,
        external_action_id=staged.action_id,
        result_ref=None,
        result_detail=None,
        resolved_at=None,
    )


class ActionRead:
    """One public read of a core action: its record, proven absent, or unreadable."""

    __slots__ = ("missing", "record")

    def __init__(self, record: dict[str, Any] | None, *, missing: bool) -> None:
        self.record = record
        self.missing = missing


def read_action(ctx: WorkspaceContext, uow: UnitOfWork, action_id: UUID) -> ActionRead:
    """``core.external_action.get`` under ``ctx``, inside a savepoint."""
    try:
        with uow.connection.begin_nested():
            record = call_in_transaction(
                ctx,
                uow,
                EXTERNAL_ACTION_GET,
                {"external_action_id": str(action_id)},
            )
    except OperationRefused as refused:
        return ActionRead(None, missing=refused.state == NOT_FOUND)
    return ActionRead(record.model_dump(), missing=False)


def outcome_values(
    handoff: dict[str, Any], read: ActionRead, now: datetime
) -> dict[str, Any]:
    """The handoff columns a core read changes; empty when nothing changes.

    A pending handoff whose action no longer exists (a restored workspace carries no
    core action) becomes unresolved: nothing can prove whether the item was made.
    A handoff Leads already settled keeps what it recorded from core.
    """
    if read.record is None:
        if read.missing and handoff["state"] == "pending":
            values: dict[str, Any] = dict(
                state="unresolved",
                result_ref=None,
                result_detail=EXTERNAL_ACTION_MISSING,
                resolved_at=now,
            )
        else:
            return {}
    else:
        record = read.record
        state = CORE_STATES[str(record["state"])]
        if state == "pending":
            values = dict(state=state)
        else:
            succeeded = state == "succeeded"
            values = dict(
                state=state,
                result_ref=record["result_ref"] if succeeded else None,
                result_detail=None
                if succeeded
                else record["failure_class"] or record["error_code"],
                resolved_at=record["terminal_at"],
            )
    return {key: value for key, value in values.items() if handoff[key] != value}


def write(uow: UnitOfWork, handoff: dict[str, Any], values: dict[str, Any]) -> bool:
    """Apply ``values`` to the handoff row and to ``handoff``; whether it moved."""
    if not values:
        return False
    uow.connection.execute(
        update(p.handoff).where(p.handoff.c.id == handoff["id"]).values(**values)
    )
    handoff.update(values)
    return True


def refresh(
    ctx: WorkspaceContext, uow: UnitOfWork, handoff: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Copy core's outcome for the handoff's latest attempt, when ``ctx`` may read
    it. A handoff with no staged attempt, or an unreadable action, is unchanged."""
    if handoff["external_action_id"] is None:
        return handoff
    read = read_action(ctx, uow, handoff["external_action_id"])
    write(uow, handoff, outcome_values(handoff, read, now))
    return handoff
