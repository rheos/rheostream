"""Stage 15's application: what each payment paid (FR-21, FR-22; Key decisions 18 and
20; edge cases 14 to 17, 34 and 66; AC-25, AC-10's e-Transfer bill, AC-58's obligation
part).

Seam: ``derive.run`` through the derive ``ledger`` over synthetic chequing exports,
card statements in the Alder Neem layout with invented figures, typed-in bills and
arrangements, with the owner's decisions recorded through ``finance.decision.record``,
read back as ``obligation``, ``payment_application`` and ``event``. Every date, amount
and name is invented.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, NamedTuple

import pytest
from sqlalchemy import Row, select

from rheo_finance.derive import stage15_apply
from rheo_finance.derive.run import run
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.manual.accounts import ACCOUNTS_FILE
from rheo_finance.manual.arrangements import ARRANGEMENTS_FILE
from rheo_finance.manual.obligations import OBLIGATIONS_FILE
from rheo_finance.manual.payees import PAYEES_FILE
from rheo_finance.reports import ops as report_ops
from rheo_finance.reports.ops import StatusIn
from rheo_finance.reports.views import ARRANGEMENT_UNSETTLED, ARRANGEMENT_UNSETTLED_BY
from rheo_finance.settings import parse_settings
from rheo_finance.storage.schema import (
    event,
    obligation,
    payee,
    payment,
    payment_application,
)

from .support import CSV_IDENTIFIER, RULE_SETTINGS, D, Ledger, read_as_text
from .test_invoice_decisions import call, reverse
from .test_obligations import accounts, boxwood_bill, boxwood_with_arrears, facts
from .test_payments import bank, bank_line, notice, notice_key, set_cell
from .test_transfer_edges import PAYMENT_RECEIVED, file_bank, neem_export, on

ISP = "AUTO-PULL DEBIT SAMPLE ISP 99911"
LANDLORD = "CAROB E-SEND  receiver = SAMPLE LANDLORD"
CANCEL = "E-SEND OUT UNDONE BY SENDER"
NET = "CAROB E-SEND  receiver = SAMPLE NET"
MOBILE = "AUTO-PULL DEBIT SAMPLE MOBILE 99912"
POWER = "AUTO-PULL DEBIT SAMPLE POWER 99913"
SHOP = "SAMPLE SHOP SAMPLE CITY"
JUNE = D(2033, 6, 11)


class Applied(NamedTuple):
    """One application as a test reads it: the obligation's period, or the credit's
    reason, and the amount."""

    to: str
    amount: Decimal
    auto: bool


def applied(ledger: Ledger) -> list[Applied]:
    rows = ledger.conn.execute(
        select(
            obligation.c.period_label,
            payment_application.c.reason,
            payment_application.c.amount,
            payment_application.c.auto,
        ).select_from(
            payment_application.outerjoin(
                obligation, obligation.c.id == payment_application.c.obligation_id
            )
        )
    ).all()
    return sorted(
        Applied(row.period_label or row.reason, row.amount, row.auto) for row in rows
    )


def owed(ledger: Ledger) -> dict[str, Row[Any]]:
    return {
        row.period_label: row for row in ledger.conn.execute(select(obligation)).all()
    }


def figures(ledger: Ledger, label: str) -> tuple[object, ...]:
    """An obligation's paid, remaining, threshold remaining and status."""
    row = owed(ledger)[label]
    return (row.paid, row.remaining, row.threshold_remaining, row.status)


def bills(ledger: Ledger, *rows: tuple[str, str, dt.date, dt.date]) -> None:
    """Typed-in bills as ``(payee, amount due, issued on, due on)``."""
    ledger.manual(
        OBLIGATIONS_FILE,
        *(
            f"{who},bill,,,{amount},,{due},,{issued}"
            for who, amount, issued, due in rows
        ),
    )


def with_settings(ledger: Ledger, text: str) -> None:
    ledger.ctx = dataclasses.replace(
        ledger.ctx,
        settings=parse_settings(
            f'[finance.paths]\narchive = "{ledger.archive}"\n'
            f'downloads = "{ledger.archive.parent / "downloads-unset"}"\n'
            + RULE_SETTINGS
            + text
        ),
    )


def line_ref(ledger: Ledger, day: dt.date) -> str:
    return f"finance.txn:{bank_line(ledger, day)}"


def obligation_ref(ledger: Ledger, label: str) -> str:
    return f"finance.obligation:{owed(ledger)[label].id}"


def decide(ledger: Ledger, kind: str, **fields: object) -> uuid.UUID:
    out = call(ledger, "finance.decision.record", {"kind": kind, **fields})
    decision_id = getattr(out, "decision_id", None)
    assert isinstance(decision_id, uuid.UUID)
    return decision_id


@pytest.fixture
def paying(own: Ledger, notice_parser: None) -> Ledger:
    """The ``own`` accounts with the Neem named by its payment wording, a bill vendor's
    account whose bills chain, and vendors and a landlord whose bills do not."""
    with (own.data / "manual" / ACCOUNTS_FILE).open("a", encoding="utf-8") as out:
        out.write(
            "mobile,Mobile,Sample Mobile,bill_vendor,CAD,monthly,,,,,,"
            "SAMPLE MOBILE,,,\n"
        )
    own.add_identifier("mobile", "999000123")
    set_cell(own, "neem", "payee_patterns", "ALDER NEEM")
    own.manual(
        PAYEES_FILE,
        "isp,Our net,vendor,SAMPLE ISP,,",
        "landlord,Our rent,person,SAMPLE LANDLORD,,",
        "net,Our line,vendor,SAMPLE NET,,",
    )
    own.ctx = dataclasses.replace(own.ctx, snapshot_provider=txn_snapshot)
    return own


# --- bills and every other kind (AC-25) -----------------------------------------------


@pytest.mark.parametrize("amount", ["206.40", "206.39", "206.41"])
def test_one_open_obligation_and_an_equal_payment_applies(
    paying: Ledger, amount: str
) -> None:
    bills(paying, ("isp", "206.40", D(2033, 6, 1), D(2033, 6, 21)))
    bank(paying, (JUNE, ISP, -Decimal(amount)))

    paying.derive()

    assert applied(paying) == [Applied("bill-2033-06", Decimal(amount), True)]
    assert figures(paying, "bill-2033-06") == (Decimal(amount), 0, 0, "paid")


