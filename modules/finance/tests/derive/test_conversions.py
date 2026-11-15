"""Same-account currency conversions: the ``conversion`` edge, its spread and its one
fee line (FR-15(c), Derivation stage 9 ``conversion``, edge case 69, AC-85).

Seam: ``derive.run`` through the derive ``ledger`` over Fig and Hazel documents read
by the real parsers (the ``fig_history.txt``, ``fig_statement.csv`` and
``hazel_activity.csv`` fixtures, and Fig statement and Hazel activity CSVs built
here in the same layouts), with Rowan rates stored as a fetch leaves them;
read back as the ``conversion`` rows of ``pair_edge``, the legs' transfer states, the
fee lines on the conversion date and the CAD totals. Every date, amount and rate is
invented.

The parsers print a conversion's out-leg net of the issuer's fee, with the fee as its
own line, so the spread is the out-leg's CAD value minus the in-leg's: the printed fee
is never taken off it a second time.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import ColumnElement, select

from rheo_finance.ledger.totals import CadTotal, cad_total
from rheo_finance.manual.stage2 import account_id
from rheo_finance.manual.transfer_hints import TRANSFER_HINTS_FILE
from rheo_finance.storage.schema import pair_edge, txn

from ..csv_layouts import csv_text
from .support import CSV_IDENTIFIER, FIXTURES, Ledger
from .test_conversion import cover, store_rates
from .test_resolution import CHEQUING, HAZEL_PULL, Account, register
from .test_reversals import export

D = dt.date
FIG_ID = "W-0001-ZZ"
HAZEL_ID = "AB12CD34"
FIG: Account = ("fig", "Fig", "wallet", "CAD;USD;EUR", FIG_ID)
HAZEL: Account = ("hazel", "Hazel", "payment_processor", "CAD;USD", HAZEL_ID)
CONVERSION = "HZ RATE"
DAY = D(2054, 7, 16)

# One Fig statement row: (kind, day, amount with fee, currency, description, the
# other currency, fee, the sum in the other currency).
FigRow = tuple[str, dt.date, str, str, str, str, str, str]
# One Hazel activity row: (day, type, currency, gross), or with its printed fee
# (negative) after the gross.
HazelRow = tuple[dt.date, str, str, str] | tuple[dt.date, str, str, str, str]
Leg = tuple[dt.date, Decimal, str, str | None, str | None]


def fig_csv(*rows: FigRow) -> str:
    """A Fig statement CSV of ``rows``, oldest first, each currency's balance running
    from 1,101.59."""
    balances: dict[str, Decimal] = {}
    found = []
    for kind, day, amount, code, description, other, fee, sum_ in rows:
        balances[code] = balances.get(code, Decimal("1101.59")) + Decimal(amount)
        found.append(
            {
                "Fig Kind": kind, "Fig Day": day.strftime("%Y/%m/%d"),
                "Fig Amount": amount, "Fig Unit": code, "Fig Text": description,
                "Fig Left": f"{balances[code]:.2f}", "Fig Other Unit": other,
                "Fig Fee": fee, "Fig Other Sum": sum_,
            }
        )  # fmt: skip
    return csv_text("fig", found)


def converted(
    kind: str, day: dt.date, out: str, out_code: str, into: str, into_code: str,
    fee: str = "0.00",
) -> tuple[FigRow, FigRow]:  # fmt: skip
    """A Fig conversion: the out row (its amount the converted ``out`` plus ``fee``)
    and the in row, each naming the other's currency and sum."""
    words = f"Turned {out} {out_code} to {into} {into_code}"
    paid = f"-{Decimal(out) + Decimal(fee):.2f}"
    return (
        (kind, day, paid, out_code, words, into_code, fee, into),
        (kind, day, into, into_code, words, out_code, "0.00", out),
    )


