"""The owner's ``pair`` and ``unpair`` decisions (FR-15, FR-18, AC-16, AC-17, AC-19;
SPEC Derivation stages 8, 9 and 11).

Seams: ``finance.decision.record`` and ``finance.decision.reverse`` through
``operations.dispatch``, then ``derive.run`` (the derive ``ledger``) and
``finance.ledger.rebuild`` through ``dispatch``, read back as the txn states, the
``pair_edge`` rows with their kind, ``applied`` flag and ``basis_ref``, the fee lines,
and spending and income as ``ledger.totals.cad_total`` sums the lines that count.
Decisions name lines by their line key (``txn_source.line_key_id``). Every date,
amount, name, address and quantity is invented.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from rheo_finance.clock import FakeClock
from rheo_finance.contracts import Role
from rheo_finance.decision import RecordOut
from rheo_finance.derive.ops import RebuildOut
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.manual.own_addresses import OWN_ADDRESSES_FILE
from rheo_finance.manual.stage2 import account_id
from rheo_finance.operations import dispatch
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import pair_edge, txn, txn_source

from .support import (
    CARD_IDENTIFIER,
    CARD_INSTITUTION,
    CSV_IDENTIFIER,
    Ledger,
    fx_lines,
)
from .support import FX_ISSUER as FX_DIR
from .test_conversion import cover, store_rates
from .test_conversions import DAY as CONVERTED
from .test_conversions import FIG, converted, fee_lines, fig_csv, usd_rate
from .test_crypto_edges import (
    EXCHANGE,
    OWN_WALLET,
    VAULT,
    WALLET,
    at,
    file,
    three_accounts,
)
from .test_overlap import file as file_card
from .test_resolution import CHEQUING, NEEM, income, register, spending
from .test_reversals import OUTGOING, export
from .test_transfer_edges import BILL_PAYMENT, PAYMENT_RECEIVED, neem_export

D = dt.date
PAID, ARRIVED, LATER = D(2055, 6, 11), D(2055, 6, 13), D(2055, 6, 15)
State = tuple[str | None, str | None]
Line = tuple[dt.date, Decimal]
REVERSAL_OF_PAYMENT = OUTGOING.reversal


# --- helpers --------------------------------------------------------------------------


def call(ledger: Ledger, name: str, payload: dict[str, object]) -> BaseModel:
    """``name`` through ``dispatch``, then a minute on the fake clock, so each
    decision is newer than the one before it."""
    ledger.ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)
    out = dispatch(name, payload, Role.OWNER, ctx=ledger.ctx)
    assert isinstance(out, BaseModel)
    assert isinstance(ledger.ctx.clock, FakeClock)
    ledger.ctx.clock.advance(dt.timedelta(minutes=1))
    return out


def key_ref(ledger: Ledger, day: dt.date, amount: str) -> str:
    """The line reference of the one line of ``amount`` on ``day``: its primary
    source's line key."""
    keys = ledger.conn.scalars(
        select(txn_source.c.line_key_id)
        .join(txn, txn.c.id == txn_source.c.txn_id)
        .where(
            txn.c.posting_date == day,
            txn.c.amount == Decimal(amount),
            txn_source.c.role == "primary",
        )
    ).all()
    assert len(keys) == 1
    return format_ref("finance", "txn", keys[0])


def added_ref(ledger: Ledger, day: dt.date, amount: str) -> str:
    """The line reference of the one line of ``amount`` on ``day`` through the key of
    an additional source that is not the txn's id: a line another source matched,
    so the key and the txn id differ."""
    keys = ledger.conn.scalars(
        select(txn_source.c.line_key_id)
        .join(txn, txn.c.id == txn_source.c.txn_id)
        .where(
            txn.c.posting_date == day,
            txn.c.amount == Decimal(amount),
            txn_source.c.role == "additional",
            txn_source.c.line_key_id != txn.c.id,
        )
    ).all()
    assert len(keys) == 1
    return format_ref("finance", "txn", keys[0])


def decide(
    ledger: Ledger,
    kind: str,
    target: str,
    counterpart: str | None = None,
    legs: tuple[str, ...] = (),
) -> uuid.UUID:
    payload: dict[str, object] = {"kind": kind, "target_ref": target}
    if counterpart is not None:
        payload["counterpart_ref"] = counterpart
    if legs:
        payload["legs"] = [{"target_ref": leg} for leg in legs]
    out = call(ledger, "finance.decision.record", payload)
    assert isinstance(out, RecordOut)
    return out.decision_id


def reverse(ledger: Ledger, decision_id: uuid.UUID) -> None:
    call(ledger, "finance.decision.reverse", {"decision_id": str(decision_id)})


def rebuild(ledger: Ledger) -> None:
    assert isinstance(call(ledger, "finance.ledger.rebuild", {}), RebuildOut)


def states(ledger: Ledger) -> dict[Line, State]:
    rows = ledger.conn.execute(
        select(
            txn.c.posting_date, txn.c.amount, txn.c.transfer_state, txn.c.pair_kind
        ).where(txn.c.origin != "derived_fee")
    )
    return {(row[0], row[1]): (row[2], row[3]) for row in rows}


def edges(ledger: Ledger, kind: str = "own_transfer") -> list[tuple[bool, str | None]]:
    """Each edge of ``kind`` as (applied, basis_ref), sorted."""
    rows = ledger.conn.execute(
        select(pair_edge.c.applied, pair_edge.c.basis_ref).where(
            pair_edge.c.kind == kind
        )
    )
    return sorted((tuple(row) for row in rows), key=str)


def kinds(ledger: Ledger) -> list[tuple[str, bool]]:
    rows = ledger.conn.execute(select(pair_edge.c.kind, pair_edge.c.applied))
    return sorted(tuple(row) for row in rows)


def fees(ledger: Ledger) -> list[Decimal]:
    return sorted(
        ledger.conn.scalars(select(txn.c.amount).where(txn.c.origin == "derived_fee"))
    )


def fee_rows(ledger: Ledger) -> list[tuple[uuid.UUID, Decimal]]:
    """Each derived fee line as (account, amount), sorted."""
    rows = ledger.conn.execute(
        select(txn.c.account_id, txn.c.amount).where(txn.c.origin == "derived_fee")
    )
    return sorted((tuple(row) for row in rows), key=str)


def basis(decision_id: uuid.UUID) -> str:
    return format_ref("finance", "decision", decision_id)


def transfer_like_states(ledger: Ledger) -> list[str | None]:
    return list(
        ledger.conn.scalars(select(txn.c.transfer_state).where(txn.c.transfer_like))
    )