def test_two_open_obligations_and_a_payment_equal_to_one_applies_to_that_one(
    paying: Ledger,
) -> None:
    bills(
        paying,
        ("isp", "206.40", D(2033, 6, 1), D(2033, 6, 21)),
        ("isp", "154.80", D(2033, 7, 1), D(2033, 7, 21)),
    )
    bank(paying, (D(2033, 7, 11), ISP, Decimal("-154.80")))

    paying.derive()

    assert applied(paying) == [Applied("bill-2033-07", Decimal("154.80"), True)]
    assert figures(paying, "bill-2033-06")[3] == "open"
    assert figures(paying, "bill-2033-07")[3] == "paid"


def test_two_open_obligations_and_a_payment_matching_neither_applies_nothing(
    paying: Ledger,
) -> None:
    """A confirm item: the credit waits for the owner's answer."""
    bills(
        paying,
        ("isp", "206.40", D(2033, 6, 1), D(2033, 6, 21)),
        ("isp", "154.80", D(2033, 7, 1), D(2033, 7, 21)),
    )
    bank(paying, (D(2033, 7, 11), ISP, Decimal("-180.60")))

    paying.derive()

    assert applied(paying) == [Applied("confirm", Decimal("180.60"), True)]
    assert {row.paid for row in owed(paying).values()} == {0}


def test_a_partial_payment_toward_an_arrangement_applies_oldest_first(
    paying: Ledger,
) -> None:
    paying.manual(
        ARRANGEMENTS_FILE,
        "plan,isp,2033-05-01,1,51.60,2033-06-01,",
        "plan,isp,2033-05-01,2,61.92,2033-07-01,",
    )
    bank(paying, (JUNE, ISP, Decimal("-82.56")))

    paying.derive()

    assert applied(paying) == [
        Applied("inst-plan-1", Decimal("51.60"), True),
        Applied("inst-plan-2", Decimal("30.96"), True),
    ]
    assert figures(paying, "inst-plan-1") == (Decimal("51.60"), 0, 0, "paid")
    assert figures(paying, "inst-plan-2") == (
        Decimal("30.96"),
        Decimal("30.96"),
        Decimal("30.96"),
        "partial",
    )


def test_an_instalment_overpaid_leaves_the_rest_as_excess(paying: Ledger) -> None:
    paying.manual(ARRANGEMENTS_FILE, "plan,isp,2033-05-01,1,51.60,2033-06-01,")
    bank(paying, (JUNE, ISP, Decimal("-82.56")))

    paying.derive()

    assert applied(paying) == [
        Applied("excess", Decimal("30.96"), True),
        Applied("inst-plan-1", Decimal("51.60"), True),
    ]


@pytest.mark.parametrize(
    ("before", "applies"),
    [(3, True), (10, True), (11, False), (20, False)],
    ids=["3 days", "10 days", "11 days", "20 days"],
)
def test_a_payment_before_its_bill_applies_when_the_bill_loads_within_early_days(
    paying: Ledger, before: int, applies: bool
) -> None:
    issued = D(2033, 6, 14)
    paid_on = issued - dt.timedelta(days=before)
    bank(paying, (paid_on, ISP, Decimal("-206.40")))
    paying.derive()
    (credit,) = paying.conn.execute(select(payment_application)).all()
    assert (credit.reason, credit.obligation_id) == ("no_target", None)
    assert credit.bound_on == paid_on + dt.timedelta(days=10)

    bills(paying, ("isp", "206.40", issued, D(2033, 7, 3)))
    paying.derive()

    if applies:
        assert applied(paying) == [Applied("bill-2033-07", Decimal("206.40"), True)]
        assert figures(paying, "bill-2033-07")[3] == "paid"
    else:
        assert applied(paying) == [Applied("no_target", Decimal("206.40"), True)]
        assert figures(paying, "bill-2033-07") == (
            0,
            Decimal("206.40"),
            Decimal("206.40"),
            "open",
        )


def test_a_payment_years_before_the_only_open_bill_stays_a_credit(
    paying: Ledger,
) -> None:
    """It never rolls forward, however many derives or later bills follow."""
    bank(paying, (D(2030, 6, 11), ISP, Decimal("-206.40")))

    for newer in ((), (("isp", "206.40", D(2033, 7, 1), D(2033, 7, 21)),)):
        bills(paying, ("isp", "206.40", D(2033, 6, 1), D(2033, 6, 21)), *newer)
        paying.derive()
        paying.derive()

        assert applied(paying) == [Applied("no_target", Decimal("206.40"), True)]
        assert all(row.remaining == Decimal("206.40") for row in owed(paying).values())
    bound = paying.conn.scalar(select(payment_application.c.bound_on))
    assert bound == D(2030, 6, 21)


@pytest.mark.parametrize(
    ("day", "amount", "to"),
    [
        (D(2028, 3, 16), "82.56", "bill-2028-03"),
        (D(2028, 2, 20), "82.56", "bill-2028-03"),
        (D(2031, 4, 16), "82.56", "bill-2028-03"),
        (D(2031, 4, 26), "98.04", "bill-2031-05"),
        (D(2028, 3, 16), "72.59", "confirm"),
        (D(2028, 2, 19), "82.56", "no_target"),
    ],
    ids=[
        "inside",
        "early days before",
        "before the current bill is a candidate",
        "the current bill",
        "does not fit",
        "before the window",
    ],
)
def test_a_payment_inside_a_superseded_bills_window_lands_on_that_bill(
    paying: Ledger, day: dt.date, amount: str, to: str
) -> None:
    """A bill vendor's bills chain, so the old one is superseded and the current one
    owes what is left. A 2028 payment pays the 2028 bill or nothing."""
    bills(
        paying,
        ("mobile", "82.56", D(2028, 3, 1), D(2028, 3, 21)),
        ("mobile", "98.04", D(2031, 5, 1), D(2031, 5, 21)),
    )
    bank(paying, (day, MOBILE, -Decimal(amount)))

    paying.derive()

    assert applied(paying) == [Applied(to, Decimal(amount), True)]
    old, current = owed(paying)["bill-2028-03"], owed(paying)["bill-2031-05"]
    assert old.status == "superseded"
    assert old.paid == (Decimal(amount) if to == "bill-2028-03" else 0)
    if to != "bill-2031-05":
        assert (current.remaining, current.status) == (Decimal("98.04"), "open")