def hazel_csv(*rows: HazelRow) -> str:
    """A Hazel activity CSV of ``rows``, oldest first, a fee only where a row gives
    one, each currency's balance running from 414.82."""
    balances: dict[str, Decimal] = {}
    found = []
    for row in rows:
        day, kind, code, gross = row[:4]
        fee = row[4] if len(row) == 5 else "0.00"
        net = f"{Decimal(gross) + Decimal(fee):.2f}"
        balances[code] = balances.get(code, Decimal("414.82")) + Decimal(net)
        found.append(
            {
                "Hz Day": day.strftime("%Y-%m-%d"), "Hz Kind": kind, "Hz Unit": code,
                "Hz Amount": gross, "Hz Fee": fee,
                "Hz Balance": f"{balances[code]:.2f}",
            }
        )  # fmt: skip
    return csv_text("hazel", found)


def usd_rate(ledger: Ledger, value: str, *, on: dt.date = DAY) -> None:
    store_rates(ledger, "USD", (on, value))
    cover(ledger, "FXUSDCAD", (on, on + dt.timedelta(days=30)))


def legs(ledger: Ledger) -> list[Leg]:
    """Every line with conversion wording (the parsers' fee lines carry none)."""
    rows = ledger.conn.execute(
        select(txn.c.posting_date, txn.c.amount, txn.c.currency,
               txn.c.transfer_state, txn.c.pair_kind)
        .where(txn.c.transfer_like)
    )  # fmt: skip
    return sorted(tuple(row) for row in rows)


def edges(ledger: Ledger) -> list[tuple[Decimal, Decimal, bool, Decimal | None]]:
    """The conversion edges as (out amount, in amount, applied, spread)."""
    a, b = txn.alias("a"), txn.alias("b")
    rows = ledger.conn.execute(
        select(a.c.amount, b.c.amount, pair_edge.c.applied, pair_edge.c.spread_cad)
        .join(a, a.c.id == pair_edge.c.a_txn_id)
        .join(b, b.c.id == pair_edge.c.b_txn_id)
        .where(pair_edge.c.kind == "conversion")
    )
    return sorted(tuple(row) for row in rows)


def fee_lines(ledger: Ledger, day: dt.date) -> list[tuple[str, Decimal, str]]:
    """The fee lines posted on ``day`` as (origin, amount, currency)."""
    rows = ledger.conn.execute(
        select(txn.c.origin, txn.c.amount, txn.c.currency).where(
            txn.c.fee_type.is_not(None), txn.c.posting_date == day
        )
    )
    return sorted(tuple(row) for row in rows)


def counted(ledger: Ledger, *filters: ColumnElement[bool]) -> CadTotal:
    """The lines that count (no transfer state) ``filters`` select, in CAD."""
    return cad_total(ledger.uow, [txn.c.transfer_state.is_(None), *filters])


def paired(day: dt.date, amount: str, code: str) -> Leg:
    return (day, Decimal(amount), code, "paired", "conversion")


def left(day: dt.date, amount: str, code: str, state: str) -> Leg:
    return (day, Decimal(amount), code, state, None)


# --- Fig: one fee line, printed or derived -------------------------------------------


@pytest.mark.parametrize(
    ("fee", "expected"),
    [("0.53", ("document_line", "-0.53", "USD")),
     ("0.00", ("derived_fee", "-37.19", "CAD"))],
    ids=["printed", "derived"],
)  # fmt: skip
def test_a_conversion_date_carries_exactly_one_fee_line_printed_or_derived(
    ledger: Ledger, fee: str, expected: tuple[str, str, str]
) -> None:
    # 723.18 USD at 1.3600 is 983.52 CAD against 946.33 CAD in: a spread of 37.19,
    # inside 5 percent of 983.52. Printed, the fee is the parser's own line and the
    # spread is booked nowhere; not printed, the spread is the one fee line.
    register(ledger, FIG)
    usd_rate(ledger, "1.3600")
    ledger.file(
        "fig/statement-2054-07-31.csv",
        fig_csv(*converted("FG-X", DAY, "723.18", "USD", "946.33",
                            "CAD", fee)),
    )  # fmt: skip

    ledger.derive()

    assert legs(ledger) == [
        paired(DAY, "-723.18", "USD"),
        paired(DAY, "946.33", "CAD"),
    ]
    # The spread is the out-leg's CAD value minus the in-leg's, the printed fee not
    # taken off it a second time.
    assert edges(ledger) == [
        (Decimal("-723.18"), Decimal("946.33"), True, Decimal("37.19"))
    ]
    origin, amount, code = expected
    assert fee_lines(ledger, DAY) == [(origin, Decimal(amount), code)]
    # The legs count zero times; the fee once, as spending; nothing is income.
    spent = counted(ledger, txn.c.amount < 0)
    assert spent.sum == (Decimal("-37.19") if fee == "0.00" else Decimal("-0.72"))
    assert counted(ledger, txn.c.amount > 0).sum == Decimal("0.00")


