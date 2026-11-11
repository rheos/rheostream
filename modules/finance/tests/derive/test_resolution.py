"""Derive stage 11 for fiat: mutual uniqueness, the transfer states and the derived fee
lines (FR-15, FR-16, Key decision 9, edge cases 2, 3, 5 and 70; AC-16, AC-17, AC-18
and the AC-80 pairing clause).

Seam: ``derive.run`` through the derive ``ledger`` over synthetic exports read by the
real parsers (Alder chequing and Neem, the Hazel activity CSV, the Fig statement
CSV, the Juniper balance CSV) and a manual cash line, then read back as the txn states,
the ``own_transfer`` edges with their ``applied`` flag and fee line, and spending and
income as ``ledger.totals.cad_total`` sums the lines that count: negative and positive
lines with no transfer state (SPEC "Derivation", stage 13's spending rule). Every
date, amount and name is invented.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal

import pytest
from sqlalchemy import ColumnElement, select

from rheo_finance.ledger.totals import cad_total
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.stage2 import account_id
from rheo_finance.manual.transfer_hints import TRANSFER_HINTS_FILE
from rheo_finance.storage.schema import pair_edge, txn, txn_source

from ..csv_layouts import csv_text
from .support import CSV_IDENTIFIER, FIXTURES, SECOND, Ledger
from .test_reversals import OUTGOING, export
from .test_transfer_edges import BILL_PAYMENT, PAYMENT_RECEIVED, neem_export

D = dt.date
CARD_ACCOUNT = "999000009991"
HAZEL_ID = "AB12CD34"
FIG_ID = "W-0001-ZZ"
JUNIPER_ID = "SAMPLE999JUNIPER"
EXCHANGE_ID = "999000000055"

HAZEL_PULL = "AUTOPULL DEBIT HAZEL 99982"
EXCHANGE_OUT = "SAMPLE EXCHANGE DEPOSIT 99983"
FIG_IN = "DEPOSIT SAMPLE ARRIVAL 99984"
FIG_OUT = "OUT Sample Account Holder"
HINT_ROWS = (
    "sample exchange deposit,out,exchange",
    "out sample account holder,out,chequing",
)

State = tuple[dt.date, Decimal, str | None, str | None]
Account = tuple[str, str, str, str, str]


def register(ledger: Ledger, *rows: Account) -> None:
    """Accounts as ``(slug, institution, kind, currencies, identifier)``."""
    ledger.manual(
        ACCOUNTS_FILE,
        *(f"{slug},{slug.title()},{inst},{kind},{currencies},monthly,,,,,,,,,"
          for slug, inst, kind, currencies, _ in rows),
    )  # fmt: skip
    ledger.manual(
        IDENTIFIERS_FILE, *(f"{slug},{ident},number,," for slug, *_, ident in rows)
    )


CHEQUING: Account = ("chequing", "Alder", "chequing", "CAD", CSV_IDENTIFIER)
SECOND_CHEQUING: Account = ("second", "Alder", "chequing", "CAD", SECOND)
NEEM: Account = ("neem", "Alder", "credit_card", "CAD", CARD_ACCOUNT)
HAZEL: Account = ("hazel", "Hazel", "payment_processor", "CAD;USD", HAZEL_ID)
FIG: Account = ("fig", "Fig", "wallet", "CAD;USD", FIG_ID)
JUNIPER: Account = ("juniper", "Juniper", "payment_processor", "CAD;USD", JUNIPER_ID)
EXCHANGE: Account = ("exchange", "Sample Exchange", "exchange", "CAD", EXCHANGE_ID)


def hazel_csv(*rows: tuple[dt.date, str, str, Decimal]) -> str:
    """A Hazel activity CSV in CAD of ``(day, type, name, gross)`` rows, oldest
    first, no fees, the balance running from zero."""
    balance = Decimal(0)
    found = []
    for day, kind, name, gross in rows:
        balance += gross
        found.append(
            {
                "Hz Day": day.strftime("%Y-%m-%d"), "Hz Party": name, "Hz Kind": kind,
                "Hz Unit": "CAD", "Hz Amount": f"{gross:.2f}", "Hz Fee": "0.00",
                "Hz Balance": f"{balance:.2f}",
            }
        )  # fmt: skip
    return csv_text("hazel", found)


def fig_csv(*rows: tuple[dt.date, str, Decimal, Decimal]) -> str:
    """A Fig statement CSV in CAD of ``(day, description, amount with fee, fee)``
    rows, oldest first, the balance running from 3,285.76."""
    balance = Decimal("3285.76")
    found = []
    for day, description, amount, fee in rows:
        balance += amount
        found.append(
            {
                "Fig Kind": "FG-A",
                "Fig Day": day.strftime("%Y/%m/%d"), "Fig Amount": f"{amount:.2f}",
                "Fig Unit": "CAD", "Fig Text": description,
                "Fig Left": f"{balance:.2f}", "Fig Fee": f"{fee:.2f}",
            }
        )  # fmt: skip
    return csv_text("fig", found)


def states(ledger: Ledger) -> list[State]:
    rows = ledger.conn.execute(
        select(txn.c.posting_date, txn.c.amount, txn.c.transfer_state, txn.c.pair_kind)
    )
    return sorted(tuple(row) for row in rows)


def applied(ledger: Ledger) -> list[bool]:
    return sorted(
        ledger.conn.scalars(
            select(pair_edge.c.applied).where(pair_edge.c.kind == "own_transfer")
        )
    )


def total(ledger: Ledger, *filters: ColumnElement[bool]) -> Decimal:
    """The CAD sum of the lines that count (no transfer state) ``filters`` select."""
    found = cad_total(ledger.uow, [txn.c.transfer_state.is_(None), *filters])
    assert found.unconverted_count == 0
    return found.sum


def spending(ledger: Ledger, *filters: ColumnElement[bool]) -> Decimal:
    return total(ledger, txn.c.amount < 0, *filters)


def income(ledger: Ledger, *filters: ColumnElement[bool]) -> Decimal:
    return total(ledger, txn.c.amount > 0, *filters)


def transfer_like_states(ledger: Ledger) -> list[str | None]:
    return list(
        ledger.conn.scalars(select(txn.c.transfer_state).where(txn.c.transfer_like))
    )


# --- AC-16: four movements between own accounts, each counted zero times --------------


def four_movements(ledger: Ledger) -> list[Callable[[], object]]:
    """A card payment, a Hazel payment funded by the bank, an exchange deposit and a
    Fig to chequing transfer whose chequing leg arrived 4.30 short, as the steps
    that file them."""
    register(ledger, CHEQUING, NEEM, HAZEL, FIG, EXCHANGE)
    ledger.manual(TRANSFER_HINTS_FILE, *HINT_ROWS)
    ledger.cash(f"exchange,{D(2037, 6, 11)},821.44,CAD,SAMPLE EXCHANGE CREDIT,")
    bank = export(
        (CSV_IDENTIFIER, D(2037, 6, 3), BILL_PAYMENT, Decimal("-1008.02")),
        (CSV_IDENTIFIER, D(2037, 6, 6), HAZEL_PULL, Decimal("-47.61")),
        (CSV_IDENTIFIER, D(2037, 6, 10), EXCHANGE_OUT, Decimal("-821.44")),
        (CSV_IDENTIFIER, D(2037, 6, 13), FIG_IN, Decimal("1642.88")),
    )
    card = neem_export(
        (D(2037, 5, 29), "SAMPLE SHOP 99985", Decimal("-1008.02")),
        (D(2037, 6, 5), PAYMENT_RECEIVED, Decimal("1008.02")),
    )
    hazel = hazel_csv(
        (D(2037, 6, 7), "HZ LOAD", "", Decimal("47.61")),
        (D(2037, 6, 7), "HZ SPEND", "Sample Merchant",
         Decimal("-47.61")),
    )  # fmt: skip
    fig = fig_csv((D(2037, 6, 12), FIG_OUT, Decimal("-1647.18"), Decimal("0")))
    return [
        lambda: ledger.file("alder/export.csv", bank),
        lambda: ledger.file("alder-neem/2037-06-30.csv", card),
        lambda: ledger.file("hazel/activity-2037-06-30.csv", hazel),
        lambda: ledger.file("fig/statement-2037-06-30.csv", fig),
    ]


PAIRED = ("paired", "own_transfer")
AC16_STATES: list[State] = [
    (D(2037, 5, 29), Decimal("-1008.02"), None, None),  # the card purchase
    (D(2037, 6, 3), Decimal("-1008.02"), *PAIRED),
    (D(2037, 6, 5), Decimal("1008.02"), *PAIRED),
    (D(2037, 6, 6), Decimal("-47.61"), *PAIRED),
    (D(2037, 6, 7), Decimal("-47.61"), None, None),  # the Hazel purchase
    (D(2037, 6, 7), Decimal("47.61"), *PAIRED),
    (D(2037, 6, 10), Decimal("-821.44"), *PAIRED),
    (D(2037, 6, 11), Decimal("821.44"), *PAIRED),
    (D(2037, 6, 12), Decimal("-1647.18"), *PAIRED),
    (D(2037, 6, 12), Decimal("-4.30"), None, None),  # the derived fee
    (D(2037, 6, 13), Decimal("1642.88"), *PAIRED),
]


@pytest.mark.parametrize("order", ["filed", "reversed"])
def test_movements_between_own_accounts_count_zero_times_and_the_fee_once(
    ledger: Ledger, order: str
) -> None:
    steps = four_movements(ledger)
    for step in steps if order == "filed" else steps[::-1]:
        step()

    ledger.derive()

    # The same result whichever order the documents were imported in.
    assert states(ledger) == AC16_STATES
    assert applied(ledger) == [True] * 4
    # Spending is the two purchases and the fee; nothing is income.
    assert spending(ledger) == Decimal("-1059.93")
    assert income(ledger) == Decimal("0.00")


def test_the_derived_fee_is_one_line_on_the_outbound_account(ledger: Ledger) -> None:
    for step in four_movements(ledger):
        step()

    ledger.derive()

    fees = ledger.conn.execute(
        select(txn.c.id, txn.c.account_id, txn.c.posting_date, txn.c.amount,
               txn.c.currency, txn.c.cad_amount, txn.c.tax_year, txn.c.fee_type)
        .where(txn.c.origin == "derived_fee")
    ).all()  # fmt: skip
    assert [tuple(fee)[1:] for fee in fees] == [
        (account_id("fig"), D(2037, 6, 12), Decimal("-4.30"), "CAD",
         Decimal("-4.30"), 2037, "fee"),
    ]  # fmt: skip
    # The Fig transfer's edge names it; the other three balance and name none.
    named = ledger.conn.scalars(
        select(pair_edge.c.fee_txn_id).where(pair_edge.c.fee_txn_id.is_not(None))
    ).all()
    assert named == [fees[0].id]
    source = ledger.conn.execute(
        select(txn_source.c.role, txn_source.c.line_key_id).where(
            txn_source.c.txn_id == fees[0].id
        )
    ).all()
    assert [tuple(row) for row in source] == [("primary", fees[0].id)]


@pytest.mark.parametrize("printed", [True, False], ids=["printed", "derived"])
def test_a_transfer_fee_counts_once_printed_or_derived(
    ledger: Ledger, printed: bool
) -> None:
    # Printed, the export's fee is its own line and the legs balance after it; not
    # printed, the legs differ by it and stage 11 books it.
    register(ledger, CHEQUING, FIG)
    ledger.manual(TRANSFER_HINTS_FILE, *HINT_ROWS)
    fee = Decimal("4.30") if printed else Decimal("0")
    ledger.file(
        "fig/statement-2037-06-30.csv",
        fig_csv((D(2037, 6, 12), FIG_OUT, Decimal("-1647.18"), fee)),
    )
    ledger.file(
        "alder/export.csv",
        export((CSV_IDENTIFIER, D(2037, 6, 13), FIG_IN, Decimal("1642.88"))),
    )

    ledger.derive()

    fee_lines = ledger.conn.execute(
        select(txn.c.origin, txn.c.amount).where(txn.c.fee_type.is_not(None))
    ).all()
    origin = "document_line" if printed else "derived_fee"
    assert [tuple(row) for row in fee_lines] == [(origin, Decimal("-4.30"))]
    assert applied(ledger) == [True]
    assert spending(ledger) == Decimal("-4.30")
    assert income(ledger) == Decimal("0.00")


# --- AC-16, AC-17: ambiguous and unresolved -------------------------------------------


def test_two_candidate_counterparts_leave_every_line_ambiguous(ledger: Ledger) -> None:
    register(ledger, CHEQUING, NEEM)
    ledger.file(
        "alder/export.csv",
        export((CSV_IDENTIFIER, D(2037, 6, 3), BILL_PAYMENT, Decimal("-1008.02"))),
    )
    ledger.file(
        "alder-neem/2037-06-30.csv",
        neem_export(
            (D(2037, 6, 5), PAYMENT_RECEIVED, Decimal("1008.02")),
            (D(2037, 6, 7), PAYMENT_RECEIVED, Decimal("1008.02")),
        ),
    )

    ledger.derive()

    assert states(ledger) == [
        (D(2037, 6, 3), Decimal("-1008.02"), "ambiguous", None),
        (D(2037, 6, 5), Decimal("1008.02"), "ambiguous", None),
        (D(2037, 6, 7), Decimal("1008.02"), "ambiguous", None),
    ]
    assert applied(ledger) == [False, False]
    assert income(ledger) == spending(ledger) == Decimal("0.00")


def test_two_debits_sharing_their_one_credit_leave_all_three_ambiguous(
    ledger: Ledger,
) -> None:
    register(ledger, CHEQUING, SECOND_CHEQUING, NEEM)
    ledger.file(
        "alder/export.csv",
        export(
            (CSV_IDENTIFIER, D(2037, 6, 3), BILL_PAYMENT, Decimal("-1008.02")),
            (SECOND, D(2037, 6, 4), BILL_PAYMENT, Decimal("-1008.02")),
        ),
    )
    ledger.file(
        "alder-neem/2037-06-30.csv",
        neem_export((D(2037, 6, 5), PAYMENT_RECEIVED, Decimal("1008.02"))),
    )

    ledger.derive()

    assert states(ledger) == [
        (D(2037, 6, 3), Decimal("-1008.02"), "ambiguous", None),
        (D(2037, 6, 4), Decimal("-1008.02"), "ambiguous", None),
        (D(2037, 6, 5), Decimal("1008.02"), "ambiguous", None),
    ]
    assert applied(ledger) == [False, False]


@pytest.mark.parametrize(
    ("received", "state"),
    [("17.28", "paired"), ("17.27", "unresolved")],
    ids=["inside", "outside"],
)
def test_a_lone_counterpart_outside_the_tolerance_leaves_both_unresolved(
    ledger: Ledger, received: str, state: str
) -> None:
    register(ledger, CHEQUING, NEEM)
    ledger.file(
        "alder/export.csv",
        export((CSV_IDENTIFIER, D(2037, 6, 3), BILL_PAYMENT, Decimal("-22.28"))),
    )
    ledger.file(
        "alder-neem/2037-06-30.csv",
        neem_export((D(2037, 6, 5), PAYMENT_RECEIVED, Decimal(received))),
    )

    ledger.derive()

    assert sorted(transfer_like_states(ledger), key=str) == [state, state]


# --- AC-18: a card payment across a year end ------------------------------------------


def test_a_december_purchase_paid_in_january_counts_once_in_the_earlier_year(
    ledger: Ledger,
) -> None:
    register(ledger, CHEQUING, NEEM)
    ledger.file(
        "alder/export.csv",
        export((CSV_IDENTIFIER, D(2036, 12, 30), BILL_PAYMENT, Decimal("-322.20"))),
    )
    ledger.file(
        "alder-neem/2037-01-31.csv",
        neem_export(
            (D(2036, 12, 30), "SAMPLE SHOP 99986", Decimal("-322.20")),
            (D(2037, 1, 3), PAYMENT_RECEIVED, Decimal("322.20")),
        ),
    )

    ledger.derive()

    purchase_year = ledger.conn.scalar(
        select(txn.c.tax_year).where(txn.c.transfer_state.is_(None))
    )
    assert purchase_year == 2036
    assert transfer_like_states(ledger) == ["paired", "paired"]
    assert spending(ledger, txn.c.tax_year == 2036) == Decimal("-322.20")
    assert spending(ledger, txn.c.tax_year == 2037) == Decimal("0.00")


# --- AC-80: a Fig deposit from Juniper and the Juniper payout ------------------------


@pytest.mark.parametrize("with_juniper", [True, False], ids=["juniper", "no-juniper"])
def test_a_fig_deposit_from_juniper_pairs_with_the_payout_or_ends_unresolved(
    ledger: Ledger, with_juniper: bool
) -> None:
    register(ledger, FIG, JUNIPER)
    fig = (FIXTURES / "fig_statement.csv").read_text(encoding="utf-8")
    ledger.file("fig/statement-2045-08-28.csv", fig)
    if with_juniper:
        juniper = (FIXTURES / "juniper_balance.csv").read_text(encoding="utf-8")
        ledger.file("juniper/balance-2045-08-27.csv", juniper)

    ledger.derive()

    found = states(ledger)
    deposit = (D(2045, 8, 27), Decimal("257.05"))
    payout = (D(2045, 8, 26), Decimal("-257.05"))
    if with_juniper:
        assert (*deposit, *PAIRED) in found
        assert (*payout, *PAIRED) in found
        assert applied(ledger) == [True]
    else:
        assert (*deposit, "unresolved", None) in found
        assert applied(ledger) == []
    # AC-17: every transfer-like line is in exactly one of the three states.
    assert set(transfer_like_states(ledger)) <= {"paired", "ambiguous", "unresolved"}
    assert None not in transfer_like_states(ledger)


# --- stage 8's reversals stay as stage 8 settled them ---------------------------------


def test_a_reversed_payment_stays_settled_and_its_card_leg_is_unresolved(
    ledger: Ledger,
) -> None:
    register(ledger, CHEQUING, NEEM)
    sent, arrived, cancelled = D(2037, 6, 3), D(2037, 6, 5), D(2037, 6, 6)
    ledger.file(
        "alder/export.csv",
        export(
            (CSV_IDENTIFIER, sent, BILL_PAYMENT, Decimal("-1008.02")),
            (CSV_IDENTIFIER, cancelled, OUTGOING.reversal, Decimal("1008.02")),
        ),
    )
    ledger.file(
        "alder-neem/2037-06-30.csv",
        neem_export((arrived, PAYMENT_RECEIVED, Decimal("1008.02"))),
    )

    ledger.derive()

    assert states(ledger) == [
        (sent, Decimal("-1008.02"), "paired", "reversal"),
        (arrived, Decimal("1008.02"), "unresolved", None),
        (cancelled, Decimal("1008.02"), "paired", "reversal"),
    ]
    reversal = ledger.conn.scalars(
        select(pair_edge.c.applied).where(pair_edge.c.kind == "reversal")
    ).all()
    assert reversal == [True]


def test_the_original_of_a_same_day_reversal_is_not_made_ambiguous(
    ledger: Ledger,
) -> None:
    # Stage 8 offers the same-day original as the reversal's one candidate (the
    # reversal ``ambiguous``) and leaves the original an ordinary line. That edge is
    # stage 8's: the original, a transfer-like payment with no transfer candidate,
    # is unresolved, never ambiguous on its account.
    register(ledger, CHEQUING)
    day = D(2037, 6, 3)
    ledger.file(
        "alder/export.csv",
        export(
            (CSV_IDENTIFIER, day, BILL_PAYMENT, Decimal("-1008.02")),
            (CSV_IDENTIFIER, day, OUTGOING.reversal, Decimal("1008.02")),
        ),
    )

    ledger.derive()

    assert states(ledger) == [
        (day, Decimal("-1008.02"), "unresolved", None),
        (day, Decimal("1008.02"), "ambiguous", None),
    ]
    reversal = ledger.conn.scalars(
        select(pair_edge.c.applied).where(pair_edge.c.kind == "reversal")
    ).all()
    assert reversal == [False]


def test_a_same_day_reversal_edge_and_a_card_candidate_leave_both_ambiguous(
    ledger: Ledger,
) -> None:
    # Uniqueness counts every kind together: the payment is the original a same-day
    # reversal names (stage 8's edge, not applied) and the card credit's one transfer
    # candidate. Two edges on the payment, so the transfer edge does not apply.
    register(ledger, CHEQUING, NEEM)
    day, arrived = D(2037, 6, 3), D(2037, 6, 5)
    ledger.file(
        "alder/export.csv",
        export(
            (CSV_IDENTIFIER, day, BILL_PAYMENT, Decimal("-1008.02")),
            (CSV_IDENTIFIER, day, OUTGOING.reversal, Decimal("1008.02")),
        ),
    )
    ledger.file(
        "alder-neem/2037-06-30.csv",
        neem_export((arrived, PAYMENT_RECEIVED, Decimal("1008.02"))),
    )

    ledger.derive()

    assert states(ledger) == [
        (day, Decimal("-1008.02"), "ambiguous", None),
        (day, Decimal("1008.02"), "ambiguous", None),
        (arrived, Decimal("1008.02"), "ambiguous", None),
    ]
    assert applied(ledger) == [False]
    assert not ledger.conn.scalar(select(pair_edge.c.applied).where(
        pair_edge.c.kind == "reversal"))  # fmt: skip
    assert income(ledger) == spending(ledger) == Decimal("0.00")