@pytest.mark.parametrize(
    ("day", "to"),
    [(D(2031, 5, 10), "bill-2028-03"), (D(2031, 5, 11), "no_target")],
    ids=["inside", "at the end"],
)
def test_an_old_bills_window_ends_at_the_next_bill_plus_early_days(
    paying: Ledger, day: dt.date, to: str
) -> None:
    """With the current bill paid there is no live candidate, so the window decides;
    past it, the payment found only bills already paid: no target."""
    bills(
        paying,
        ("mobile", "82.56", D(2028, 3, 1), D(2028, 3, 21)),
        ("mobile", "98.04", D(2031, 5, 1), D(2031, 5, 21)),
    )
    bank(
        paying,
        (D(2031, 5, 3), MOBILE, Decimal("-98.04")),
        (day, MOBILE, Decimal("-82.56")),
    )

    paying.derive()

    assert applied(paying) == sorted(
        [
            Applied("bill-2031-05", Decimal("98.04"), True),
            Applied(to, Decimal("82.56"), True),
        ]
    )


@pytest.mark.parametrize(
    ("issued", "paid_before", "to"),
    [
        (D(2033, 6, 1), False, "confirm"),
        (D(2033, 7, 1), False, "no_target"),
        (D(2033, 6, 1), True, "no_target"),
    ],
    ids=["with a bill to pay", "with none issued", "with the bill paid already"],
)
def test_a_payment_in_another_currency_pays_nothing(
    paying: Ledger, issued: dt.date, paid_before: bool, to: str
) -> None:
    """Every obligation is in CAD: with one to pay, the owner says what it was; with
    nothing to pay there is nothing to ask."""
    bills(paying, ("isp", "219.29", issued, issued + dt.timedelta(days=19)))
    if paid_before:
        bank(paying, (issued, ISP, Decimal("-219.29")))
    notice(paying, JUNE, currency="USD")

    paying.derive()

    assert Applied(to, Decimal("219.29"), True) in applied(paying)
    assert {row.paid for row in owed(paying).values()} == {
        Decimal("219.29") if paid_before else 0
    }


def twelve_instalments(ledger: Ledger, payee_slug: str = "isp") -> None:
    """Twelve instalments of 51.60, agreed on one day, due monthly."""
    ledger.manual(
        ARRANGEMENTS_FILE,
        *(
            f"plan,{payee_slug},2033-05-01,{n},51.60,"
            f"{D(2033 + (4 + n) // 12, (4 + n) % 12 + 1, 1)},"
            for n in range(1, 13)
        ),
    )


@pytest.mark.parametrize(
    ("payments", "rows"),
    [
        (
            ("51.60", "51.60"),
            [
                Applied("inst-plan-1", Decimal("51.60"), True),
                Applied("inst-plan-2", Decimal("51.60"), True),
            ],
        ),
        (
            ("77.40",),
            [
                Applied("inst-plan-1", Decimal("51.60"), True),
                Applied("inst-plan-2", Decimal("25.80"), True),
            ],
        ),
    ],
    ids=["two on time", "a partial"],
)
def test_instalments_are_paid_in_their_own_order_not_their_labels(
    paying: Ledger, payments: tuple[str, ...], rows: list[Applied]
) -> None:
    """inst-plan-10 sorts before inst-plan-2 as text; it is paid tenth."""
    twelve_instalments(paying)
    bank(
        paying,
        *((D(2033, 6 + n, 1), ISP, -Decimal(out)) for n, out in enumerate(payments)),
    )

    paying.derive()

    assert applied(paying) == rows


def test_instalments_due_on_one_day_are_paid_in_their_number_order(
    paying: Ledger,
) -> None:
    """Ten instalments agreed and due on the same days: only the label's number says
    which is older, and as text inst-plan-10 would come before inst-plan-2."""
    paying.manual(
        ARRANGEMENTS_FILE,
        *(f"plan,isp,2033-05-01,{n},51.60,2033-06-01," for n in range(1, 11)),
    )
    bank(paying, (JUNE, ISP, Decimal("-77.40")))

    paying.derive()

    assert applied(paying) == [
        Applied("inst-plan-1", Decimal("51.60"), True),
        Applied("inst-plan-2", Decimal("25.80"), True),
    ]


def test_two_bills_issued_and_due_on_the_same_days_that_both_fit_are_a_question(
    paying: Ledger,
) -> None:
    paying.manual(
        OBLIGATIONS_FILE,
        "isp,bill,bill-2033-06,,206.40,,2033-06-21,,2033-06-01",
        "isp,bill,bill-2033-06-2,,206.40,,2033-06-21,,2033-06-01",
    )
    bank(paying, (JUNE, ISP, Decimal("-206.40")))

    paying.derive()

    assert applied(paying) == [Applied("confirm", Decimal("206.40"), True)]


def test_of_two_bills_that_fit_the_older_is_paid(paying: Ledger) -> None:
    bills(
        paying,
        ("isp", "206.40", D(2033, 6, 1), D(2033, 6, 21)),
        ("isp", "206.40", D(2033, 7, 1), D(2033, 7, 21)),
    )
    bank(paying, (D(2033, 7, 11), ISP, Decimal("-206.40")))

    paying.derive()

    assert applied(paying) == [Applied("bill-2033-06", Decimal("206.40"), True)]


def test_a_payment_toward_a_bill_and_an_instalment_both_live_is_a_question(
    paying: Ledger,
) -> None:
    """Rule (b) fills partial kinds only: with a bill live too, it is not the rule's
    to say where a payment that fits neither went."""
    paying.manual(ARRANGEMENTS_FILE, "plan,isp,2033-05-01,1,51.60,2033-06-01,")
    bills(paying, ("isp", "206.40", D(2033, 6, 1), D(2033, 6, 21)))
    bank(paying, (JUNE, ISP, Decimal("-30.96")))

    paying.derive()

    assert applied(paying) == [Applied("confirm", Decimal("30.96"), True)]


# --- reversed payments (edge cases 17 and 34; AC-58) ----------------------------------