def chequing_and_card(
    ledger: Ledger,
    bank: list[tuple[dt.date, str, str]],
    card: list[tuple[dt.date, str]],
) -> None:
    """Chequing lines ``(day, wording, amount)`` and card payment credits ``(day,
    amount)``."""
    register(ledger, CHEQUING, NEEM)
    ledger.file(
        "alder/export.csv",
        export(*((CSV_IDENTIFIER, d, w, Decimal(a)) for d, w, a in bank)),
    )
    if card:
        ledger.file(
            "alder-neem/2055-06-30.csv",
            neem_export(*((d, PAYMENT_RECEIVED, Decimal(a)) for d, a in card)),
        )


def one_payment_two_credits(ledger: Ledger) -> None:
    """A card payment from chequing with two card credits as its candidates."""
    chequing_and_card(
        ledger,
        [(PAID, BILL_PAYMENT, "-216.00")],
        [(ARRIVED, "216.00"), (LATER, "216.00")],
    )


AMBIGUOUS: dict[Line, State] = {
    (PAID, Decimal("-216.00")): ("ambiguous", None),
    (ARRIVED, Decimal("216.00")): ("ambiguous", None),
    (LATER, Decimal("216.00")): ("ambiguous", None),
}


# --- pair: an ambiguous pair resolves, survives rebuild, reverses ---------------------


def test_a_pair_decision_resolves_an_ambiguous_pair_through_a_rebuild_and_reverses(
    ledger: Ledger,
) -> None:
    one_payment_two_credits(ledger)
    ledger.derive()
    assert states(ledger) == AMBIGUOUS

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-216.00"),
        key_ref(ledger, ARRIVED, "216.00"),
    )
    ledger.derive()

    decided = {
        (PAID, Decimal("-216.00")): ("paired", "own_transfer"),
        (ARRIVED, Decimal("216.00")): ("paired", "own_transfer"),
        # The payment is taken, so the later credit has no candidate.
        (LATER, Decimal("216.00")): ("unresolved", None),
    }
    assert states(ledger) == decided
    assert edges(ledger) == [(True, basis(decision_id))]
    assert spending(ledger) == income(ledger) == Decimal("0.00")

    rebuild(ledger)

    assert states(ledger) == decided
    assert edges(ledger) == [(True, basis(decision_id))]

    reverse(ledger, decision_id)
    ledger.derive()

    assert states(ledger) == AMBIGUOUS
    assert edges(ledger) == [(False, None), (False, None)]


def test_the_newest_of_two_pair_decisions_on_one_line_applies(ledger: Ledger) -> None:
    one_payment_two_credits(ledger)
    ledger.derive()
    payment = key_ref(ledger, PAID, "-216.00")
    decide(ledger, "pair", payment, key_ref(ledger, ARRIVED, "216.00"))
    newer = decide(ledger, "pair", payment, key_ref(ledger, LATER, "216.00"))

    ledger.derive()

    assert states(ledger) == {
        (PAID, Decimal("-216.00")): ("paired", "own_transfer"),
        (ARRIVED, Decimal("216.00")): ("unresolved", None),
        (LATER, Decimal("216.00")): ("paired", "own_transfer"),
    }
    assert edges(ledger) == [(True, basis(newer))]


@pytest.mark.parametrize(
    ("credit", "fee"),
    [("15.68", "-5.73"), ("27.14", "5.73")],
    ids=["short", "favourable-keeps-its-sign"],
)
def test_a_pair_across_accounts_with_no_edge_books_the_difference_as_one_fee(
    ledger: Ledger, credit: str, fee: str
) -> None:
    # The card credit is outside the move tolerance, so no candidate edge exists
    # and both lines are unresolved. The owner's pair joins them, and the difference
    # is one fee line on the outbound account, counted once in spending. A two-line
    # pair keeps the signed net (FR-15): only a pair with legs is floored at zero.
    chequing_and_card(ledger, [(PAID, BILL_PAYMENT, "-21.41")], [(ARRIVED, credit)])
    ledger.derive()
    assert edges(ledger) == []

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, ARRIVED, credit),
        key_ref(ledger, PAID, "-21.41"),
    )
    ledger.derive()

    assert states(ledger) == {
        (PAID, Decimal("-21.41")): ("paired", "own_transfer"),
        (ARRIVED, Decimal(credit)): ("paired", "own_transfer"),
    }
    assert edges(ledger) == [(True, basis(decision_id))]
    # On the outbound account (the debit's), though the target is the card credit.
    assert fee_rows(ledger) == [(account_id("chequing"), Decimal(fee))]
    # The fee line is the only line that counts.
    assert spending(ledger) + income(ledger) == Decimal(fee)


def test_an_applied_own_transfer_keeps_a_favourable_net_signed(ledger: Ledger) -> None:
    # No decision: the payment and the card credit are each other's only candidate
    # (inside the spec's 5.00 floor), so the edge applies and its net of +3.24 is one
    # fee line with its sign (FR-15). The zero floor is for decided pairs with legs.
    chequing_and_card(ledger, [(PAID, BILL_PAYMENT, "-216.00")], [(ARRIVED, "219.24")])
    ledger.derive()

    assert edges(ledger) == [(True, None)]
    assert fee_rows(ledger) == [(account_id("chequing"), Decimal("3.24"))]


@pytest.mark.parametrize(
    ("credit", "kind", "fee"),
    [("57.84", "reversal", []), ("52.84", "own_transfer", [Decimal("-5.00")])],
    ids=["reversal-shaped", "unequal"],
)
def test_a_pair_on_one_account_in_one_currency_is_a_reversal_only_when_shaped_so(
    ledger: Ledger, credit: str, kind: str, fee: list[Decimal]
) -> None:
    # Neither line carries reversal wording, so stage 8 offers nothing. Exactly
    # opposite amounts pair as a reversal; any other difference is one fee line.
    register(ledger, CHEQUING)
    ledger.file(
        "alder/export.csv",
        export(
            (CSV_IDENTIFIER, PAID, "SAMPLE SHOP 99971", Decimal("-57.84")),
            (CSV_IDENTIFIER, LATER, "SAMPLE SHOP CREDIT 99972", Decimal(credit)),
        ),
    )
    ledger.derive()

    decision_id = decide(
        ledger, "pair", key_ref(ledger, LATER, credit), key_ref(ledger, PAID, "-57.84")
    )
    ledger.derive()

    assert kinds(ledger) == [(kind, True)]
    assert edges(ledger, kind) == [(True, basis(decision_id))]
    assert set(states(ledger).values()) == {("paired", kind)}
    assert fees(ledger) == fee
    assert spending(ledger) == sum(fee, Decimal("0.00"))