@pytest.mark.parametrize(
    ("name", "path"),
    [("fig_history.txt", "fig/2045-08-history.pdf"),
     ("fig_statement.csv", "fig/statement-2045-08-28.csv")],
    ids=["history-pdf", "statement-csv"],
)  # fmt: skip
def test_the_fig_fixtures_pair_their_conversion_and_keep_the_printed_fee_alone(
    ledger: Ledger, name: str, path: str
) -> None:
    # The fixtures convert 504.01 USD (a printed fee of 0.53 USD) to 687.54 CAD. At
    # 1.3652 the out-leg is worth 688.07 CAD: a spread of 0.53, booked nowhere.
    day = D(2045, 8, 20)
    register(ledger, FIG)
    usd_rate(ledger, "1.3652", on=day)
    ledger.file(path, (FIXTURES / name).read_text(encoding="utf-8"))

    ledger.derive()

    assert paired(day, "-504.01", "USD") in legs(ledger)
    assert paired(day, "687.54", "CAD") in legs(ledger)
    assert edges(ledger) == [
        (Decimal("-504.01"), Decimal("687.54"), True, Decimal("0.53"))
    ]
    assert fee_lines(ledger, day) == [("document_line", Decimal("-0.53"), "USD")]
    named = ledger.conn.scalar(select(pair_edge.c.fee_txn_id))
    printed = ledger.conn.scalar(
        select(txn.c.id).where(txn.c.fee_type.is_not(None), txn.c.posting_date == day)
    )
    assert named == printed


# --- Hazel: the spread tolerance in both directions ----------------------------------


@pytest.mark.parametrize(
    ("rate", "pairs", "fee"),
    [
        ("1.3408", True, "-1.56"),  # 43.08 CAD in: a 3.5 percent spread
        ("1.3200", True, "-2.23"),  # 42.41 CAD in: the last cent inside 5 percent
        ("1.4100", True, None),  # 45.30 CAD in: richer than the out-leg
        ("1.3196", False, None),  # 42.40 CAD in: just over 5 percent
        ("1.2800", False, None),  # 41.13 CAD in: far under
        ("1.5000", False, None),  # 48.20 CAD in: far over
    ],
    ids=["spread-3.5pct", "at-5pct", "in-leg-richer", "just-outside", "low",
         "high"],
)  # fmt: skip
def test_the_hazel_conversion_pairs_only_inside_its_own_spread_tolerance(
    ledger: Ledger, rate: str, pairs: bool, fee: str | None
) -> None:
    # The fixture converts 44.64 CAD to 32.13 USD on one day. The move tolerance
    # (the larger of 5.00 CAD and 1.5 percent) is never used here.
    day = D(2030, 11, 3)
    register(ledger, HAZEL)
    usd_rate(ledger, rate, on=day)
    ledger.file(
        "hazel/activity-2030-11-20.csv",
        (FIXTURES / "hazel_activity.csv").read_text(encoding="utf-8"),
    )

    ledger.derive()

    out, into = ("-44.64", "CAD"), ("32.13", "USD")
    found = legs(ledger)
    if pairs:
        assert {paired(day, *out), paired(day, *into)} <= set(found)
        assert [applied for _, _, applied, _ in edges(ledger)] == [True]
    else:
        # No edge, so nothing measured them: both legs are unresolved.
        assert {left(day, *out, "unresolved"), left(day, *into, "unresolved")} <= set(
            found
        )
        assert edges(ledger) == []
    expected = [] if fee is None else [("derived_fee", Decimal(fee), "CAD")]
    assert fee_lines(ledger, day) == expected
    # A richer in-leg is a zero fee, never income.
    income = counted(ledger, txn.c.amount > 0, txn.c.posting_date == day)
    assert income.sum == Decimal("0.00")