def test_a_reversed_payment_never_reduces_the_bill_and_reopens_one_it_closed(
    paying: Ledger,
) -> None:
    bills(paying, ("landlord", "206.40", D(2033, 6, 1), D(2033, 6, 21)))
    bank(paying, (JUNE, LANDLORD, Decimal("-206.40")))
    paying.derive()
    assert figures(paying, "bill-2033-06")[3] == "paid"

    bank(
        paying,
        (JUNE, LANDLORD, Decimal("-206.40")),
        (D(2033, 6, 13), CANCEL, Decimal("206.40")),
    )
    paying.derive()

    assert paying.conn.scalar(select(payment.c.state)) == "reversed"
    assert applied(paying) == []
    assert figures(paying, "bill-2033-06") == (
        0,
        Decimal("206.40"),
        Decimal("206.40"),
        "open",
    )


def test_a_cancelled_e_transfer_and_its_resend_pay_the_rent_once(
    paying: Ledger,
) -> None:
    """The shape of AC-58 on invented dates and amounts: a debit, its cancellation a
    few days on, and the same payment sent again that day."""
    bills(paying, ("landlord", "877.20", D(2033, 9, 1), D(2033, 9, 6)))
    bank(
        paying,
        (D(2033, 9, 4), LANDLORD, Decimal("-877.20")),
        (D(2033, 9, 10), CANCEL, Decimal("877.20")),
        (D(2033, 9, 10), LANDLORD, Decimal("-877.20")),
    )

    paying.derive()

    assert applied(paying) == [Applied("bill-2033-09", Decimal("877.20"), True)]
    assert figures(paying, "bill-2033-09")[3] == "paid"
    applied_from = paying.conn.scalar(
        select(payment.c.paid_on).join(
            payment_application, payment_application.c.payment_id == payment.c.id
        )
    )
    assert applied_from == D(2033, 9, 10)


def test_a_typed_in_bill_is_closed_by_a_later_e_transfer_of_its_amount(
    paying: Ledger,
) -> None:
    bills(paying, ("net", "55.86", D(2033, 6, 1), D(2033, 6, 21)))
    bank(paying, (D(2033, 6, 16), NET, Decimal("-55.86")))

    paying.derive()

    assert figures(paying, "bill-2033-06") == (Decimal("55.86"), 0, 0, "paid")


# --- the owner's decisions ------------------------------------------------------------


def test_undo_restores_the_remaining_and_reversing_the_undo_applies_again(
    paying: Ledger,
) -> None:
    bills(paying, ("isp", "206.40", D(2033, 6, 1), D(2033, 6, 21)))
    bank(paying, (JUNE, ISP, Decimal("-206.40")))
    paying.derive()
    before = figures(paying, "bill-2033-06")

    undo = decide(
        paying,
        "payment_unapply",
        target_ref=line_ref(paying, JUNE),
        obligation_ref=obligation_ref(paying, "bill-2033-06"),
    )
    paying.derive()

    assert figures(paying, "bill-2033-06") == (
        0,
        Decimal("206.40"),
        Decimal("206.40"),
        "open",
    )
    assert applied(paying) == [Applied("payment_unapply", Decimal("206.40"), False)]
    basis = paying.conn.scalar(select(payment_application.c.basis_ref))
    assert basis == f"finance.decision:{undo}"

    reverse(paying, undo)
    paying.derive()
    assert figures(paying, "bill-2033-06") == before


@pytest.mark.parametrize(
    ("amount", "rows"),
    [
        (None, [Applied("bill-2033-06", Decimal("180.60"), False)]),
        (
            "103.20",
            [
                Applied("bill-2033-06", Decimal("103.20"), False),
                Applied("excess", Decimal("77.40"), False),
            ],
        ),
    ],
    ids=["whole", "an amount"],
)
def test_payment_apply_places_a_payment_the_rules_left_to_the_owner(
    paying: Ledger, amount: str | None, rows: list[Applied]
) -> None:
    bills(
        paying,
        ("isp", "206.40", D(2033, 6, 1), D(2033, 6, 21)),
        ("isp", "154.80", D(2033, 7, 1), D(2033, 7, 21)),
    )
    bank(paying, (D(2033, 7, 11), ISP, Decimal("-180.60")))
    paying.derive()
    assert [each.to for each in applied(paying)] == ["confirm"]

    fields = {} if amount is None else {"amount": amount}
    decide(
        paying,
        "payment_apply",
        target_ref=line_ref(paying, D(2033, 7, 11)),
        obligation_ref=obligation_ref(paying, "bill-2033-06"),
        **fields,
    )
    paying.derive()

    assert applied(paying) == rows
    assert figures(paying, "bill-2033-06")[3] == "partial"
    assert figures(paying, "bill-2033-07")[3] == "open"


def test_a_notice_names_its_payment_in_a_decision(paying: Ledger) -> None:
    bills(paying, ("isp", "219.29", D(2033, 6, 1), D(2033, 6, 21)))
    filed = notice(paying, JUNE, printed="Sample ISP")
    paying.derive()
    assert figures(paying, "bill-2033-06")[3] == "paid"

    decide(
        paying,
        "payment_unapply",
        target_ref=f"finance.payment_notice:{notice_key(paying, filed)}",
    )
    paying.derive()

    assert figures(paying, "bill-2033-06")[3] == "open"
    assert applied(paying) == [Applied("payment_unapply", Decimal("219.29"), False)]


def test_an_unapply_naming_an_obligation_bars_an_older_apply_to_it(
    paying: Ledger,
) -> None:
    bills(
        paying,
        ("isp", "206.40", D(2033, 6, 1), D(2033, 6, 21)),
        ("isp", "154.80", D(2033, 7, 1), D(2033, 7, 21)),
    )
    bank(paying, (D(2033, 7, 11), ISP, Decimal("-180.60")))
    paying.derive()
    named = {
        "target_ref": line_ref(paying, D(2033, 7, 11)),
        "obligation_ref": obligation_ref(paying, "bill-2033-06"),
    }
    decide(paying, "payment_apply", **named)
    decide(paying, "payment_unapply", **named)

    paying.derive()

    assert applied(paying) == [Applied("payment_unapply", Decimal("180.60"), False)]
    assert {row.paid for row in owed(paying).values()} == {0}