def test_a_pair_decision_nets_a_same_day_reversal_stage_8_only_offered(
    ledger: Ledger,
) -> None:
    register(ledger, CHEQUING)
    ledger.file(
        "alder/export.csv",
        export(
            (CSV_IDENTIFIER, PAID, OUTGOING.original, OUTGOING.amount),
            (CSV_IDENTIFIER, PAID, OUTGOING.reversal, -OUTGOING.amount),
        ),
    )
    ledger.derive()
    assert edges(ledger, "reversal") == [(False, None)]

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, str(-OUTGOING.amount)),
        key_ref(ledger, PAID, str(OUTGOING.amount)),
    )
    ledger.derive()

    assert set(states(ledger).values()) == {("paired", "reversal")}
    assert edges(ledger, "reversal") == [(True, basis(decision_id))]
    assert spending(ledger) == income(ledger) == Decimal("0.00")


# --- pair with legs: one movement, netted once ----------------------------------------


@pytest.mark.parametrize(
    ("second", "fee"),
    [("108.00", []), ("103.00", [Decimal("-5.00")]), ("113.00", [])],
    ids=["nets-to-zero", "short-by-5", "over-by-5-floors-at-zero"],
)
def test_a_pair_with_legs_nets_the_target_against_the_legs_once(
    ledger: Ledger, second: str, fee: list[Decimal]
) -> None:
    # One payment settled by two card credits: no candidate edge (each credit is far
    # from the payment's amount), so the lines are unresolved until the owner pairs
    # the payment with both credits as legs.
    chequing_and_card(
        ledger,
        [(PAID, BILL_PAYMENT, "-216.00")],
        [(ARRIVED, "108.00"), (LATER, second)],
    )
    ledger.derive()

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-216.00"),
        legs=(key_ref(ledger, ARRIVED, "108.00"), key_ref(ledger, LATER, second)),
    )
    ledger.derive()

    assert set(states(ledger).values()) == {("paired", "own_transfer")}
    assert edges(ledger) == [(True, basis(decision_id))] * 2
    assert fees(ledger) == fee
    assert spending(ledger) == sum(fee, Decimal("0.00"))
    assert income(ledger) == Decimal("0.00")


# --- a hand conversion and a mixed pair ----------------------------------------------


def test_a_pair_on_one_account_in_two_currencies_is_a_conversion_with_its_spread(
    fx_ledger: Ledger,
) -> None:
    # No conversion wording, so stage 9 offers no conversion. Paired by the owner, the
    # spread is the out-leg's CAD value minus the in-leg's (its stated CAD value, with
    # no rate stored), and it is one fee line on the conversion date.
    ledger = fx_ledger
    ledger.file(
        f"{FX_DIR}/cad.txt", fx_lines("CAD", "2055-06-11 -151.20 - - SAMPLE OUT 99961")
    )
    ledger.file(
        f"{FX_DIR}/usd.txt",
        fx_lines("USD", "2055-06-11 108.00 146.88 - SAMPLE IN 99962"),
    )
    ledger.derive()
    assert kinds(ledger) == []

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-151.20"),
        key_ref(ledger, PAID, "108.00"),
    )
    ledger.derive()

    assert kinds(ledger) == [("conversion", True)]
    assert edges(ledger, "conversion") == [(True, basis(decision_id))]
    assert ledger.conn.scalar(select(pair_edge.c.spread_cad)) == Decimal("4.32")
    assert fees(ledger) == [Decimal("-4.32")]
    assert set(states(ledger).values()) == {("paired", "conversion")}


def test_a_pair_of_a_fiat_and_a_crypto_line_is_inert(crypto_ledger: Ledger) -> None:
    # No edge kind joins a fiat line to a crypto line, so the decision stays recorded
    # and takes nothing: the withdrawal still pairs with its deposit.
    ledger = crypto_ledger
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={at()} asset=ETH qty=-0.5 type=Withdrawal hint=1",
    )
    file(
        ledger,
        "wallet.txt",
        WALLET,
        f"MOVE at={at(1)} asset=ethereum qty=0.5 type=Received hint=1",
    )
    ledger.cash(f"exchange,{D(2057, 6, 6)},612.45,CAD,SAMPLE SALE 99963,")
    ledger.derive()
    before = states(ledger)
    assert before[(D(2057, 6, 6), Decimal("-0.5"))] == ("paired", "own_transfer")

    decide(
        ledger,
        "pair",
        key_ref(ledger, D(2057, 6, 6), "-0.5"),
        key_ref(ledger, D(2057, 6, 6), "612.45"),
    )
    ledger.derive()

    assert states(ledger) == before
    assert edges(ledger) == [(True, None)]


# --- crypto ---------------------------------------------------------------------------


def test_a_pair_decision_resolves_an_ambiguous_crypto_transfer(
    crypto_ledger: Ledger,
) -> None:
    ledger = crypto_ledger
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={at()} asset=ETH qty=-0.5 type=Withdrawal hint=1",
    )
    file(
        ledger,
        "wallet.txt",
        WALLET,
        f"MOVE at={at(1)} asset=ethereum qty=0.5 type=Received hint=1",
        f"MOVE at={at(3)} asset=ethereum qty=0.5 type=Received hint=1",
    )
    ledger.derive()
    first, second = sorted(
        ledger.conn.execute(
            select(txn.c.occurred_at, txn.c.id).where(txn.c.amount > 0)
        ).all()
    )

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, D(2057, 6, 6), "-0.5"),
        format_ref("finance", "txn", first.id),
    )
    ledger.derive()

    found = {
        row.id: row.transfer_state
        for row in ledger.conn.execute(select(txn.c.id, txn.c.transfer_state))
    }
    assert found[first.id] == "paired"
    assert found[second.id] == "unresolved"
    assert edges(ledger) == [(True, basis(decision_id))]


def test_a_pair_joins_two_crypto_lines_with_no_candidate(
    crypto_ledger: Ledger,
) -> None:
    # The deposit is far below the withdrawal's quantity, so no candidate edge
    # exists; the owner's pair joins them as one own transfer.
    ledger = crypto_ledger
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={at()} asset=ETH qty=-0.5 type=Withdrawal hint=1",
    )
    file(
        ledger,
        "wallet.txt",
        WALLET,
        f"MOVE at={at(1)} asset=ethereum qty=0.3 type=Received hint=1",
    )
    ledger.derive()
    assert kinds(ledger) == []

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, D(2057, 6, 6), "0.3"),
        key_ref(ledger, D(2057, 6, 6), "-0.5"),
    )
    ledger.derive()

    assert edges(ledger) == [(True, basis(decision_id))]
    assert set(states(ledger).values()) == {("paired", "own_transfer")}
    assert fees(ledger) == []