def test_a_hazel_spread_of_three_and_a_half_percent_pairs_on_a_built_export(
    ledger: Ledger,
) -> None:
    # 1008.02 CAD to 778.18 USD at 1.2500: 972.72 CAD in, a spread of 35.30. The
    # move tolerance (5.00 CAD here) would refuse it; the spread tolerance holds.
    register(ledger, HAZEL)
    usd_rate(ledger, "1.2500")
    ledger.file(
        "hazel/activity-2054-07-31.csv",
        hazel_csv((DAY, CONVERSION, "CAD", "-1008.02"),
                   (DAY, CONVERSION, "USD", "778.18")),
    )  # fmt: skip

    ledger.derive()

    assert legs(ledger) == [
        paired(DAY, "-1008.02", "CAD"),
        paired(DAY, "778.18", "USD"),
    ]
    assert edges(ledger) == [
        (Decimal("-1008.02"), Decimal("778.18"), True, Decimal("35.30"))
    ]
    assert fee_lines(ledger, DAY) == [("derived_fee", Decimal("-35.30"), "CAD")]
    fee_account = ledger.conn.scalar(
        select(txn.c.account_id).where(txn.c.origin == "derived_fee")
    )
    assert fee_account == account_id("hazel")


# --- a printed fee line belongs to at most one conversion -----------------------------


def fee_owners(ledger: Ledger) -> list[tuple[Decimal, Decimal | None]]:
    """Each conversion edge's out-leg amount and the amount of the fee line it names."""
    out, fee = txn.alias("out"), txn.alias("fee")
    rows = ledger.conn.execute(
        select(out.c.amount, fee.c.amount)
        .select_from(pair_edge)
        .join(out, out.c.id == pair_edge.c.a_txn_id)
        .outerjoin(fee, fee.c.id == pair_edge.c.fee_txn_id)
        .where(pair_edge.c.kind == "conversion")
    )
    return sorted((row[0], row[1]) for row in rows)


def test_two_conversions_with_their_own_wording_each_keep_their_printed_fee(
    ledger: Ledger,
) -> None:
    # The statement's wording names each conversion's amounts, so each fee line is
    # one conversion's.
    register(ledger, FIG)
    usd_rate(ledger, "1.3600")
    ledger.file(
        "fig/statement-2054-07-31.csv",
        fig_csv(
            *converted("FG-X", DAY, "723.18", "USD", "946.33", "CAD",
                       "0.53"),
            *converted("FG-X", DAY, "504.01", "USD", "665.99", "CAD",
                       "0.39"),
        ),
    )  # fmt: skip

    ledger.derive()

    assert fee_owners(ledger) == [
        (Decimal("-723.18"), Decimal("-0.53")),
        (Decimal("-504.01"), Decimal("-0.39")),
    ]
    assert fee_lines(ledger, DAY) == [
        ("document_line", Decimal("-0.53"), "USD"),
        ("document_line", Decimal("-0.39"), "USD"),
    ]


@pytest.mark.parametrize("swapped", [False, True], ids=["in-order", "swapped"])
@pytest.mark.parametrize("second_fee", ["-0.41", None], ids=["both", "one"])
def test_same_wording_conversions_on_one_day_leave_the_printed_fees_no_ones(
    ledger: Ledger, second_fee: str | None, swapped: bool
) -> None:
    # Two conversions print the same wording on one day, so a fee line cannot be told
    # to be either one's: no edge names one, and neither books a derived fee (each
    # has a positive spread, 35.30 and 17.65), since a printed fee is the only fee line
    # of its date. Whatever the order of the rows, the result is the same.
    register(ledger, HAZEL)
    usd_rate(ledger, "1.2500")
    second: HazelRow = (
        (DAY, CONVERSION, "CAD", "-504.01")
        if second_fee is None
        else (DAY, CONVERSION, "CAD", "-504.01", second_fee)
    )
    first: list[HazelRow] = [
        (DAY, CONVERSION, "CAD", "-1008.02", "-1.05"),
        (DAY, CONVERSION, "USD", "778.18"),
    ]
    later: list[HazelRow] = [second, (DAY, CONVERSION, "USD", "389.09")]
    rows = later + first if swapped else first + later
    ledger.file("hazel/activity-2054-07-31.csv", hazel_csv(*rows))  # fmt: skip

    ledger.derive()

    assert fee_owners(ledger) == [
        (Decimal("-1008.02"), None),
        (Decimal("-504.01"), None),
    ]
    assert [applied for _, _, applied, _ in edges(ledger)] == [True, True]
    printed = [("document_line", Decimal("-1.05"), "CAD")]
    if second_fee is not None:
        printed.insert(0, ("document_line", Decimal(second_fee), "CAD"))
    assert fee_lines(ledger, DAY) == sorted(printed)