def test_a_decision_applies_no_payment_in_another_currency(paying: Ledger) -> None:
    bills(paying, ("isp", "219.29", D(2033, 6, 1), D(2033, 6, 21)))
    filed = notice(paying, JUNE, currency="USD")
    paying.derive()

    decide(
        paying,
        "payment_apply",
        target_ref=f"finance.payment_notice:{notice_key(paying, filed)}",
        obligation_ref=obligation_ref(paying, "bill-2033-06"),
    )
    paying.derive()

    assert applied(paying) == [Applied("confirm", Decimal("219.29"), True)]
    assert figures(paying, "bill-2033-06")[3] == "open"


def test_an_unapply_clears_a_payment_in_another_currency(paying: Ledger) -> None:
    """The confirm credit is a question the owner can answer: the unapply takes the
    payment off the rules, although an apply on it is refused."""
    bills(paying, ("isp", "219.29", D(2033, 6, 1), D(2033, 6, 21)))
    filed = notice(paying, JUNE, currency="USD")
    paying.derive()
    assert applied(paying) == [Applied("confirm", Decimal("219.29"), True)]

    decide(
        paying,
        "payment_unapply",
        target_ref=f"finance.payment_notice:{notice_key(paying, filed)}",
    )
    paying.derive()

    assert applied(paying) == [Applied("payment_unapply", Decimal("219.29"), False)]
    assert figures(paying, "bill-2033-06")[3] == "open"


# --- events ---------------------------------------------------------------------------


def test_an_automatic_application_registers_payment_detected_without_an_amount(
    paying: Ledger,
) -> None:
    bills(
        paying,
        ("isp", "206.40", D(2033, 6, 1), D(2033, 6, 21)),
        ("isp", "154.80", D(2033, 7, 1), D(2033, 7, 21)),
    )
    bank(
        paying,
        (JUNE, ISP, Decimal("-206.40")),
        (D(2033, 7, 11), ISP, Decimal("-180.60")),
    )
    paying.derive()

    (row,) = paying.conn.execute(
        select(event).where(event.c.type == "finance.payment.detected")
    ).all()
    isp = paying.conn.scalar(select(payee.c.id).where(payee.c.slug == "isp"))
    assert row.data == {
        "payee_ref": f"finance.payee:{isp}",
        "obligation_ref": obligation_ref(paying, "bill-2033-06"),
    }
    assert row.occurred_on == JUNE

    decide(
        paying,
        "payment_apply",
        target_ref=line_ref(paying, D(2033, 7, 11)),
        obligation_ref=obligation_ref(paying, "bill-2033-07"),
    )
    paying.derive()

    # A decided application is the owner's own: no event for it.
    found = paying.conn.execute(
        select(event.c.id).where(event.c.type == "finance.payment.detected")
    ).all()
    assert len(found) == 1


def test_a_derive_and_a_rebuild_apply_the_same(paying: Ledger) -> None:
    bills(
        paying,
        ("isp", "206.40", D(2033, 6, 1), D(2033, 6, 21)),
        ("mobile", "82.56", D(2028, 3, 1), D(2028, 3, 21)),
        ("mobile", "98.04", D(2031, 5, 1), D(2031, 5, 21)),
    )
    bank(
        paying,
        (JUNE, ISP, Decimal("-206.40")),
        (D(2028, 3, 16), MOBILE, Decimal("-82.56")),
        (D(2030, 1, 6), ISP, Decimal("-20.64")),
    )
    paying.derive()

    def state() -> tuple[list[str], list[str]]:
        return (
            sorted(
                repr(tuple(row))
                for row in paying.conn.execute(select(payment_application))
            ),
            sorted(repr(tuple(row)) for row in paying.conn.execute(select(obligation))),
        )

    derived = state()
    paying.derive()
    assert state() == derived
    run(paying.uow, paying.ctx, extract_pdf=read_as_text, reparse=True)
    assert state() == derived


# --- cards, with no early_days slack --------------------------------------------------

STATEMENT_N = D(2033, 3, 1)
STATEMENT_N1 = D(2033, 3, 31)
CardRow = tuple[dt.date, str, Decimal]


def neem_statement(
    start: dt.date,
    end: dt.date,
    previous: Decimal,
    rows: tuple[CardRow, ...],
    minimum: Decimal,
    due: dt.date,
) -> str:
    """A statement in the Alder Neem layout with these figures; the summary ties out
    to the lines, which are signed from the owner's side."""
    purchases = sum((-amount for _, _, amount in rows if amount < 0), Decimal(0))
    credits = sum((amount for _, _, amount in rows if amount > 0), Decimal(0))
    balance = previous + purchases - credits

    def cad(value: Decimal) -> str:
        return f"${value:,.2f}"

    def day(value: dt.date) -> str:
        return f"{value:%b}".upper() + f" {value.day:02d}"

    return "\n".join(
        [
            f"Neem cycle: {start:%B} {start.day} to {end:%B} {end.day},"
            f" {end.year}   Neem card no XXXX-9991",
            "",
            "Neem overview   What to pay",
            f" Neem cycle balance   {cad(balance)}",
            f" Neem least due   {cad(minimum)}",
            f" Neem pay by date   {due:%B} {due.day}, {due.year}",
            f" Neem opening figure   {cad(previous)}",
            f" Neem credits in   -   {cad(credits)}",
            f" Neem spending   +   {cad(purchases)}",
            " Neem cash taken   +   $0.00",
            " Neem interest   +   $0.00",
            " Neem charges   +   $0.00",
            f"Neem cycle balance   {cad(balance)}",
            "",
            *(
                f"{day(when)}   {day(when)} {words}   "
                f"{'- ' if amount > 0 else ''}{cad(abs(amount))}"
                for when, words, amount in rows
            ),
            "",
        ]
    )


def statement_n(ledger: Ledger, *payments: CardRow) -> None:
    """Statement N, dated 1 March: 825.60 owed, a 25.80 minimum due 26 March."""
    ledger.file(
        "alder-neem/2033-03-01.pdf",
        neem_statement(
            D(2033, 2, 3),
            STATEMENT_N,
            Decimal(0),
            ((D(2033, 2, 11), SHOP, Decimal("-825.60")), *payments),
            Decimal("25.80"),
            D(2033, 3, 26),
        ),
    )


