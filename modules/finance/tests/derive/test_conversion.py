"""Derivation stage 4, conversion to CAD, and ``ledger.totals.cad_total`` (FR-14,
AC-15).

Seams: ``derive.run`` over ``FX_LINES`` documents with rates stored in ``rate`` and the
fetched ranges in ``rate_coverage`` (as a fetch leaves them), read back from ``txn``;
``cad_total`` over the derived txns. Every date, amount and rate is invented.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import insert, select

from rheo_finance.ledger.totals import cad_total
from rheo_finance.rates import LEGACY_NOON_SERIES
from rheo_finance.storage.schema import rate, rate_coverage, txn

from .support import NOW, Ledger, fx_lines

D = dt.date
LEFT_OUT = (None, None, None, None, "unconverted")


def store_rates(ledger: Ledger, base: str, *rows: tuple[dt.date, str]) -> None:
    ledger.conn.execute(
        insert(rate),
        [
            {
                "rate_date": day,
                "base": base,
                "quote": "CAD",
                "rate": Decimal(value),
                "source": "rowan",
                "fetched_at": NOW,
            }
            for day, value in rows
        ],
    )


def cover(ledger: Ledger, series: str, *ranges: tuple[dt.date, dt.date]) -> None:
    ledger.conn.execute(
        insert(rate_coverage),
        [
            {
                "series": series,
                "source": "rowan",
                "covered_from": first,
                "covered_to": last,
                "fetched_at": NOW,
            }
            for first, last in ranges
        ],
    )


def converted(ledger: Ledger) -> dict[str, tuple[object, ...]]:
    rows = ledger.conn.execute(
        select(
            txn.c.description_norm,
            txn.c.cad_amount,
            txn.c.cad_source,
            txn.c.rate,
            txn.c.rate_date,
            txn.c.conversion_state,
        )
    )
    return {str(row[0]): tuple(row[1:]) for row in rows}


def test_a_usd_line_converts_at_the_stored_rate_of_its_day_half_even(
    fx_ledger: Ledger,
) -> None:
    store_rates(
        fx_ledger, "USD", (D(2046, 12, 11), "1.3725"), (D(2046, 12, 12), "1.3800")
    )
    cover(fx_ledger, "FXUSDCAD", (D(2046, 12, 4), D(2046, 12, 12)))
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines("USD", "2046-12-11 -14 - - SAMPLE SHOP 99941"),
    )

    fx_ledger.derive()

    # 14 x 1.3725 = 19.2150: half-even to the even cent.
    assert converted(fx_ledger)["sample shop 99941"] == (
        Decimal("-19.22"),
        "rowan",
        Decimal("1.3725"),
        D(2046, 12, 11),
        "converted",
    )


def test_a_weekend_line_inside_a_covered_range_takes_the_business_day_before(
    fx_ledger: Ledger,
) -> None:
    store_rates(
        fx_ledger, "USD", (D(2046, 12, 13), "1.3500"), (D(2046, 12, 16), "1.4000")
    )
    cover(fx_ledger, "FXUSDCAD", (D(2046, 12, 7), D(2046, 12, 16)))
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines("USD", "2046-12-14 -110.06 - - SAMPLE SHOP 99942"),
    )

    fx_ledger.derive()

    assert converted(fx_ledger)["sample shop 99942"] == (
        Decimal("-148.58"),
        "rowan",
        Decimal("1.3500"),
        D(2046, 12, 13),
        "converted",
    )


def test_adjacent_covered_ranges_carry_a_rate_across_their_join(
    fx_ledger: Ledger,
) -> None:
    # Two fetches: the first covered to Sunday, the second from the Monday holiday.
    store_rates(
        fx_ledger, "USD", (D(2046, 12, 6), "1.3500"), (D(2046, 12, 10), "1.3600")
    )
    cover(
        fx_ledger,
        "FXUSDCAD",
        (D(2046, 11, 29), D(2046, 12, 8)),
        (D(2046, 12, 9), D(2046, 12, 10)),
    )
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines("USD", "2046-12-09 -11.35 - - SAMPLE SHOP 99955"),
    )

    fx_ledger.derive()

    assert converted(fx_ledger)["sample shop 99955"][2:] == (
        Decimal("1.3500"),
        D(2046, 12, 6),
        "converted",
    )


def test_a_line_in_a_gap_or_after_the_last_observation_is_unconverted_and_counted(
    fx_ledger: Ledger,
) -> None:
    # Two fetches two weeks apart, each returning one Monday: the days between were
    # never fetched.
    store_rates(
        fx_ledger,
        "USD",
        (D(2046, 12, 1), "1.3000"),
        (D(2046, 12, 16), "1.3200"),
    )
    cover(
        fx_ledger,
        "FXUSDCAD",
        (D(2046, 11, 25), D(2046, 12, 1)),
        (D(2046, 12, 9), D(2046, 12, 16)),
    )
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines(
            "USD",
            # The Wednesday after the first Monday: in the gap.
            "2046-12-04 -11.35 - - SAMPLE SHOP 99943",
            # After the latest observation fetched.
            "2046-12-17 -22.70 - - SAMPLE SHOP 99944",
            "2046-12-16 -34.05 - - SAMPLE SHOP 99945",
        ),
    )
    fx_ledger.file(
        "synthetic-fx/eur.txt",
        fx_lines("EUR", "2046-12-16 -45.40 - - SAMPLE SHOP 99946"),
    )

    fx_ledger.derive()

    found = converted(fx_ledger)
    assert found["sample shop 99943"] == LEFT_OUT
    assert found["sample shop 99944"] == LEFT_OUT
    assert found["sample shop 99946"] == LEFT_OUT  # no EUR rate at all
    assert found["sample shop 99945"][0] == Decimal("-44.95")
    total = cad_total(fx_ledger.uow)
    assert total.sum == Decimal("-44.95")
    assert total.unconverted_count == 3
    assert total.text() == "-44.95 CAD (3 unconverted)"
    # Filters narrow both figures.
    usd = cad_total(fx_ledger.uow, [txn.c.currency == "USD"])
    assert (usd.sum, usd.unconverted_count) == (Decimal("-44.95"), 2)


def test_a_line_on_the_last_legacy_days_needs_only_legacy_coverage(
    fx_ledger: Ledger,
) -> None:
    legacy = LEGACY_NOON_SERIES["USD"]
    store_rates(fx_ledger, "USD", (D(2016, 12, 30), "1.3400"))
    cover(fx_ledger, legacy, (D(2016, 12, 23), D(2017, 1, 2)))
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines("USD", "2017-01-02 -11.35 - - SAMPLE SHOP 99956"),
    )

    fx_ledger.derive()

    assert converted(fx_ledger)["sample shop 99956"][2:] == (
        Decimal("1.3400"),
        D(2016, 12, 30),
        "converted",
    )


def test_an_issuer_stated_cad_amount_is_never_re_converted(fx_ledger: Ledger) -> None:
    store_rates(fx_ledger, "USD", (D(2046, 12, 11), "1.5000"))
    cover(fx_ledger, "FXUSDCAD", (D(2046, 12, 4), D(2046, 12, 11)))
    # A card line billed in CAD for a USD purchase, and a USD line whose issuer
    # states the CAD amount.
    fx_ledger.file(
        "synthetic-fx/cad.txt",
        fx_lines("CAD", "2046-12-11 -15.04 -15.04 USD SAMPLE CARD SHOP 99947"),
    )
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines("USD", "2046-12-11 -11.75 -15.04 - SAMPLE STATED SHOP 99948"),
    )

    fx_ledger.derive()

    found = converted(fx_ledger)
    assert found["sample card shop 99947"] == (
        Decimal("-15.04"),
        "native",
        None,
        None,
        "native",
    )
    assert found["sample stated shop 99948"] == (
        Decimal("-15.04"),
        "issuer",
        Decimal("1.2800000000"),
        D(2046, 12, 11),
        "converted",
    )


def test_every_total_prints_its_unconverted_count_even_when_zero(
    fx_ledger: Ledger,
) -> None:
    fx_ledger.file(
        "synthetic-fx/cad.txt",
        fx_lines(
            "CAD",
            "2046-12-11 -14.89 - - SAMPLE SHOP 99949",
            "2046-12-12 45.40 - - SAMPLE REFUND 99950",
        ),
    )

    fx_ledger.derive()

    assert {row[4] for row in converted(fx_ledger).values()} == {"native"}
    assert cad_total(fx_ledger.uow).text() == "30.51 CAD (0 unconverted)"
    assert cad_total(fx_ledger.uow, [txn.c.amount < 0]).text() == (
        "-14.89 CAD (0 unconverted)"
    )
