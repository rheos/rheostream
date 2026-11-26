"""Reported paid (#639): the owner says a bill is paid, it leaves the owed list, and
the derive checks it against the statements later (``reported``; ``derive.presumed``
"Reported paid, then checked"; ``stage15_apply`` "Status").

Seams: the two operations through ``dispatch``, ``derive.run`` as the status it leaves
on ``obligation``, and ``finance.owed.list``. Over synthetic chequing exports whose
running balances bound each statement, and typed-in bills. Every date, amount and name
is invented; no floor is set unless a test says so, so nothing is history.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select

from rheo_finance.clock import FakeClock
from rheo_finance.contracts import Role
from rheo_finance.decision import DecisionRefused
from rheo_finance.derive.run import run
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.operations import dispatch
from rheo_finance.reported import ReportOut
from rheo_finance.reports.owed import ListIn, ListOut, list_owed
from rheo_finance.storage.schema import decision, statement_period

from .support import CSV_IDENTIFIER, SECOND, D, Ledger, read_as_text
from .test_payment_application import bills, with_settings
from .test_presumed_paid import ISP, filler, payees, rows, statement

BILL = ("isp", "69.12", D(2055, 4, 1), D(2055, 4, 25))
APRIL_END = D(2055, 4, 30)
NAME = "isp bill-2055-04"


def at(ledger: Ledger, day: dt.date, hour: int = 18) -> None:
    """Move the clock to ``day`` at ``hour`` UTC."""
    ledger.ctx = dataclasses.replace(
        ledger.ctx,
        clock=FakeClock(dt.datetime(day.year, day.month, day.day, hour, tzinfo=dt.UTC)),
    )


def call(ledger: Ledger, name: str, ref: str) -> BaseModel:
    out = dispatch(name, {"obligation_ref": ref}, Role.OWNER, ctx=ledger.ctx)
    assert isinstance(out, ReportOut)
    return out


def ref(ledger: Ledger, name: str = NAME) -> str:
    return f"finance.obligation:{rows(ledger)[name].id}"


def report(ledger: Ledger, name: str = NAME) -> BaseModel:
    return call(ledger, "finance.obligation.report_paid", ref(ledger, name))


def unreport(ledger: Ledger, name: str = NAME) -> BaseModel:
    return call(ledger, "finance.obligation.unreport_paid", ref(ledger, name))


def owed_list(ledger: Ledger) -> ListOut:
    """The list's own handler on the test's transaction, which a reader connection
    would not see; ``test_owed_ops`` covers the reader."""
    return list_owed(ledger.ctx, ledger.uow, ListIn())


def status(ledger: Ledger, name: str = NAME) -> str:
    return str(rows(ledger)[name].status)


def march(ledger: Ledger, identifier: str = CSV_IDENTIFIER, n: int = 1) -> None:
    statement(
        ledger,
        f"march-{n}",
        identifier,
        filler(D(2055, 3, 1), n),
        filler(D(2055, 3, 31), n + 1),
    )


def april(ledger: Ledger, *, last: dt.date = APRIL_END, paid: str = "") -> None:
    """Chequing's April export, ending on ``last``, with an ISP payment of ``paid`` on
    April 23 when given."""
    lines = [filler(D(2055, 4, 1), 3)]
    if paid:
        lines.append((D(2055, 4, 23), ISP, -Decimal(paid)))
    lines.append(filler(last, 4))
    statement(ledger, "april", CSV_IDENTIFIER, *lines)


@pytest.fixture
def reported(ledger: Ledger) -> Ledger:
    """One ISP bill due April 25 (2055) on chequing, had in March, reported paid on
    the evening of April 20 UTC."""
    ledger.chequing()
    payees(ledger)
    with_settings(ledger, "")
    ledger.ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)
    march(ledger)
    bills(ledger, BILL)
    ledger.derive()
    at(ledger, D(2055, 4, 20))
    report(ledger)
    return ledger


# --- reporting ------------------------------------------------------------------------


def test_reporting_takes_the_bill_off_the_list_at_once(reported: Ledger) -> None:
    """No derive in between: the operation sets what the derive would, and the list
    reads it from the views straight away."""
    found = rows(reported)[NAME]
    assert found.status == "presumed_paid"
    assert found.presumed_paid_decision_id is not None
    row = next(r for r in owed_list(reported).rows if r.period_label == "bill-2055-04")
    assert (row.status, row.reported_unconfirmed) == ("presumed_paid", False)
    assert row.reported_at == dt.datetime(2055, 4, 20, 18, tzinfo=dt.UTC)
    assert owed_list(reported).overdue == 0


def test_a_derive_keeps_it_reported_until_the_statements_come_in(
    reported: Ledger,
) -> None:
    before = rows(reported)[NAME]

    reported.derive()

    after = rows(reported)[NAME]
    assert (after.status, after.presumed_paid_decision_id) == (
        "presumed_paid",
        before.presumed_paid_decision_id,
    )
    # A rebuild reads the same decision and lands in the same place.
    run(reported.uow, reported.ctx, extract_pdf=read_as_text, reparse=True)
    assert status(reported) == "presumed_paid"


def test_the_payment_on_a_statement_confirms_it(reported: Ledger) -> None:
    april(reported, paid="69.12")

    reported.derive()

    found = rows(reported)[NAME]
    assert (found.status, found.paid) == ("paid", Decimal("69.12"))
    # Paid is not owed: it leaves the list for good.
    assert all(r.period_label != "bill-2055-04" for r in owed_list(reported).rows)


def test_a_statement_without_the_payment_puts_it_back_as_owed(reported: Ledger) -> None:
    """Chequing's April export runs to April 30, past April 20 plus the five days a
    payment may take, and holds no ISP payment: the report was not confirmed."""
    april(reported)

    reported.derive()

    found = rows(reported)[NAME]
    assert found.status == "open"
    assert found.presumed_paid_decision_id is not None
    row = next(r for r in owed_list(reported).rows if r.period_label == "bill-2055-04")
    assert row.reported_unconfirmed is True
    assert row.due_in_window == Decimal("69.12")


def test_a_statement_ending_inside_the_lag_proves_nothing(reported: Ledger) -> None:
    """April 24 is inside April 20 plus five days: the payment may still be posting."""
    april(reported, last=D(2055, 4, 24))

    reported.derive()

    assert status(reported) == "presumed_paid"


def test_the_lag_is_a_setting(reported: Ledger) -> None:
    with_settings(reported, "[finance.payments]\nreported_paid_lag_days = 2\n")
    april(reported, last=D(2055, 4, 24))

    reported.derive()

    assert status(reported) == "open"


def test_the_reported_day_is_the_local_one(ledger: Ledger) -> None:
    """Reported at 03:00 UTC on April 25, which is the evening of April 24 in
    Vancouver: April 29 is that day plus five, so an export ending then is enough."""
    ledger.chequing()
    payees(ledger)
    with_settings(ledger, '[finance.locale]\ntimezone = "America/Vancouver"\n')
    ledger.ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)
    march(ledger)
    bills(ledger, BILL)
    ledger.derive()
    at(ledger, D(2055, 4, 25), hour=3)
    report(ledger)
    april(ledger, last=D(2055, 4, 29))

    ledger.derive()

    assert status(ledger) == "open"


def test_every_paying_account_must_have_a_late_statement(ledger: Ledger) -> None:
    """A never-paid payee is paid from every chequing account. ``second`` has nothing
    after March, so its April payment could still be there: it stays reported."""
    ledger.accounts(
        ("chequing", "Alder", "chequing", CSV_IDENTIFIER),
        ("second", "Alder", "chequing", SECOND),
    )
    payees(ledger)
    with_settings(ledger, "")
    ledger.ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)
    march(ledger)
    march(ledger, SECOND, n=5)
    bills(ledger, BILL)
    ledger.derive()
    at(ledger, D(2055, 4, 20))
    report(ledger)
    april(ledger)

    ledger.derive()

    assert status(ledger) == "presumed_paid"


def test_a_partial_payment_stays_reported_until_the_check_then_shows_what_is_left(
    reported: Ledger,
) -> None:
    """33.56 of 69.12 paid on April 23 in an export that ends April 24: short of the
    bill, still inside the lag, so it stays off the list. Once May's export is in, the
    rest was never paid: partial, with 35.56 left."""
    with_settings(reported, '[finance.payments]\npartial_auto_kinds = ["bill"]\n')
    april(reported, last=D(2055, 4, 24), paid="33.56")
    reported.derive()
    found = rows(reported)[NAME]
    assert (found.status, found.paid) == ("presumed_paid", Decimal("33.56"))

    statement(
        reported,
        "may",
        CSV_IDENTIFIER,
        filler(D(2055, 5, 1), 7),
        filler(D(2055, 5, 31), 8),
    )
    reported.derive()

    found = rows(reported)[NAME]
    assert (found.status, found.paid, found.remaining) == (
        "partial",
        Decimal("33.56"),
        Decimal("35.56"),
    )


def test_reporting_it_again_starts_the_check_from_the_new_day(reported: Ledger) -> None:
    april(reported)
    reported.derive()
    assert status(reported) == "open"
    first = rows(reported)[NAME].presumed_paid_decision_id

    at(reported, D(2055, 5, 2))
    report(reported)
    reported.derive()

    found = rows(reported)[NAME]
    assert found.status == "presumed_paid"
    assert found.presumed_paid_decision_id not in (None, first)
    reversals = reported.conn.scalar(
        select(func.count()).where(
            decision.c.kind == "reverse", decision.c.reverses_id == first
        )
    )
    assert reversals == 1


# --- undo -----------------------------------------------------------------------------


def test_undo_puts_it_back_at_once_and_after_a_derive(reported: Ledger) -> None:
    out = unreport(reported)

    assert getattr(out, "status", None) == "open"
    found = rows(reported)[NAME]
    assert (found.status, found.presumed_paid_decision_id) == ("open", None)
    reported.derive()
    found = rows(reported)[NAME]
    assert (found.status, found.presumed_paid_decision_id) == ("open", None)


def test_undo_clears_an_unconfirmed_report(reported: Ledger) -> None:
    april(reported)
    reported.derive()

    unreport(reported)
    reported.derive()

    row = next(r for r in owed_list(reported).rows if r.period_label == "bill-2055-04")
    assert (row.status, row.reported_unconfirmed) == ("open", False)


# --- refusals -------------------------------------------------------------------------


def test_a_bill_already_reported_or_paid_is_refused(reported: Ledger) -> None:
    with pytest.raises(DecisionRefused, match="nothing is owed"):
        report(reported)
    april(reported, paid="69.12")
    reported.derive()
    with pytest.raises(DecisionRefused, match="nothing is owed"):
        report(reported)


def test_undo_with_nothing_reported_is_refused(reported: Ledger) -> None:
    unreport(reported)
    with pytest.raises(DecisionRefused, match="not reported paid"):
        unreport(reported)


@pytest.mark.parametrize(
    "bad",
    [
        "finance.txn:0190f5a0-0000-7000-8000-000000000001",
        "not a ref",
        f"finance.obligation:{uuid.UUID(int=7 << 76 | 1 << 63)}",
    ],
    ids=["another type", "not a ref", "no such obligation"],
)
def test_a_ref_that_names_no_obligation_is_refused(reported: Ledger, bad: str) -> None:
    with pytest.raises(DecisionRefused):
        call(reported, "finance.obligation.report_paid", bad)


def test_a_job_cannot_report(reported: Ledger) -> None:
    service = dataclasses.replace(reported.ctx, role=Role.SERVICE)
    with pytest.raises(Exception, match="refuses role service"):
        dispatch(
            "finance.obligation.report_paid",
            {"obligation_ref": ref(reported)},
            Role.SERVICE,
            ctx=service,
        )


# --- history --------------------------------------------------------------------------


def test_history_before_the_floor_is_never_checked(reported: Ledger) -> None:
    with_settings(reported, "[finance.reminders]\nmin_due_date = 2055-09-01\n")
    april(reported)

    reported.derive()

    assert status(reported) == "presumed_paid"


def test_the_exports_bound_the_statements_the_check_reads(reported: Ledger) -> None:
    """The check reads ``statement_period``: each chequing export is one."""
    april(reported)
    reported.derive()
    ends = reported.conn.scalars(select(statement_period.c.period_end)).all()
    assert D(2055, 4, 30) in ends