def statement_n1(ledger: Ledger, *payments: CardRow) -> None:
    """Statement N+1, dated 31 March, carrying N's balance and the payments made."""
    ledger.file(
        "alder-neem/2033-03-31.pdf",
        neem_statement(
            D(2033, 3, 3),
            STATEMENT_N1,
            Decimal("825.60"),
            ((D(2033, 3, 13), SHOP, Decimal("-103.20")), *payments),
            Decimal("30.96"),
            D(2033, 4, 26),
        ),
    )


def card_export(ledger: Ledger, *rows: CardRow) -> None:
    """The card's transactions as downloaded before the next statement."""
    ledger.file("alder-neem/2033-03-29.csv", neem_export(*rows))


def paid_card(day: dt.date, amount: str) -> CardRow:
    return (day, PAYMENT_RECEIVED, Decimal(amount))


N, N1 = "card-2033-03-01", "card-2033-03-31"


def test_a_minimum_paid_on_its_due_date_stays_on_its_statement_when_the_next_loads(
    paying: Ledger,
) -> None:
    """Edge case 66 and AC-25's card case: the payment is dated before N+1, so N+1's
    balance already nets it, and N+1's minimum stays wholly unpaid."""
    minimum = paid_card(D(2033, 3, 26), "25.80")
    statement_n(paying)
    card_export(paying, minimum)
    paying.derive()
    before = figures(paying, N)
    assert before == (Decimal("25.80"), Decimal("799.80"), 0, "partial")

    statement_n1(paying, minimum)
    paying.derive()

    assert len(paying.conn.execute(select(payment)).all()) == 1
    assert applied(paying) == [Applied(N, Decimal("25.80"), True)]
    assert figures(paying, N) == (Decimal("25.80"), Decimal("799.80"), 0, "superseded")
    assert figures(paying, N1) == (0, Decimal("903.00"), Decimal("30.96"), "open")
    assert owed(paying)[N1].payments_mismatch is None


@pytest.mark.parametrize(
    ("day", "to"),
    [(D(2033, 3, 30), N), (STATEMENT_N1, N1), (D(2033, 4, 4), N1)],
    ids=["the day before N+1", "N+1's date", "after N+1"],
)
def test_only_a_payment_dated_on_or_after_the_next_statement_pays_its_minimum(
    paying: Ledger, day: dt.date, to: str
) -> None:
    minimum = paid_card(D(2033, 3, 26), "25.80")
    second = paid_card(day, "30.96")
    statement_n(paying)
    statement_n1(paying, minimum, *((second,) if day <= STATEMENT_N1 else ()))
    if day > STATEMENT_N1:
        paying.file("alder-neem/2033-04-06.csv", neem_export(second))

    paying.derive()

    assert Applied(to, Decimal("30.96"), True) in applied(paying)
    threshold = figures(paying, N1)[2]
    assert threshold == (0 if to == N1 else Decimal("30.96"))


def test_a_card_payment_before_the_earliest_statement_has_no_target(
    paying: Ledger,
) -> None:
    """Printed on N, dated before N: it paid a statement that is not loaded."""
    statement_n(paying, paid_card(D(2033, 2, 21), "41.28"))
    paying.derive()

    (credit,) = paying.conn.execute(select(payment_application)).all()
    assert (credit.reason, credit.bound_on) == ("no_target", D(2033, 2, 21))
    assert figures(paying, N) == (0, Decimal("784.32"), Decimal("25.80"), "open")


@pytest.mark.parametrize(
    ("amount", "rows", "after"),
    [
        (
            "25.80",
            [Applied(N, Decimal("25.80"), True)],
            (Decimal("25.80"), Decimal("799.80"), 0, "partial"),
        ),
        (
            "877.20",
            [
                Applied(N, Decimal("825.60"), True),
                Applied("excess", Decimal("51.60"), True),
            ],
            (Decimal("825.60"), 0, 0, "paid"),
        ),
    ],
    ids=["the minimum", "more than the balance"],
)
def test_a_card_payment_fills_the_minimum_first_and_its_excess_stays_a_credit(
    paying: Ledger, amount: str, rows: list[Applied], after: tuple[Any, ...]
) -> None:
    """The next statement receives nothing of it: its balance already counts it."""
    paid = paid_card(D(2033, 3, 19), amount)
    statement_n(paying)
    statement_n1(paying, paid)

    paying.derive()

    assert applied(paying) == rows
    assert figures(paying, N) == (*after[:3], "superseded")
    assert owed(paying)[N1].paid == 0


@pytest.mark.parametrize(
    ("partial_kinds", "rows", "status"),
    [
        (None, [Applied(N, Decimal("103.20"), True)], "partial"),
        (
            '["arrangement_instalment"]',
            [Applied("confirm", Decimal("103.20"), True)],
            "open",
        ),
    ],
    ids=["default", "cards removed"],
)
def test_a_partial_card_payment_applies_only_while_cards_are_partial_kinds(
    paying: Ledger, partial_kinds: str | None, rows: list[Applied], status: str
) -> None:
    if partial_kinds is not None:
        with_settings(
            paying, f"[finance.payments]\npartial_auto_kinds = {partial_kinds}\n"
        )
    statement_n(paying)
    card_export(paying, paid_card(D(2033, 3, 11), "103.20"))

    paying.derive()

    assert applied(paying) == rows
    assert figures(paying, N)[3] == status


def test_a_payment_the_next_statement_does_not_print_is_a_payments_mismatch(
    paying: Ledger,
) -> None:
    """FR-21: an email says 41.28 reached the card on 21 March; N+1 prints only the
    25.80 line, so applied 67.08 and printed 25.80 differ by 41.28."""
    minimum = paid_card(D(2033, 3, 26), "25.80")
    statement_n(paying)
    statement_n1(paying, minimum)
    notice(paying, D(2033, 3, 21), amount=Decimal("41.28"), printed="Alder Neem")

    paying.derive()

    assert owed(paying)[N1].payments_mismatch == Decimal("41.28")
    assert owed(paying)[N].payments_mismatch is None


