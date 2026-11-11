"""Presumed paid, the reminders floor and the presumed-paid candidates (FR-19, FR-26,
FR-67; SPEC Derivation stage 14 "Presumed paid" and "The paying account of a payee";
edge case 80; AC-10's ``paid_from``, AC-82's presumed-paid clause).

Seams: ``derive.presumed.presumed_paid_candidates`` after a derive, and ``derive.run``
as the statuses and the floor marker it leaves on ``obligation``, over synthetic
chequing exports whose running balances make the coverage, typed-in bills and the
owner's decisions recorded through ``finance.decision.record``. Every date, amount and
name is invented.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Row as SqlRow
from sqlalchemy import select

from rheo_finance.clock import local_today
from rheo_finance.derive.presumed import presumed_paid_candidates
from rheo_finance.derive.run import run
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.manual.payees import PAYEES_FILE
from rheo_finance.storage.schema import (
    account,
    coverage,
    obligation,
    payee,
    payment,
)

from .support import CSV_IDENTIFIER, SECOND, THIRD, D, Ledger, read_as_text
from .test_coverage import account_row, register
from .test_invoice_decisions import reverse
from .test_payment_application import (
    Applied,
    applied,
    bills,
    decide,
    line_ref,
    with_settings,
)
from .test_reversals import export

ISP = "AUTO-PULL DEBIT SAMPLE ISP 99911"
LANDLORD = "CAROB E-SEND  receiver = SAMPLE LANDLORD"
FLOOR = "[finance.reminders]\nmin_due_date = 2055-09-01\n"

Line = tuple[dt.date, str, Decimal]


def statement(ledger: Ledger, name: str, identifier: str, *rows: Line) -> None:
    """One chequing export whose running balance verifies its first to last day."""
    ledger.file(
        f"alder/{name}.csv",
        export(*((identifier, day, words, amount) for day, words, amount in rows)),
    )


def filler(day: dt.date, n: int) -> Line:
    return (day, f"SAMPLE SHOP 9993{n}", Decimal("-5.00"))


def payees(ledger: Ledger, *, landlord_paid_from: str = "") -> None:
    ledger.manual(
        PAYEES_FILE,
        "isp,Our net,vendor,SAMPLE ISP,,",
        f"landlord,Our rent,person,SAMPLE LANDLORD,,{landlord_paid_from}",
        "net,Our line,vendor,SAMPLE NET,,",
    )


def history(ledger: Ledger, *, landlord_paid_from: str = "") -> None:
    """Two chequing accounts. ``chequing`` is had January to March and July to August
    2055, so April to June and every month from September are gaps; it paid the ISP
    and the landlord in February. ``second`` is had in December 2054 and March to
    August (2055), so January and February are gaps. The floor is 2055-09-01."""
    ledger.accounts(
        ("chequing", "Alder", "chequing", CSV_IDENTIFIER),
        ("second", "Alder", "chequing", SECOND),
    )
    payees(ledger, landlord_paid_from=landlord_paid_from)
    with_settings(ledger, FLOOR)
    statement(
        ledger,
        "chequing-q1",
        CSV_IDENTIFIER,
        filler(D(2055, 1, 1), 1),
        (D(2055, 2, 15), ISP, Decimal("-77.12")),
        (D(2055, 2, 20), LANDLORD, Decimal("-414.85")),
        filler(D(2055, 3, 31), 2),
    )
    statement(
        ledger,
        "chequing-summer",
        CSV_IDENTIFIER,
        filler(D(2055, 7, 1), 3),
        filler(D(2055, 8, 31), 4),
    )
    statement(
        ledger,
        "second-dec",
        SECOND,
        filler(D(2054, 12, 1), 5),
        filler(D(2054, 12, 31), 6),
    )
    statement(
        ledger,
        "second-spring",
        SECOND,
        filler(D(2055, 3, 1), 7),
        filler(D(2055, 8, 31), 8),
    )
    bills(
        ledger,
        # Due in January, a month chequing has, as it has the month after.
        ("isp", "62.12", D(2055, 1, 7), D(2055, 1, 25)),
        # Paid by the February line.
        ("isp", "77.12", D(2055, 2, 1), D(2055, 2, 25)),
        # Due in March, had, but April, the month after, is a gap.
        ("isp", "68.12", D(2055, 3, 1), D(2055, 3, 25)),
        ("isp", "69.12", D(2055, 4, 1), D(2055, 4, 25)),
        ("isp", "70.12", D(2055, 5, 1), D(2055, 5, 25)),
        # After the floor, in a gap.
        ("isp", "71.12", D(2055, 10, 1), D(2055, 10, 25)),
        # Never paid: every chequing account is a paying account.
        ("net", "42.84", D(2055, 1, 7), D(2055, 1, 25)),
        ("net", "43.84", D(2055, 7, 1), D(2055, 7, 25)),
        # Paid from chequing in February, a payment no landlord bill fits.
        ("landlord", "439.13", D(2055, 1, 7), D(2055, 1, 25)),
        ("landlord", "424.85", D(2055, 5, 1), D(2055, 5, 25)),
    )


def rows(ledger: Ledger) -> dict[str, SqlRow[Any]]:
    """Every obligation, as ``<payee slug> <period label>``."""
    found = ledger.conn.execute(
        select(obligation, payee.c.slug).join(
            payee, payee.c.id == obligation.c.payee_id
        )
    ).all()
    return {f"{row.slug} {row.period_label}": row for row in found}


def offered(ledger: Ledger) -> dict[str, tuple[str, ...]]:
    """The candidates, each with the paying accounts that left a gap."""
    label = {row.id: name for name, row in rows(ledger).items()}
    today = local_today(ledger.ctx.clock, ledger.ctx.settings.timezone)
    return {
        label[each.obligation_id]: each.gaps
        for each in presumed_paid_candidates(ledger.uow, today)
    }


def ref(ledger: Ledger, name: str) -> str:
    return f"finance.obligation:{rows(ledger)[name].id}"


def presume(ledger: Ledger, name: str) -> uuid.UUID:
    return decide(ledger, "presumed_paid", obligation_ref=ref(ledger, name))


# --- the candidates -------------------------------------------------------------------


def test_old_bills_whose_paying_account_has_a_gap_are_offered(ledger: Ledger) -> None:
    """Three ISP bills with a gap on chequing, the account that paid the ISP, for the
    due month or the month after; the January bill, whose months chequing has, the
    paid February bill and the bill after the floor are not. The never-paid line's
    January bill has a gap on ``second`` only, its July bill on neither. The landlord,
    paid from chequing, is tested against chequing."""
    history(ledger)

    ledger.derive()

    assert offered(ledger) == {
        "isp bill-2055-03": ("chequing",),
        "isp bill-2055-04": ("chequing",),
        "isp bill-2055-05": ("chequing",),
        "net bill-2055-01": ("second",),
        "landlord bill-2055-05": ("chequing",),
    }


def test_a_paid_from_row_narrows_the_paying_accounts_to_its_own(
    ledger: Ledger,
) -> None:
    """Payment evidence names chequing, ``paid_from`` names ``second``: only
    ``second`` is read, so its January gap offers the January bill and chequing's May
    gap no longer offers the May one."""
    history(ledger, landlord_paid_from="second")

    ledger.derive()

    found = offered(ledger)
    assert found["landlord bill-2055-01"] == ("second",)
    assert "landlord bill-2055-05" not in found


def test_a_bill_due_after_this_month_is_not_offered(ledger: Ledger) -> None:
    """Presumed paid is a statement about the past (edge case 24). Today is 15 January
    2058 and the floor a year on: the bill due this month is offered on chequing's
    gap, the one due in March is not, though chequing has no data for it either."""
    history(ledger)
    with_settings(ledger, "[finance.reminders]\nmin_due_date = 2059-01-01\n")
    bills(
        ledger,
        ("net", "39.58", D(2058, 1, 7), D(2058, 1, 31)),
        ("net", "40.58", D(2058, 3, 1), D(2058, 3, 25)),
    )

    ledger.derive()

    assert {name for name, row in rows(ledger).items() if row.before_floor} == {
        "net bill-2058-01",
        "net bill-2058-03",
    }
    assert offered(ledger) == {"net bill-2058-01": ("chequing", "second")}


def test_nothing_is_offered_while_the_floor_is_unset(ledger: Ledger) -> None:
    history(ledger)
    with_settings(ledger, "")

    ledger.derive()

    assert offered(ledger) == {}
    assert not any(row.before_floor for row in rows(ledger).values())


# --- which months are gaps -----------------------------------------------------------

CANCEL = "E-SEND OUT UNDONE BY SENDER"
GONE = "999000000055"


def dated_accounts(ledger: Ledger) -> None:
    """``chequing``, had January to March (2055); ``later``, opened 2055-06-01 with
    nothing loaded; ``closed``, open from 2053 to October 2054 with nothing loaded;
    ``gone``, closed in October 2054 with no opened date and nothing loaded. Coverage
    rows: ``later`` from June 2055, ``closed`` from 2053 and ``not_applicable`` after
    October (2054), ``gone`` none."""
    register(
        ledger,
        (account_row("chequing", "Alder", "chequing"), CSV_IDENTIFIER),
        (account_row("later", "Alder", "chequing", opened="2055-06-01"), SECOND),
        (
            account_row(
                "closed",
                "Alder",
                "chequing",
                opened="2053-01-01",
                closed="2054-10-31",
            ),
            THIRD,
        ),
        (account_row("gone", "Alder", "chequing", closed="2054-10-31"), GONE),
    )
    payees(ledger)
    with_settings(ledger, FLOOR)
    statement(
        ledger,
        "chequing-q1",
        CSV_IDENTIFIER,
        filler(D(2055, 1, 1), 1),
        filler(D(2055, 3, 31), 2),
    )


def test_an_account_not_open_in_the_month_leaves_no_gap(ledger: Ledger) -> None:
    """A never-paid payee is tested against every chequing account. January (2055) is
    had on chequing, before ``later`` opened and after ``closed`` closed: no gap, so
    the January bill is not offered, and ``gone`` had closed, though it has no row and
    no opened date. July is a gap on chequing (no data) and on ``later`` (open, no
    data), never on ``closed`` or ``gone``."""
    dated_accounts(ledger)
    bills(
        ledger,
        ("net", "42.84", D(2055, 1, 7), D(2055, 1, 25)),
        ("net", "43.84", D(2055, 7, 1), D(2055, 7, 25)),
    )

    ledger.derive()

    assert offered(ledger) == {"net bill-2055-07": ("chequing", "later")}


def test_a_partial_month_is_a_gap(ledger: Ledger) -> None:
    """Chequing is had 2055-01-01 to 2055-03-20, so March is ``partial``: the
    February bill, whose own month chequing has, is offered on the month after."""
    dated_accounts(ledger)
    statement(
        ledger,
        "chequing-q1",
        CSV_IDENTIFIER,
        filler(D(2055, 1, 1), 1),
        filler(D(2055, 3, 20), 2),
    )
    bills(ledger, ("net", "42.84", D(2055, 2, 1), D(2055, 2, 25)))

    ledger.derive()

    months = dict(
        ledger.conn.execute(
            select(coverage.c.month, coverage.c.state)
            .join(account, account.c.id == coverage.c.account_id)
            .where(account.c.slug == "chequing", coverage.c.month < D(2055, 4, 1))
        ).all()
    )
    assert months == {
        D(2055, 1, 1): "have",
        D(2055, 2, 1): "have",
        D(2055, 3, 1): "partial",
    }
    assert offered(ledger) == {"net bill-2055-02": ("chequing",)}


def test_a_payee_paid_only_from_a_later_account_has_no_gap_before_it_opened(
    ledger: Ledger,
) -> None:
    """The ISP's one payment is on ``later`` in July, so ``later`` is its paying
    account; its January bill falls before ``later`` opened and is not offered."""
    dated_accounts(ledger)
    statement(
        ledger,
        "later-summer",
        SECOND,
        filler(D(2055, 7, 1), 3),
        (D(2055, 7, 15), ISP, Decimal("-86.36")),
        filler(D(2055, 8, 31), 4),
    )
    bills(ledger, ("isp", "62.12", D(2055, 1, 7), D(2055, 1, 25)))

    ledger.derive()

    assert offered(ledger) == {}


def test_a_reversed_payment_names_no_paying_account(ledger: Ledger) -> None:
    """The landlord's only payment, from ``second``, was cancelled: with no active
    payment the paying accounts are every chequing account, and chequing, with no row
    for January and no opened date, is the gap. Read as paying from ``second``, which
    has January and February, nothing would be offered."""
    ledger.accounts(
        ("chequing", "Alder", "chequing", CSV_IDENTIFIER),
        ("second", "Alder", "chequing", SECOND),
    )
    payees(ledger)
    with_settings(ledger, FLOOR)
    statement(
        ledger,
        "chequing-summer",
        CSV_IDENTIFIER,
        filler(D(2055, 7, 1), 1),
        filler(D(2055, 8, 31), 2),
    )
    statement(
        ledger,
        "second-q1",
        SECOND,
        filler(D(2055, 1, 1), 3),
        (D(2055, 2, 8), LANDLORD, Decimal("-797.09")),
        (D(2055, 2, 11), CANCEL, Decimal("797.09")),
        filler(D(2055, 3, 31), 4),
    )
    bills(ledger, ("landlord", "439.13", D(2055, 1, 7), D(2055, 1, 25)))

    ledger.derive()

    assert ledger.conn.execute(select(payment.c.state)).scalars().all() == ["reversed"]
    assert offered(ledger) == {"landlord bill-2055-01": ("chequing",)}


# --- the floor ------------------------------------------------------------------------


def test_every_obligation_due_before_the_floor_is_marked_whatever_its_status(
    ledger: Ledger,
) -> None:
    history(ledger)
    ledger.derive()
    presume(ledger, "isp bill-2055-04")

    ledger.derive()

    found = rows(ledger)
    marked = {name for name, row in found.items() if row.before_floor}
    assert marked == set(found) - {"isp bill-2055-10"}
    assert found["isp bill-2055-02"].status == "paid"
    assert found["isp bill-2055-04"].status == "presumed_paid"


def test_the_floor_day_itself_is_not_before_the_floor(ledger: Ledger) -> None:
    history(ledger)
    with_settings(ledger, "[finance.reminders]\nmin_due_date = 2055-10-25\n")

    ledger.derive()

    found = rows(ledger)
    assert not found["isp bill-2055-10"].before_floor
    assert found["isp bill-2055-05"].before_floor


# --- presumed paid --------------------------------------------------------------------


def test_a_presumed_paid_decision_closes_the_bill_and_its_reversal_reopens_it(
    ledger: Ledger,
) -> None:
    history(ledger)
    ledger.derive()
    decided = presume(ledger, "isp bill-2055-04")

    ledger.derive()

    found = rows(ledger)["isp bill-2055-04"]
    assert (found.status, found.presumed_paid_decision_id) == ("presumed_paid", decided)
    assert (found.paid, found.remaining) == (0, Decimal("69.12"))
    assert "isp bill-2055-04" not in offered(ledger)
    assert len(offered(ledger)) == 4
    # Keyed by the natural key, the decision holds across a rebuild.
    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, reparse=True)
    assert rows(ledger)["isp bill-2055-04"].status == "presumed_paid"

    reverse(ledger, decided)
    ledger.derive()

    found = rows(ledger)["isp bill-2055-04"]
    assert (found.status, found.presumed_paid_decision_id) == ("open", None)
    assert "isp bill-2055-04" in offered(ledger)


def test_a_presumed_paid_decision_leaves_a_bill_with_nothing_owed_paid(
    ledger: Ledger,
) -> None:
    """Only an open obligation becomes presumed paid; the decision still names it."""
    one_bill(ledger)
    bills(ledger, ONE_BILL, ("isp", "0.00", D(2055, 6, 1), D(2055, 6, 25)))
    ledger.derive()
    decided = presume(ledger, "isp bill-2055-06")

    ledger.derive()

    found = rows(ledger)["isp bill-2055-06"]
    assert (found.status, found.presumed_paid_decision_id) == ("paid", decided)


ONE_BILL = ("isp", "69.12", D(2055, 4, 1), D(2055, 4, 25))


def one_bill(ledger: Ledger) -> None:
    """One ISP bill before the floor, presumed paid, and chequing, had in March."""
    ledger.chequing()
    payees(ledger)
    with_settings(ledger, FLOOR)
    # A decision on a line reads the line it names.
    ledger.ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)
    statement(
        ledger,
        "march",
        CSV_IDENTIFIER,
        filler(D(2055, 3, 1), 1),
        filler(D(2055, 3, 31), 2),
    )
    bills(ledger, ONE_BILL)
    ledger.derive()


def test_a_presumed_paid_bill_reverts_to_paid_when_its_line_loads(
    ledger: Ledger,
) -> None:
    one_bill(ledger)
    decided = presume(ledger, "isp bill-2055-04")
    ledger.derive()
    assert rows(ledger)["isp bill-2055-04"].status == "presumed_paid"

    statement(
        ledger,
        "april",
        CSV_IDENTIFIER,
        filler(D(2055, 4, 1), 3),
        (D(2055, 4, 23), ISP, Decimal("-69.12")),
        filler(D(2055, 4, 30), 4),
    )
    ledger.derive()

    found = rows(ledger)["isp bill-2055-04"]
    assert (found.status, found.paid, found.remaining) == ("paid", Decimal("69.12"), 0)
    assert applied(ledger) == [Applied("bill-2055-04", Decimal("69.12"), True)]
    # The decision stays on record and changes nothing.
    assert found.presumed_paid_decision_id == decided


def test_a_partial_payment_on_a_presumed_paid_bill_shows_what_is_left(
    ledger: Ledger,
) -> None:
    """A 33.56 line does not fit the 69.12 bill, so alone it is a question and the bill
    stays presumed paid; placed on it by the owner, the bill is partial with 35.56
    left."""
    one_bill(ledger)
    presume(ledger, "isp bill-2055-04")
    statement(
        ledger,
        "april",
        CSV_IDENTIFIER,
        filler(D(2055, 4, 1), 3),
        (D(2055, 4, 23), ISP, Decimal("-33.56")),
        filler(D(2055, 4, 30), 4),
    )
    ledger.derive()
    assert rows(ledger)["isp bill-2055-04"].status == "presumed_paid"
    assert applied(ledger) == [Applied("confirm", Decimal("33.56"), True)]

    decide(
        ledger,
        "payment_apply",
        target_ref=line_ref(ledger, D(2055, 4, 23)),
        obligation_ref=ref(ledger, "isp bill-2055-04"),
    )
    ledger.derive()

    found = rows(ledger)["isp bill-2055-04"]
    assert (found.status, found.paid, found.remaining) == (
        "partial",
        Decimal("33.56"),
        Decimal("35.56"),
    )


@pytest.mark.parametrize("presumed", [False, True], ids=["open", "presumed paid"])
def test_a_presumed_paid_bill_is_not_a_live_candidate_for_a_payment(
    ledger: Ledger, presumed: bool
) -> None:
    """Two bills of 69.12 and a payment of 69.12 that could pay either: open, the older
    is paid; presumed paid, the older is not live, so the newer is."""
    one_bill(ledger)
    bills(ledger, ONE_BILL, ("isp", "69.12", D(2055, 4, 10), D(2055, 5, 10)))
    ledger.derive()
    if presumed:
        presume(ledger, "isp bill-2055-04")
    statement(
        ledger,
        "april",
        CSV_IDENTIFIER,
        filler(D(2055, 4, 1), 3),
        (D(2055, 4, 27), ISP, Decimal("-69.12")),
        filler(D(2055, 4, 30), 4),
    )

    ledger.derive()

    statuses = {name: row.status for name, row in rows(ledger).items()}
    assert statuses == {
        "isp bill-2055-04": "presumed_paid" if presumed else "paid",
        "isp bill-2055-05": "paid" if presumed else "open",
    }