def test_a_one_line_pair_keeps_a_withdrawal_one_sided(crypto_ledger: Ledger) -> None:
    # The withdrawal goes to the wallet's own address and two wallet deposits match:
    # ambiguous. The owner's pair of the withdrawal alone keeps only its one-sided
    # edge, so neither deposit is its other end.
    ledger = crypto_ledger
    ledger.manual(OWN_ADDRESSES_FILE, f"wallet,{OWN_WALLET}")
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={at()} asset=ETH qty=-0.5 type=Withdrawal to={OWN_WALLET}",
    )
    file(
        ledger,
        "wallet.txt",
        WALLET,
        f"MOVE at={at(1)} asset=ethereum qty=0.5 type=Received hint=1",
        f"MOVE at={at(3)} asset=ethereum qty=0.5 type=Received hint=1",
    )
    ledger.derive()
    assert kinds(ledger) == [("own_transfer", False)] * 2

    decision_id = decide(ledger, "pair", key_ref(ledger, D(2057, 6, 6), "-0.5"))
    ledger.derive()

    assert kinds(ledger) == [("own_one_sided", True)]
    assert edges(ledger, "own_one_sided") == [(True, basis(decision_id))]
    found = ledger.conn.execute(select(txn.c.transfer_state, txn.c.pair_kind)).all()
    assert sorted(map(str, found)) == [
        "('paired', 'own_one_sided')",
        "('unresolved', None)",
        "('unresolved', None)",
    ]


# --- unpair ---------------------------------------------------------------------------


def test_an_unpair_removes_an_edge_and_reversing_it_restores_the_pair(
    ledger: Ledger,
) -> None:
    chequing_and_card(ledger, [(PAID, BILL_PAYMENT, "-216.00")], [(ARRIVED, "216.00")])
    ledger.derive()
    assert edges(ledger) == [(True, None)]

    decision_id = decide(
        ledger,
        "unpair",
        key_ref(ledger, PAID, "-216.00"),
        key_ref(ledger, ARRIVED, "216.00"),
    )
    ledger.derive()

    assert edges(ledger) == []
    assert set(states(ledger).values()) == {("unresolved", None)}

    rebuild(ledger)
    assert edges(ledger) == []

    reverse(ledger, decision_id)
    ledger.derive()

    assert edges(ledger) == [(True, None)]
    assert set(states(ledger).values()) == {("paired", "own_transfer")}


def test_an_unpair_makes_a_line_eligible_for_its_other_edge(ledger: Ledger) -> None:
    one_payment_two_credits(ledger)
    ledger.derive()

    decide(
        ledger,
        "unpair",
        key_ref(ledger, PAID, "-216.00"),
        key_ref(ledger, LATER, "216.00"),
    )
    ledger.derive()

    assert states(ledger) == {
        (PAID, Decimal("-216.00")): ("paired", "own_transfer"),
        (ARRIVED, Decimal("216.00")): ("paired", "own_transfer"),
        (LATER, Decimal("216.00")): ("unresolved", None),
    }
    # The remaining edge applied on its own: no decision is its basis.
    assert edges(ledger) == [(True, None)]


def netted_card_payment(ledger: Ledger) -> None:
    """A card payment that stage 8 nets against a later reversal, and the card's
    credit for it, which then has no candidate."""
    chequing_and_card(
        ledger,
        [(PAID, BILL_PAYMENT, "-216.00"), (LATER, REVERSAL_OF_PAYMENT, "216.00")],
        [(ARRIVED, "216.00")],
    )
    ledger.derive()
    assert states(ledger) == {
        (PAID, Decimal("-216.00")): ("paired", "reversal"),
        (LATER, Decimal("216.00")): ("paired", "reversal"),
        (ARRIVED, Decimal("216.00")): ("unresolved", None),
    }


FREED: dict[Line, State] = {
    (PAID, Decimal("-216.00")): ("paired", "own_transfer"),
    (ARRIVED, Decimal("216.00")): ("paired", "own_transfer"),
    (LATER, Decimal("216.00")): ("unresolved", None),
}


def test_an_unpaired_reversal_frees_its_original_for_its_transfer_candidate(
    ledger: Ledger,
) -> None:
    netted_card_payment(ledger)

    decide(
        ledger,
        "unpair",
        key_ref(ledger, LATER, "216.00"),
        key_ref(ledger, PAID, "-216.00"),
    )
    ledger.derive()

    assert states(ledger) == FREED
    assert kinds(ledger) == [("own_transfer", True)]
    assert set(transfer_like_states(ledger)) <= {"paired", "ambiguous", "unresolved"}
    assert spending(ledger) == Decimal("0.00")


def test_a_pair_that_takes_one_line_of_a_reversal_leaves_the_other_unresolved(
    ledger: Ledger,
) -> None:
    netted_card_payment(ledger)

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-216.00"),
        key_ref(ledger, ARRIVED, "216.00"),
    )
    ledger.derive()

    assert states(ledger) == FREED
    assert kinds(ledger) == [("own_transfer", True)]
    assert edges(ledger) == [(True, basis(decision_id))]


@pytest.mark.parametrize(
    "unpair_first", [True, False], ids=["pair-newer", "unpair-newer"]
)
def test_an_unpair_and_a_pair_with_legs_on_one_edge_resolve_by_age(
    ledger: Ledger, unpair_first: bool
) -> None:
    chequing_and_card(
        ledger,
        [(PAID, BILL_PAYMENT, "-216.00")],
        [(ARRIVED, "216.00"), (LATER, "10.80")],
    )
    ledger.derive()
    payment = key_ref(ledger, PAID, "-216.00")
    credit = key_ref(ledger, ARRIVED, "216.00")
    small = key_ref(ledger, LATER, "10.80")

    def pair() -> uuid.UUID:
        return decide(ledger, "pair", payment, credit, legs=(small,))

    if unpair_first:
        decide(ledger, "unpair", payment, credit)
        pair_id = pair()
    else:
        pair_id = pair()
        decide(ledger, "unpair", payment, credit)
    ledger.derive()

    if unpair_first:
        assert edges(ledger) == [(True, basis(pair_id))] * 2
        assert set(states(ledger).values()) == {("paired", "own_transfer")}
    else:
        # The newer unpair holds the edge, so the older pair applies nothing.
        assert edges(ledger) == []
        assert set(states(ledger).values()) == {("unresolved", None)}


# --- the line-key contract ------------------------------------------------------------