@pytest.mark.parametrize(
    ("paid_before", "to"),
    [(None, "confirm"), ("825.60", "no_target"), ("825.59", "no_target")],
    ids=[
        "with the statement to pay",
        "with it paid already",
        "with it paid within the tolerance",
    ],
)
def test_a_card_payment_in_another_currency_applies_nothing(
    paying: Ledger, paid_before: str | None, to: str
) -> None:
    """A cent short of the balance is paid, as for every other kind: nothing is owed
    for the other currency's payment to wait on."""
    statement_n(paying)
    if paid_before is not None:
        card_export(paying, paid_card(D(2033, 3, 5), paid_before))
    notice(
        paying,
        D(2033, 3, 11),
        amount=Decimal("25.80"),
        currency="USD",
        printed="Alder Neem",
    )

    paying.derive()

    assert Applied(to, Decimal("25.80"), True) in applied(paying)
    assert figures(paying, N)[0] == Decimal(paid_before or 0)


def test_a_payment_left_to_the_owner_is_not_counted_as_applied_for_fr_21(
    paying: Ledger,
) -> None:
    """With cards out of the partial set, N+1's 103.20 line is a confirm credit: it
    applied nothing, so the statement's 103.20 is a mismatch until the owner says."""
    with_settings(
        paying, '[finance.payments]\npartial_auto_kinds = ["arrangement_instalment"]\n'
    )
    statement_n(paying)
    statement_n1(paying, paid_card(D(2033, 3, 11), "103.20"))

    paying.derive()

    assert applied(paying) == [Applied("confirm", Decimal("103.20"), True)]
    assert owed(paying)[N1].payments_mismatch == Decimal("-103.20")


# --- arrangements: a finished one stops setting the bills -----------------------------


LATER_ON = D(2029, 5, 15)


def power(
    ledger: Ledger,
    *payments: tuple[dt.date, str],
    later_bill: bool = False,
    later_on: dt.date = LATER_ON,
) -> None:
    """Two Boxwood bills, the older replaced by a two-instalment arrangement and the
    newer printing an arrears line, the chequing payments of the instalments, and with
    ``later_bill`` a third bill, issued ``later_on`` (15 May) and due 16 June, printing
    arrears too."""
    accounts(
        ledger,
        ("chequing", "Alder", "chequing", CSV_IDENTIFIER, ""),
        ("utility", "Boxwood", "bill_vendor", "9990001234", ""),
    )
    ledger.manual(PAYEES_FILE, "boxwood,Our power,vendor,SAMPLE POWER,,")
    ledger.manual(
        ARRANGEMENTS_FILE,
        "plan,boxwood,2029-01-21,1,38.22,2029-04-01,bill-2029-02",
        "plan,boxwood,2029-01-21,2,61.92,2029-05-01,bill-2029-02",
    )
    ledger.file("boxwood/a.pdf", boxwood_bill("January 26, 2029", "February 16, 2029"))
    ledger.file(
        "boxwood/b.pdf",
        boxwood_with_arrears(boxwood_bill("February 25, 2029", "March 18, 2029")),
    )
    if later_bill:
        ledger.file(
            "boxwood/c.pdf",
            boxwood_with_arrears(
                boxwood_bill(
                    f"{later_on:%B} {later_on.day}, {later_on.year}", "June 16, 2029"
                )
            ),
        )
    file_bank(
        ledger,
        *on(CSV_IDENTIFIER, *((day, POWER, -Decimal(out)) for day, out in payments)),
    )


PAID_UP = ((D(2029, 3, 26), "38.22"), (D(2029, 4, 26), "61.92"))


def test_a_bill_issued_while_the_arrangement_ran_keeps_owing_its_current_charges(
    ledger: Ledger,
) -> None:
    """Its arrears were what the instalments paid: finishing them does not bring the
    arrears back on that bill."""
    power(ledger, *PAID_UP)

    ledger.derive()

    found = owed(ledger)
    assert {label: found[label].status for label in found} == {
        "bill-2029-02": "superseded",
        "bill-2029-03": "open",
        "inst-plan-1": "paid",
        "inst-plan-2": "paid",
    }
    assert found["bill-2029-02"].arrangement_ref == "plan"
    assert found["bill-2029-03"].amount_due == Decimal("333.84")


def test_a_bill_issued_after_the_last_instalment_owes_its_arrears_again(
    ledger: Ledger,
) -> None:
    power(ledger, *PAID_UP, later_bill=True)

    ledger.derive()

    found = owed(ledger)
    assert {label: found[label].status for label in found} == {
        "bill-2029-02": "superseded",
        "bill-2029-03": "superseded",
        "bill-2029-06": "open",
        "arrears-2029-05": "open",
        "inst-plan-1": "paid",
        "inst-plan-2": "paid",
    }
    assert found["bill-2029-06"].amount_due == Decimal("299.22")
    assert found["arrears-2029-05"].amount_due == Decimal("195.44")


@pytest.mark.parametrize(
    ("issued", "governed"),
    [(D(2029, 5, 6), True), (D(2029, 5, 7), False)],
    ids=["early_days after the last payment", "a day later"],
)
def test_a_bill_cut_within_early_days_of_the_last_instalment_stays_governed(
    ledger: Ledger, issued: dt.date, governed: bool
) -> None:
    """The last instalment was paid 25 April; a bill cut before that payment posts
    still prints the arrears the instalments paid, so the slack keeps it governed, to
    the day."""
    power(ledger, *PAID_UP, later_bill=True, later_on=issued)

    ledger.derive()

    found = owed(ledger)
    if governed:
        assert found["bill-2029-06"].amount_due == Decimal("333.84")
        assert "arrears-2029-05" not in found
    else:
        assert found["bill-2029-06"].amount_due == Decimal("299.22")
        assert found["arrears-2029-05"].status == "open"