# --- ambiguity, the window and other accounts -----------------------------------------


def test_two_candidate_in_legs_leave_every_leg_ambiguous(ledger: Ledger) -> None:
    register(ledger, FIG)
    usd_rate(ledger, "1.3600")
    out, first = converted("FG-X", DAY, "723.18", "USD", "946.33", "CAD", "0.53")
    # A second in-leg the next day, from another conversion whose out-leg is not here.
    _, second = converted(
        "FG-X", DAY + dt.timedelta(days=1), "723.18", "USD", "946.33",
        "CAD",
    )  # fmt: skip
    ledger.file("fig/statement-2054-07-31.csv", fig_csv(out, first, second))

    ledger.derive()

    assert legs(ledger) == [
        left(DAY, "-723.18", "USD", "ambiguous"),
        left(DAY, "946.33", "CAD", "ambiguous"),
        left(DAY + dt.timedelta(days=1), "946.33", "CAD", "ambiguous"),
    ]
    assert [applied for _, _, applied, _ in edges(ledger)] == [False, False]
    # Both candidates are the one out-leg's, so both name its printed fee line.
    assert fee_owners(ledger) == [(Decimal("-723.18"), Decimal("-0.53"))] * 2
    assert fee_lines(ledger, DAY) == [("document_line", Decimal("-0.53"), "USD")]


@pytest.mark.parametrize(("gap", "edge"), [(1, True), (2, False)], ids=["1", "2"])
def test_the_legs_post_at_most_the_conversion_window_apart(
    ledger: Ledger, gap: int, edge: bool
) -> None:
    register(ledger, HAZEL)
    usd_rate(ledger, "1.2500")
    ledger.file(
        "hazel/activity-2054-07-31.csv",
        hazel_csv((DAY, CONVERSION, "CAD", "-1008.02"),
                   (DAY + dt.timedelta(days=gap), CONVERSION, "USD", "778.18")),
    )  # fmt: skip

    ledger.derive()

    assert len(edges(ledger)) == int(edge)


def test_conversion_legs_on_two_accounts_are_never_a_conversion(
    ledger: Ledger,
) -> None:
    # A CAD out-leg on Fig and a USD in-leg on Hazel: no conversion edge. The
    # transfer rule handles them (here they net inside its tolerance).
    register(ledger, FIG, HAZEL)
    usd_rate(ledger, "1.2500")
    out, _ = converted("FG-X", DAY, "1008.02", "CAD", "803.77", "USD")
    ledger.file("fig/statement-2054-07-31.csv", fig_csv(out))
    ledger.file(
        "hazel/activity-2054-07-31.csv",
        hazel_csv((DAY, CONVERSION, "USD", "803.77")),
    )

    ledger.derive()

    assert edges(ledger) == []
    kinds = ledger.conn.scalars(select(pair_edge.c.kind)).all()
    assert kinds == ["own_transfer"]
    assert fee_lines(ledger, DAY) == [("derived_fee", Decimal("-3.31"), "CAD")]