def test_a_decision_holds_when_its_line_gets_a_new_primary_source(
    ledger: Ledger, card_parsers: None
) -> None:
    # A provisional card line is decided on; a statement then becomes its primary
    # source, so the txn's id is no longer the key the decision holds. The old key
    # stays on the txn as an additional source, and the decision still applies.
    ledger.accounts(
        ("card", CARD_INSTITUTION, "credit_card", CARD_IDENTIFIER),
        ("chequing", "Alder", "chequing", CSV_IDENTIFIER),
    )
    # A debit and a credit: a hand pair of two debits is inert.
    ledger.cash("chequing,2041-06-11,22.22,CAD,SAMPLE CARD REFUND 99964,")
    for name in ("statement_may", "statement_jun", "download_2041"):
        file_card(ledger, name)
    ledger.derive()
    provisional = key_ref(ledger, D(2041, 6, 9), "-22.22")
    decision_id = decide(
        ledger,
        "pair",
        provisional,
        key_ref(ledger, D(2041, 6, 11), "22.22"),
    )

    file_card(ledger, "statement_jul")
    ledger.derive()

    card_line = ledger.conn.execute(
        select(txn.c.id, txn.c.transfer_state, txn.c.provisional).where(
            txn.c.posting_date == D(2041, 6, 10), txn.c.amount == Decimal("-22.22")
        )
    ).one()
    assert format_ref("finance", "txn", card_line.id) != provisional
    assert not card_line.provisional
    assert card_line.transfer_state == "paired"
    assert edges(ledger) == [(True, basis(decision_id))]


# --- a pair with legs nets once, whatever its edges' kinds ----------------------------


def test_a_pair_with_a_reversal_shaped_leg_nets_once_on_the_outbound_side(
    ledger: Ledger,
) -> None:
    # A deposit paired with a payment of its amount on the same account (a reversal
    # shape, so stage 8 stores that edge) and a small withdrawal, as one movement: the
    # target nets against both legs once, so the 10.80 short is one fee line on the
    # outbound side (the first debit among the legs), not lost.
    chequing_and_card(
        ledger,
        [
            (PAID, BILL_PAYMENT, "-216.00"),
            (ARRIVED, "SAMPLE WITHDRAWAL 99974", "-10.80"),
            (LATER, "SAMPLE DEPOSIT 99973", "216.00"),
        ],
        [],
    )
    ledger.derive()

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, LATER, "216.00"),
        legs=(key_ref(ledger, PAID, "-216.00"), key_ref(ledger, ARRIVED, "-10.80")),
    )
    ledger.derive()

    assert kinds(ledger) == [("own_transfer", True), ("reversal", True)]
    assert edges(ledger, "reversal") == [(True, basis(decision_id))]
    assert edges(ledger) == [(True, basis(decision_id))]
    assert {state for state, _ in states(ledger).values()} == {"paired"}
    assert fee_rows(ledger) == [(account_id("chequing"), Decimal("-10.80"))]


def test_a_pair_with_a_reversal_shaped_leg_and_a_favourable_residual_books_no_fee(
    ledger: Ledger,
) -> None:
    # A payment, a credit of its amount on the same account and a small card credit:
    # the legs are worth 10.80 more than the payment. The net of a pair with legs is
    # floored at zero, as a spread is, so no line is booked and nothing is income.
    chequing_and_card(
        ledger,
        [(PAID, BILL_PAYMENT, "-216.00"), (LATER, "SAMPLE DEPOSIT 99973", "216.00")],
        [(ARRIVED, "10.80")],
    )
    ledger.derive()

    decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-216.00"),
        legs=(key_ref(ledger, LATER, "216.00"), key_ref(ledger, ARRIVED, "10.80")),
    )
    ledger.derive()

    assert kinds(ledger) == [("own_transfer", True), ("reversal", True)]
    assert {state for state, _ in states(ledger).values()} == {"paired"}
    assert fee_rows(ledger) == []
    assert income(ledger) == Decimal("0.00")


def test_a_pair_with_a_conversion_leg_books_its_spread_and_floors_the_rest(
    fx_ledger: Ledger,
) -> None:
    # The target converts to USD on its own account (a conversion by hand, spread
    # 4.32) and a CAD credit of 2.16 comes back. The conversion keeps its own rule
    # and books its spread; the net leaves it out (-151.20, 146.88 and 2.16, plus the
    # 4.32 spread, is 2.16 in the owner's favour), and that is floored at zero.
    ledger = fx_ledger
    ledger.file(
        f"{FX_DIR}/cad.txt",
        fx_lines(
            "CAD",
            "2055-06-11 -151.20 - - SAMPLE OUT 99961",
            "2055-06-11 2.16 - - SAMPLE BACK 99970",
        ),
    )
    ledger.file(
        f"{FX_DIR}/usd.txt",
        fx_lines("USD", "2055-06-11 108.00 146.88 - SAMPLE IN 99962"),
    )
    ledger.derive()

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-151.20"),
        legs=(key_ref(ledger, PAID, "108.00"), key_ref(ledger, PAID, "2.16")),
    )
    ledger.derive()

    assert kinds(ledger) == [("conversion", True), ("own_transfer", True)]
    assert edges(ledger, "conversion") == [(True, basis(decision_id))]
    assert fee_rows(ledger) == [(account_id("fx"), Decimal("-4.32"))]


def test_a_pair_with_a_conversion_leg_nets_the_other_legs_without_its_spread(
    fx_ledger: Ledger,
) -> None:
    # A USD credit (146.88 CAD stated) paired with a CAD debit (a conversion by hand,
    # spread 4.32) and a USD debit of 2.16 (2.36 CAD stated). The conversion books its
    # 4.32; the net leaves it out (146.88 - 151.20 - 2.36 + 4.32), so the 2.36 short
    # is one more fee line on the outbound side, and the spread is not counted twice.
    ledger = fx_ledger
    ledger.file(
        f"{FX_DIR}/cad.txt", fx_lines("CAD", "2055-06-11 -151.20 - - SAMPLE OUT 99961")
    )
    ledger.file(
        f"{FX_DIR}/usd.txt",
        fx_lines(
            "USD",
            "2055-06-11 108.00 146.88 - SAMPLE IN 99962",
            "2055-06-11 -2.16 -2.36 - SAMPLE OUT 99963",
        ),
    )
    ledger.derive()

    decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "108.00"),
        legs=(key_ref(ledger, PAID, "-151.20"), key_ref(ledger, PAID, "-2.16")),
    )
    ledger.derive()

    assert kinds(ledger) == [("conversion", True), ("own_transfer", True)]
    assert fee_rows(ledger) == [
        (account_id("fx"), Decimal("-2.36")),
        (account_id("fx"), Decimal("-4.32")),
    ]