def test_a_last_instalment_overpaid_just_before_a_new_bill_settles(
    ledger: Ledger,
) -> None:
    """The 8 May payment is the 15 May bill's current charges, and the 9 May 67.08
    overpays the 61.92 last instalment by rule (b) and finishes the plan that day.
    Cut within early_days of it, the bill stays governed, so the finished set holds
    on the second round: the derive completes, and nothing is owed twice."""
    power(
        ledger,
        (D(2029, 3, 26), "38.22"),
        (D(2029, 5, 8), "333.84"),
        (D(2029, 5, 9), "67.08"),
        later_bill=True,
    )

    ledger.derive()

    found = owed(ledger)
    assert {label: found[label].status for label in found} == {
        "bill-2029-02": "superseded",
        "bill-2029-03": "superseded",
        "bill-2029-06": "paid",
        "inst-plan-1": "paid",
        "inst-plan-2": "paid",
    }
    assert applied(ledger) == [
        Applied("bill-2029-06", Decimal("333.84"), True),
        Applied("excess", Decimal("5.16"), True),
        Applied("inst-plan-1", Decimal("38.22"), True),
        Applied("inst-plan-2", Decimal("61.92"), True),
    ]
    assert stage15_apply.finished_arrangements(ledger.uow) == {"plan": D(2029, 5, 9)}
    assert not any(row.arrangement_unsettled for row in found.values())


def test_a_finish_that_moves_a_payment_off_the_plan_keeps_it_active_and_says_so(
    ledger: Ledger,
) -> None:
    """The owner says the 26 April 61.92 paid the arrears row, which exists only once
    the plan is finished. Finished, the decision takes the payment off inst-plan-2 and
    the plan is not finished; active, the row is gone and the decision names nothing.
    Neither reading holds, so the plan stays active (its bills governed, no arrears
    owed beside its paid instalments) and status line 9 names it."""
    power(ledger, *PAID_UP, later_bill=True)
    ledger.ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)
    ledger.derive()
    assert owed(ledger)["arrears-2029-05"].status == "open"
    moved = decide(
        ledger,
        "payment_apply",
        target_ref=line_ref(ledger, D(2029, 4, 26)),
        obligation_ref=obligation_ref(ledger, "arrears-2029-05"),
    )

    ledger.derive()

    found = owed(ledger)
    assert {label: found[label].status for label in found} == {
        "bill-2029-02": "superseded",
        "bill-2029-03": "superseded",
        "bill-2029-06": "open",
        "inst-plan-1": "paid",
        "inst-plan-2": "paid",
    }
    assert found["bill-2029-06"].amount_due == Decimal("333.84")
    assert applied(ledger) == [
        Applied("inst-plan-1", Decimal("38.22"), True),
        Applied("inst-plan-2", Decimal("61.92"), True),
    ]
    assert {label for label, row in found.items() if row.arrangement_unsettled} == {
        "inst-plan-1",
        "inst-plan-2",
    }
    assert facts(ledger, ARRANGEMENT_UNSETTLED) == {"plan": 1}
    # The decision that would not settle is named, on the instalment its payment
    # paid, and nothing about it carries an amount.
    assert {
        label: row.unsettled_decision_id
        for label, row in found.items()
        if row.unsettled_decision_id is not None
    } == {"inst-plan-2": moved}
    assert facts(ledger, ARRANGEMENT_UNSETTLED_BY) == {
        f"plan finance.decision:{moved}": 1
    }
    out = report_ops.status(ledger.ctx, ledger.uow, StatusIn())
    lines = {each.line_no: each.text for each in out.lines}
    assert (
        "1 paid-up arrangement kept active, its finish did not settle:"
        f" plan (by finance.decision:{moved})" in (lines[9])
    )
    rows = sorted(repr(tuple(row)) for row in ledger.conn.execute(select(obligation)))
    ledger.derive()
    assert (
        sorted(repr(tuple(row)) for row in ledger.conn.execute(select(obligation)))
        == rows
    )


@pytest.mark.parametrize(
    ("rounds", "kept"),
    [
        (
            [{"plan": D(2034, 4, 26)}, {"plan": D(2034, 4, 26), "loan": D(2034, 4, 3)}],
            {"plan": D(2034, 4, 26)},
        ),
        (
            [
                {"plan": D(2034, 4, 26), "loan": D(2034, 4, 3)},
                {"plan": D(2034, 4, 26)},
                {"plan": D(2034, 4, 26), "loan": D(2034, 4, 3)},
            ],
            {"plan": D(2034, 4, 26)},
        ),
        ([{"plan": D(2034, 4, 26)}, {"plan": D(2034, 5, 9)}], {}),
    ],
    ids=[
        "the last round finishes one more",
        "one finished in every other round",
        "one finished on two days",
    ],
)
def test_finished_sets_that_would_not_settle_keep_only_what_every_round_agreed(
    rounds: list[dict[str, dt.date]], kept: dict[str, dt.date]
) -> None:
    """What the derive keeps finished when the rounds run out: an arrangement every
    round finished, on one day, and nothing the last round alone says."""
    assert stage15_apply.settled(rounds) == kept


def test_the_arrangements_an_application_finishes_are_settled_after_a_derive(
    ledger: Ledger,
) -> None:
    """Applying again to the bills stage 14 made last finishes the same arrangements,
    on the same day, so another round would change nothing."""
    power(ledger, *PAID_UP, later_bill=True)
    ledger.derive()
    rows = sorted(repr(tuple(row)) for row in ledger.conn.execute(select(obligation)))

    assert stage15_apply.finished_arrangements(ledger.uow) == {"plan": D(2029, 4, 26)}
    ledger.derive()
    assert (
        sorted(repr(tuple(row)) for row in ledger.conn.execute(select(obligation)))
        == rows
    )


@pytest.mark.parametrize("later_bill", [False, True])
def test_an_arrangement_with_an_instalment_unpaid_still_sets_the_bills(
    ledger: Ledger, later_bill: bool
) -> None:
    power(ledger, (D(2029, 3, 26), "38.22"), later_bill=later_bill)

    ledger.derive()

    found = owed(ledger)
    newest = "bill-2029-06" if later_bill else "bill-2029-03"
    assert not [label for label in found if label.startswith("arrears")]
    assert found[newest].amount_due == Decimal("333.84")
    assert (found["inst-plan-1"].status, found["inst-plan-2"].status) == (
        "paid",
        "open",
    )


def test_a_paid_up_arrangement_of_twelve_finishes_on_its_last_payment(
    paying: Ledger,
) -> None:
    twelve_instalments(paying)
    bank(
        paying,
        *((D(2033, 5 + n, 20) if n < 8 else D(2034, n - 7, 20), ISP, Decimal("-51.60"))
          for n in range(1, 13)),
    )  # fmt: skip

    paying.derive()

    assert stage15_apply.finished_arrangements(paying.uow) == {"plan": D(2034, 5, 20)}