def test_two_legs_in_one_currency_are_never_a_conversion(ledger: Ledger) -> None:
    register(ledger, HAZEL)
    ledger.file(
        "hazel/activity-2054-07-31.csv",
        hazel_csv((DAY, CONVERSION, "CAD", "-44.64"),
                   (DAY, CONVERSION, "CAD", "44.64")),
    )  # fmt: skip

    ledger.derive()

    assert edges(ledger) == []
    assert legs(ledger) == [
        left(DAY, "-44.64", "CAD", "unresolved"),
        left(DAY, "44.64", "CAD", "unresolved"),
    ]


def test_a_printed_fee_is_never_a_leg_even_when_a_conversion_row_matches_it(
    ledger: Ledger,
) -> None:
    # The owner's ``conversion`` row matches the fee line's wording too. The fee
    # (0.53 USD, 0.72 CAD) would sit inside the spread tolerance of a lone 0.71 CAD
    # in-leg the same day; it stays a fee line that counts once, and the lone in-leg
    # is unresolved.
    register(ledger, FIG)
    ledger.manual(TRANSFER_HINTS_FILE, "converted,conversion,")
    usd_rate(ledger, "1.3600")
    pair = converted("FG-X", DAY, "723.18", "USD", "946.33", "CAD", "0.53")
    _, lone = converted("FG-X", DAY, "0.53", "USD", "0.71", "CAD")
    ledger.file("fig/statement-2054-07-31.csv", fig_csv(*pair, lone))

    ledger.derive()

    assert legs(ledger) == [
        paired(DAY, "-723.18", "USD"),
        left(DAY, "0.71", "CAD", "unresolved"),
        paired(DAY, "946.33", "CAD"),
    ]
    assert fee_lines(ledger, DAY) == [("document_line", Decimal("-0.53"), "USD")]
    assert counted(ledger, txn.c.amount < 0).sum == Decimal("-0.72")


def test_a_printed_conversion_fee_is_never_paired_away_by_a_small_transfer_like_line(
    ledger: Ledger,
) -> None:
    # A Fig conversion prints a fee of 0.53 USD (0.72 CAD at 1.3600). The same day a
    # Hazel bank deposit of 1.05 CAD, transfer-like by its wording, sits inside the
    # move tolerance of that fee. The fee line is no end of a transfer: it stays
    # the date's one fee line and counts once, and the deposit is unresolved.
    register(ledger, FIG, HAZEL)
    usd_rate(ledger, "1.3600")
    ledger.file(
        "fig/statement-2054-07-31.csv",
        fig_csv(*converted("FG-X", DAY, "723.18", "USD", "946.33",
                            "CAD", "0.53")),
    )  # fmt: skip
    ledger.file(
        "hazel/activity-2054-07-31.csv",
        hazel_csv((DAY, "HZ LOAD", "CAD", "1.05")),
    )

    ledger.derive()

    assert fee_lines(ledger, DAY) == [("document_line", Decimal("-0.53"), "USD")]
    assert ledger.conn.scalars(select(pair_edge.c.kind)).all() == ["conversion"]
    assert left(DAY, "1.05", "CAD", "unresolved") in legs(ledger)
    assert counted(ledger, txn.c.amount < 0).sum == Decimal("-0.72")