@pytest.mark.parametrize(
    ("ins", "fee"),
    [(("87.68", "59.20"), [Decimal("-4.32")]), (("87.68", "64.80"), [])],
    ids=["short-by-4", "favourable"],
)
def test_one_out_leg_fanned_to_two_in_legs_nets_the_whole_pair_once(
    fx_ledger: Ledger, ins: tuple[str, str], fee: list[Decimal]
) -> None:
    # A CAD debit converted into two USD credits on its account: two conversion edges
    # sharing the out-leg, so neither keeps a rule of its own (each spread against
    # the whole out-leg would book far too much). The pair nets once by CAD values,
    # with its floor.
    ledger = fx_ledger
    first, second = ins
    ledger.file(
        f"{FX_DIR}/cad.txt", fx_lines("CAD", "2055-06-11 -151.20 - - SAMPLE OUT 99961")
    )
    ledger.file(
        f"{FX_DIR}/usd.txt",
        fx_lines(
            "USD",
            f"2055-06-11 64.80 {first} - SAMPLE IN 99962",
            f"2055-06-11 43.20 {second} - SAMPLE IN 99963",
        ),
    )
    ledger.derive()

    decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-151.20"),
        legs=(key_ref(ledger, PAID, "64.80"), key_ref(ledger, PAID, "43.20")),
    )
    ledger.derive()

    assert kinds(ledger) == [("conversion", True), ("conversion", True)]
    assert fees(ledger) == fee
    assert {state for state, _ in states(ledger).values()} == {"paired"}


@pytest.mark.parametrize(
    ("outs", "fee"),
    [(("-83.98", "-83.98"), [Decimal("-4.32")]), (("-83.98", "-64.80"), [])],
    ids=["short-by-4", "favourable"],
)
def test_two_out_legs_fanned_into_one_in_leg_net_the_whole_pair_once(
    fx_ledger: Ledger, outs: tuple[str, str], fee: list[Decimal]
) -> None:
    # Two CAD debits converted into one USD credit (146.88 CAD stated): two conversion
    # edges sharing the in-leg. The pair nets once, on the outbound side.
    ledger = fx_ledger
    first, second = outs
    ledger.file(
        f"{FX_DIR}/cad.txt",
        fx_lines(
            "CAD",
            f"2055-06-11 {first} - - SAMPLE OUT 99961",
            f"2055-06-12 {second} - - SAMPLE OUT 99964",
        ),
    )
    ledger.file(
        f"{FX_DIR}/usd.txt",
        fx_lines("USD", "2055-06-11 108.00 146.88 - SAMPLE IN 99962"),
    )
    ledger.derive()

    decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "108.00"),
        legs=(
            key_ref(ledger, PAID, first),
            key_ref(ledger, D(2055, 6, 12), second),
        ),
    )
    ledger.derive()

    assert kinds(ledger) == [("conversion", True), ("conversion", True)]
    expected = [(account_id("fx"), each) for each in fee]
    assert fee_rows(ledger) == expected


def test_a_conversion_leg_posted_a_day_later_leaves_no_rate_drift_in_the_net(
    fx_ledger: Ledger,
) -> None:
    # A USD credit on 12 Jun (149.04 CAD at that day's rate) paired with the CAD debit
    # of 11 Jun it converts (spread 4.32 at the 11 Jun rate) and a USD debit of 2.16
    # (2.98 CAD). The conversion books its spread; the net takes both its legs out at
    # their own CAD values, so it is the 2.98 alone, not the 0.82 a spread taken out
    # instead would leave, and it is dated by the debit left (12 Jun), not by the
    # conversion's own out-leg of 11 Jun.
    ledger = fx_ledger
    paid, converted = D(2055, 6, 11), D(2055, 6, 12)
    store_rates(ledger, "USD", (paid, "1.3600"), (converted, "1.3800"))
    cover(ledger, "FXUSDCAD", (paid, paid + dt.timedelta(days=30)))
    ledger.file(
        f"{FX_DIR}/cad.txt", fx_lines("CAD", "2055-06-11 -151.20 - - SAMPLE OUT 99961")
    )
    ledger.file(
        f"{FX_DIR}/usd.txt",
        fx_lines(
            "USD",
            "2055-06-12 108.00 - - SAMPLE IN 99962",
            "2055-06-12 -2.16 - - SAMPLE OUT 99963",
        ),
    )
    ledger.derive()

    decide(
        ledger,
        "pair",
        key_ref(ledger, converted, "108.00"),
        legs=(key_ref(ledger, paid, "-151.20"), key_ref(ledger, converted, "-2.16")),
    )
    ledger.derive()

    assert kinds(ledger) == [("conversion", True), ("own_transfer", True)]
    assert ledger.conn.scalar(
        select(pair_edge.c.spread_cad).where(pair_edge.c.kind == "conversion")
    ) == Decimal("4.32")
    assert fee_rows(ledger) == [
        (account_id("fx"), Decimal("-2.98")),
        (account_id("fx"), Decimal("-4.32")),
    ]
    assert fee_lines(ledger, paid) == [("derived_fee", Decimal("-4.32"), "CAD")]
    assert fee_lines(ledger, converted) == [("derived_fee", Decimal("-2.98"), "CAD")]


# --- the candidate stages honour the decisions ----------------------------------------


def test_an_unpair_removes_a_conversion_candidate(ledger: Ledger) -> None:
    register(ledger, FIG)
    usd_rate(ledger, "1.3600")
    ledger.file(
        "fig/statement-2055-07-31.csv",
        fig_csv(*converted("FG-X", CONVERTED, "280.03", "USD",
                            "369.42", "CAD")),
    )  # fmt: skip
    ledger.derive()
    assert kinds(ledger) == [("conversion", True)]

    decide(
        ledger,
        "unpair",
        key_ref(ledger, CONVERTED, "-280.03"),
        key_ref(ledger, CONVERTED, "369.42"),
    )
    ledger.derive()

    assert kinds(ledger) == []
    assert set(states(ledger).values()) == {("unresolved", None)}
    assert fees(ledger) == []


