"""Reported paid: the owner says a bill is paid before a statement shows it (#639).

``finance.obligation.report_paid`` records a ``presumed_paid`` decision on one owed
obligation and takes it off the owed list at once: it sets what the next derive would
set (the decision named, the status ``presumed_paid``), so the screen need not wait for
the tick. ``finance.obligation.unreport_paid`` reverses that decision and puts the
obligation back as the payments leave it. The derive then keeps it presumed paid until
a payment confirms it, or until the paying accounts' statements come in without one
(``derive.presumed``, "Reported paid, then checked").

Only an obligation with something left to pay toward its threshold (a card's minimum,
every other kind's amount due), status ``open`` or ``partial`` and not due before the
reminders floor (history, which the derive never checks) can be reported: one
the derive put back because its statements lacked the payment is ``open`` again, so it
can be reported a second time, which reverses the earlier decision and starts the check
from today. The owner alone runs either, never a job: ``owner_only``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update

from rheo_finance import decision as decisions
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.decision import DecisionKind, DecisionRefused
from rheo_finance.derive.presumed import presumed
from rheo_finance.derive.stage15_apply import (
    OBLIGATION_TYPE,
    OPEN,
    PARTIAL,
    PRESUMED_PAID,
)
from rheo_finance.operations import OperationSpec, register
from rheo_finance.storage import refs
from rheo_finance.storage.schema import obligation
from rheo_finance.tier import Tier

P: Final = Tier.PUBLIC
REPORTABLE: Final = frozenset({OPEN, PARTIAL})


class ReportIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    obligation_ref: str


class ReportOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    obligation_ref: Annotated[str, Tiered(P)]
    status: Annotated[str, Tiered(P)]
    decision_id: Annotated[uuid.UUID | None, Tiered(P)]


def _obligation_id(ref: str) -> uuid.UUID:
    try:
        parsed = refs.parse(ref)
    except refs.RefError:
        raise DecisionRefused(
            "report_paid: obligation_ref is not a reference"
        ) from None
    if (parsed.system, parsed.type) != (refs.FINANCE_SYSTEM, OBLIGATION_TYPE):
        raise DecisionRefused("report_paid: obligation_ref names no obligation")
    return uuid.UUID(parsed.id)


def _reverse_reported(
    ctx: FinanceContext, uow: UnitOfWork, obligation_id: uuid.UUID
) -> bool:
    """Reverse the decision that names this obligation, if one does; whether one did."""
    found = presumed(uow).get(obligation_id)
    if found is None:
        return False
    decisions.reverse(ctx, uow, decisions.ReverseIn(decision_id=found))
    return True


def report_paid(ctx: FinanceContext, uow: UnitOfWork, data: ReportIn) -> ReportOut:
    key = _obligation_id(data.obligation_ref)
    o = obligation.c
    row = uow.execute(
        select(o.status, o.threshold_remaining, o.before_floor)
        .where(o.id == key)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise DecisionRefused("report_paid: no such obligation")
    if row.status not in REPORTABLE or not row.threshold_remaining > 0:
        raise DecisionRefused("report_paid: nothing is owed on that obligation")
    if row.before_floor:
        raise DecisionRefused("report_paid: that obligation is due before the floor")
    _reverse_reported(ctx, uow, key)
    out = decisions.record(
        ctx,
        uow,
        decisions.RecordIn(
            kind=DecisionKind.PRESUMED_PAID, obligation_ref=data.obligation_ref
        ),
    )
    uow.execute(
        update(obligation)
        .where(o.id == key)
        .values(presumed_paid_decision_id=out.decision_id, status=PRESUMED_PAID)
    )
    return ReportOut(
        obligation_ref=data.obligation_ref,
        status=PRESUMED_PAID,
        decision_id=out.decision_id,
    )


def unreport_paid(ctx: FinanceContext, uow: UnitOfWork, data: ReportIn) -> ReportOut:
    key = _obligation_id(data.obligation_ref)
    o = obligation.c
    row = uow.execute(
        select(o.status, o.paid).where(o.id == key).with_for_update()
    ).one_or_none()
    if row is None:
        raise DecisionRefused("unreport_paid: no such obligation")
    if not _reverse_reported(ctx, uow, key):
        raise DecisionRefused("unreport_paid: that obligation is not reported paid")
    # Every decision the button makes reverses the one before it, but an older one
    # the confirm queue recorded may still stand: the next derive reads it again.
    left = presumed(uow).get(key)
    status = row.status
    if status == PRESUMED_PAID and left is None:
        status = PARTIAL if row.paid else OPEN
    uow.execute(
        update(obligation)
        .where(o.id == key)
        .values(presumed_paid_decision_id=left, status=status)
    )
    return ReportOut(
        obligation_ref=data.obligation_ref, status=status, decision_id=left
    )


for _name, _handler, _what in (
    (
        "finance.obligation.report_paid",
        report_paid,
        "Say one owed bill is paid: it leaves the owed list until its statements"
        " confirm the payment or come in without it.",
    ),
    (
        "finance.obligation.unreport_paid",
        unreport_paid,
        "Undo saying one bill is paid: it is owed again as its payments leave it.",
    ),
):
    register(
        OperationSpec(
            name=_name,
            input_model=ReportIn,
            output_model=ReportOut,
            handler=_handler,
            safety_class=SafetyClass.MUTATE,
            roles=frozenset({Role.OWNER}),
            connection="owner",
            owner_only=True,
            description=_what,
        )
    )