def test_a_bank_funded_currency_purchase_pairs_the_pull_and_the_conversion_apart(
    ledger: Ledger,
) -> None:
    # The bank pulls 1008.02 CAD into Hazel, Hazel converts it to 777.89 USD (995.70
    # CAD at 1.2800) and pays a USD merchant. The in-leg sits inside the transfer
    # tolerance of the pull too, but a line with a conversion edge is no end of an own
    # transfer: the pull pairs with the deposit, the legs with each other, and nothing
    # is ambiguous. Spending is the payment and the derived spread, 1008.02 in all.
    register(ledger, CHEQUING, HAZEL)
    usd_rate(ledger, "1.2800")
    ledger.file(
        "alder/export.csv",
        export((CSV_IDENTIFIER, DAY, HAZEL_PULL, Decimal("-1008.02"))),
    )
    ledger.file(
        "hazel/activity-2054-07-31.csv",
        hazel_csv((DAY, "HZ LOAD", "CAD", "1008.02"),
                   (DAY, CONVERSION, "CAD", "-1008.02"),
                   (DAY, CONVERSION, "USD", "777.89"),
                   (DAY, "HZ SPEND", "USD", "-777.89")),
    )  # fmt: skip

    ledger.derive()

    states = ledger.conn.execute(
        select(txn.c.account_id, txn.c.amount, txn.c.currency, txn.c.transfer_state,
               txn.c.pair_kind)
        .where(txn.c.origin == "document_line")
    ).all()  # fmt: skip
    cheq, hazel = account_id("chequing"), account_id("hazel")
    assert sorted(tuple(row) for row in states) == sorted(
        [
            (cheq, Decimal("-1008.02"), "CAD", "paired", "own_transfer"),
            (hazel, Decimal("1008.02"), "CAD", "paired", "own_transfer"),
            (hazel, Decimal("-1008.02"), "CAD", "paired", "conversion"),
            (hazel, Decimal("777.89"), "USD", "paired", "conversion"),
            (hazel, Decimal("-777.89"), "USD", None, None),
        ]
    )
    kinds = ledger.conn.execute(select(pair_edge.c.kind, pair_edge.c.applied)).all()
    assert sorted(tuple(row) for row in kinds) == [
        ("conversion", True),
        ("own_transfer", True),
    ]
    assert fee_lines(ledger, DAY) == [("derived_fee", Decimal("-12.32"), "CAD")]
    assert counted(ledger, txn.c.amount < 0).sum == Decimal("-1008.02")


# --- no stored rate -------------------------------------------------------------------


def test_a_conversion_with_no_stored_rate_pairs_with_no_fee_until_a_rate_exists(
    ledger: Ledger,
) -> None:
    # USD to EUR, neither currency with a rate: the wording settles the pair, both
    # legs sit in the unconverted count and no fee line is made. Once the rates are
    # stored, the next derive measures the spread and books it.
    register(ledger, FIG)
    ledger.file(
        "fig/statement-2054-07-31.csv",
        fig_csv(*converted("FG-X", DAY, "723.18", "USD", "658.78",
                            "EUR")),
    )  # fmt: skip

    ledger.derive()

    assert legs(ledger) == [
        paired(DAY, "-723.18", "USD"),
        paired(DAY, "658.78", "EUR"),
    ]
    assert edges(ledger) == [(Decimal("-723.18"), Decimal("658.78"), True, None)]
    assert fee_lines(ledger, DAY) == []
    assert cad_total(ledger.uow, [txn.c.transfer_like]).unconverted_count == 2

    usd_rate(ledger, "1.3600")
    store_rates(ledger, "EUR", (DAY, "1.4900"))
    cover(ledger, "FXEURCAD", (DAY, DAY + dt.timedelta(days=30)))
    ledger.derive()

    # 983.52 CAD out, 981.58 CAD in: a spread of 1.94.
    assert edges(ledger) == [
        (Decimal("-723.18"), Decimal("658.78"), True, Decimal("1.94"))
    ]
    assert fee_lines(ledger, DAY) == [("derived_fee", Decimal("-1.94"), "CAD")]
    assert cad_total(ledger.uow, [txn.c.transfer_like]).unconverted_count == 0


def test_a_cad_to_usd_conversion_with_no_rate_counts_only_its_foreign_leg(
    ledger: Ledger,
) -> None:
    # 1008.02 CAD to 141.50 USD with no USD rate: the wording pairs the legs, no
    # spread is measured and no fee line is made. The CAD out-leg is native, never
    # unconverted (FR-14), so the unconverted count is the USD in-leg alone.
    register(ledger, HAZEL)
    ledger.file(
        "hazel/activity-2054-07-31.csv",
        hazel_csv((DAY, CONVERSION, "CAD", "-1008.02"),
                   (DAY, CONVERSION, "USD", "778.18")),
    )  # fmt: skip

    ledger.derive()

    assert legs(ledger) == [
        paired(DAY, "-1008.02", "CAD"),
        paired(DAY, "778.18", "USD"),
    ]
    assert edges(ledger) == [(Decimal("-1008.02"), Decimal("778.18"), True, None)]
    assert fee_lines(ledger, DAY) == []
    assert cad_total(ledger.uow, [txn.c.transfer_like]).unconverted_count == 1