def test_a_conversion_by_hand_names_the_printed_fee_and_books_no_spread(
    ledger: Ledger,
) -> None:
    # A transfer out with its printed fee and a CAD credit, no conversion wording on
    # either. Paired by hand on one account in two currencies, the conversion names
    # the printed fee line, so that line is the date's one fee line.
    register(ledger, FIG)
    usd_rate(ledger, "1.3600")
    ledger.file(
        "fig/statement-2055-07-31.csv",
        fig_csv(
            ("FG-A", CONVERTED, "-109.08", "USD",
             "OUT Sample Payee", "", "1.08", ""),
            ("FG-A", CONVERTED, "145.80", "CAD",
             "IN Sample Payer", "", "0.00", ""),
        ),
    )  # fmt: skip
    ledger.derive()
    assert kinds(ledger) == []

    decide(
        ledger,
        "pair",
        key_ref(ledger, CONVERTED, "-108.00"),
        key_ref(ledger, CONVERTED, "145.80"),
    )
    ledger.derive()

    printed = ledger.conn.scalar(
        select(txn.c.id).where(
            txn.c.fee_type.is_not(None), txn.c.origin != "derived_fee"
        )
    )
    assert kinds(ledger) == [("conversion", True)]
    assert ledger.conn.scalar(select(pair_edge.c.fee_txn_id)) == printed
    assert fee_lines(ledger, CONVERTED) == [("document_line", Decimal("-1.08"), "USD")]


def printed_fee_with_legs(ledger: Ledger, *others: tuple[str, str, str]) -> None:
    """A Fig transfer out of 109.08 USD with its printed fee of 1.08 (the out-leg is
    108.00 USD plus a fee line) and the other lines ``(ident, amount, currency)``,
    all on one day with no conversion wording."""
    register(ledger, FIG)
    usd_rate(ledger, "1.3600")
    ledger.file(
        "fig/statement-2055-07-31.csv",
        fig_csv(
            ("FG-A", CONVERTED, "-109.08", "USD",
             "OUT Sample Payee", "", "1.08", ""),
            *((ident, CONVERTED, amount, code,
               "OUT Sample Payee" if amount.startswith("-")
               else "IN Sample Payer",
               "", "0.00", "") for ident, amount, code in others),
        ),
    )  # fmt: skip
    ledger.derive()


def edge_fees(ledger: Ledger) -> dict[str, uuid.UUID | None]:
    """Each edge's ``fee_txn_id`` by its kind (one edge of each kind)."""
    rows = ledger.conn.execute(select(pair_edge.c.kind, pair_edge.c.fee_txn_id)).all()
    assert len({kind for kind, _ in rows}) == len(rows)
    return {kind: fee for kind, fee in rows}


def test_a_pair_with_legs_keeps_the_printed_fee_and_nets_the_other_legs(
    ledger: Ledger,
) -> None:
    # A CAD credit paired with the USD out-leg (a conversion naming the issuer's
    # printed fee) and a small CAD debit (an own transfer). The printed fee is the
    # conversion's one fee line and its difference from the Rowan price is
    # booked nowhere, so the net leaves the conversion out (145.80 - 146.88 - 0.54
    # + 1.08): the 0.54 short is one fee line named by the own transfer.
    printed_fee_with_legs(
        ledger, ("FG-A", "145.80", "CAD"),
        ("FG-A", "-0.54", "CAD"),
    )  # fmt: skip

    decide(
        ledger,
        "pair",
        key_ref(ledger, CONVERTED, "145.80"),
        legs=(
            key_ref(ledger, CONVERTED, "-108.00"),
            key_ref(ledger, CONVERTED, "-0.54"),
        ),
    )
    ledger.derive()

    printed = ledger.conn.scalar(
        select(txn.c.id).where(
            txn.c.fee_type.is_not(None), txn.c.origin != "derived_fee"
        )
    )
    derived = ledger.conn.scalar(select(txn.c.id).where(txn.c.origin == "derived_fee"))
    assert kinds(ledger) == [("conversion", True), ("own_transfer", True)]
    assert edge_fees(ledger) == {"conversion": printed, "own_transfer": derived}
    assert fees(ledger) == [Decimal("-0.54")]


def test_the_net_of_a_pair_with_legs_sits_on_the_debit_left_once_a_conversion_is_out(
    ledger: Ledger,
) -> None:
    # The same shape with the small CAD debit on chequing, named after the conversion
    # leg. The conversion's own out-leg is the first debit among the lines, but its
    # legs leave the net, so the 0.54 short is dated and placed by the debit left: on
    # chequing, not on the transfer service.
    register(ledger, FIG, CHEQUING)
    usd_rate(ledger, "1.3600")
    ledger.file(
        "fig/statement-2055-07-31.csv",
        fig_csv(
            ("FG-A", CONVERTED, "-109.08", "USD",
             "OUT Sample Payee", "", "1.08", ""),
            ("FG-A", CONVERTED, "145.80", "CAD",
             "IN Sample Payer", "", "0.00", ""),
        ),
    )  # fmt: skip
    ledger.file(
        "alder/export.csv",
        export((CSV_IDENTIFIER, CONVERTED, BILL_PAYMENT, Decimal("-0.54"))),
    )
    ledger.derive()

    decide(
        ledger,
        "pair",
        key_ref(ledger, CONVERTED, "145.80"),
        legs=(
            key_ref(ledger, CONVERTED, "-108.00"),
            key_ref(ledger, CONVERTED, "-0.54"),
        ),
    )
    ledger.derive()

    assert kinds(ledger) == [("conversion", True), ("own_transfer", True)]
    assert fee_rows(ledger) == [(account_id("chequing"), Decimal("-0.54"))]


def test_a_pair_with_legs_whose_every_edge_names_a_printed_fee_books_no_net(
    ledger: Ledger,
) -> None:
    # Both edges are conversions from the one out-leg, so both name its printed fee:
    # the printed fee is the date's fee line and no net is booked beside it.
    printed_fee_with_legs(
        ledger, ("FG-A", "147.89", "CAD"),
        ("FG-A", "3.24", "CAD"),
    )  # fmt: skip
    printed = ledger.conn.scalar(
        select(txn.c.id).where(
            txn.c.fee_type.is_not(None), txn.c.origin != "derived_fee"
        )
    )

    decide(
        ledger,
        "pair",
        key_ref(ledger, CONVERTED, "-108.00"),
        legs=(key_ref(ledger, CONVERTED, "147.89"), key_ref(ledger, CONVERTED, "3.24")),
    )
    ledger.derive()

    named = ledger.conn.execute(select(pair_edge.c.kind, pair_edge.c.fee_txn_id)).all()
    assert sorted(named, key=str) == [("conversion", printed)] * 2
    assert fees(ledger) == []
    assert fee_lines(ledger, CONVERTED) == [("document_line", Decimal("-1.08"), "USD")]


def test_a_fan_one_conversion_of_which_names_a_printed_fee_books_no_net(
    ledger: Ledger,
) -> None:
    # Two USD out-legs fanned into one CAD credit: two conversion edges sharing the
    # in-leg, one naming the issuer's printed fee (its own wording plus "(fee)"), the
    # other, under other wording, naming none. The printed fee is the date's one fee
    # line (FR-15(c)), so the pair books no net beside it: the 18.14 short (176.18 -
    # 146.88 - 47.44) is booked nowhere, as it is when every edge names the fee.
    register(ledger, FIG)
    usd_rate(ledger, "1.3600")
    ledger.file(
        "fig/statement-2055-07-31.csv",
        fig_csv(
            ("FG-A", CONVERTED, "-109.08", "USD",
             "OUT Sample Payee", "", "1.08", ""),
            ("FG-A", CONVERTED, "-34.88", "USD",
             "OUT Sample Other Payee", "", "0.00", ""),
            ("FG-A", CONVERTED, "176.18", "CAD",
             "IN Sample Payer", "", "0.00", ""),
        ),
    )  # fmt: skip
    ledger.derive()
    printed = ledger.conn.scalar(
        select(txn.c.id).where(
            txn.c.fee_type.is_not(None), txn.c.origin != "derived_fee"
        )
    )

    decide(
        ledger,
        "pair",
        key_ref(ledger, CONVERTED, "176.18"),
        legs=(
            key_ref(ledger, CONVERTED, "-108.00"),
            key_ref(ledger, CONVERTED, "-34.88"),
        ),
    )
    ledger.derive()

    named = ledger.conn.execute(select(pair_edge.c.kind, pair_edge.c.fee_txn_id)).all()
    assert sorted(named, key=str) == [("conversion", None), ("conversion", printed)]
    assert states(ledger) == {
        (CONVERTED, Decimal("176.18")): ("paired", "conversion"),
        (CONVERTED, Decimal("-108.00")): ("paired", "conversion"),
        (CONVERTED, Decimal("-34.88")): ("paired", "conversion"),
        (CONVERTED, Decimal("-1.08")): (None, None),
    }
    assert fees(ledger) == []
    assert fee_lines(ledger, CONVERTED) == [("document_line", Decimal("-1.08"), "USD")]


def test_a_pair_of_a_withdrawal_to_an_own_address_and_another_deposit_is_two_sided(
    crypto_ledger: Ledger,
) -> None:
    # The withdrawal goes to the wallet's own address, but the owner pairs it with a
    # deposit on another account: the decided edge is its only edge, with no
    # one-sided edge beside it to apply on its own.
    ledger = crypto_ledger
    three_accounts(ledger)
    ledger.manual(OWN_ADDRESSES_FILE, f"wallet,{OWN_WALLET}")
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={at()} asset=ETH qty=-0.5 type=Withdrawal to={OWN_WALLET}",
    )
    file(
        ledger,
        "vault.txt",
        VAULT,
        f"MOVE at={at(1)} asset=ethereum qty=0.3 type=Received hint=1",
    )
    ledger.derive()
    assert kinds(ledger) == [("own_one_sided", True)]

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, D(2057, 6, 6), "-0.5"),
        key_ref(ledger, D(2057, 6, 6), "0.3"),
    )
    ledger.derive()

    assert kinds(ledger) == [("own_transfer", True)]
    assert edges(ledger) == [(True, basis(decision_id))]


# --- line keys: legs, one-line pairs, unpairs and hand conversions --------------------


def card_copy(ledger: Ledger, day: dt.date, amount: str) -> None:
    """A second card export printing one credit under other wording: it joins the
    credit's txn as an additional source with a key of its own."""
    ledger.file(
        "alder-neem/2055-06-30-copy.csv",
        neem_export((day, "SAMPLE PAYMENT COPY 99968", Decimal(amount))),
    )


def test_a_leg_resolves_through_its_line_key(ledger: Ledger) -> None:
    chequing_and_card(
        ledger,
        [(PAID, BILL_PAYMENT, "-216.00")],
        [(ARRIVED, "108.00"), (LATER, "108.00")],
    )
    card_copy(ledger, ARRIVED, "108.00")
    ledger.derive()

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-216.00"),
        legs=(added_ref(ledger, ARRIVED, "108.00"), key_ref(ledger, LATER, "108.00")),
    )
    ledger.derive()

    assert edges(ledger) == [(True, basis(decision_id))] * 2
    assert fees(ledger) == []


def test_an_unpair_resolves_through_its_line_key(ledger: Ledger) -> None:
    chequing_and_card(ledger, [(PAID, BILL_PAYMENT, "-216.00")], [(ARRIVED, "216.00")])
    card_copy(ledger, ARRIVED, "216.00")
    ledger.derive()
    assert edges(ledger) == [(True, None)]

    decide(
        ledger,
        "unpair",
        key_ref(ledger, PAID, "-216.00"),
        added_ref(ledger, ARRIVED, "216.00"),
    )
    ledger.derive()

    assert edges(ledger) == []


def test_a_one_line_pair_resolves_through_its_line_key(crypto_ledger: Ledger) -> None:
    # The export prints the withdrawal as one figure; the email prints it gross with
    # its fee, and that fee line folds into the withdrawal as an additional source
    # with its own key. A one-line pair naming that key is the withdrawal's.
    ledger = crypto_ledger
    ledger.manual(OWN_ADDRESSES_FILE, f"wallet,{OWN_WALLET}")
    head = f"WITHDRAW at={at()} asset=ETH txid=999tx0bb22 to={OWN_WALLET}"
    file(ledger, "export.csv", EXCHANGE, f"{head} qty=0.5")
    file(ledger, "email.txt", EXCHANGE, f"{head} qty=0.5 fee=0.001 gross=1")
    ledger.derive()
    assert edges(ledger, "own_one_sided") == [(True, None)]

    decision_id = decide(ledger, "pair", added_ref(ledger, D(2057, 6, 6), "-0.5"))
    ledger.derive()

    assert edges(ledger, "own_one_sided") == [(True, basis(decision_id))]


def test_a_conversion_by_hand_resolves_through_its_line_key(fx_ledger: Ledger) -> None:
    ledger = fx_ledger
    ledger.file(
        f"{FX_DIR}/cad.txt", fx_lines("CAD", "2055-06-11 -151.20 - - SAMPLE OUT 99961")
    )
    ledger.file(
        f"{FX_DIR}/cad-copy.txt",
        fx_lines("CAD", "2055-06-11 -151.20 - - SAMPLE OUT COPY 99969"),
    )
    ledger.file(
        f"{FX_DIR}/usd.txt",
        fx_lines("USD", "2055-06-11 108.00 146.88 - SAMPLE IN 99962"),
    )
    ledger.derive()

    decision_id = decide(
        ledger,
        "pair",
        added_ref(ledger, PAID, "-151.20"),
        key_ref(ledger, PAID, "108.00"),
    )
    ledger.derive()

    assert edges(ledger, "conversion") == [(True, basis(decision_id))]
