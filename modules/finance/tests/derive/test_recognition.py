"""``derive.run``: recognition date and tax year (stage 5, FR-16, AC-18 in part).

Seams: ``run`` over fixture documents and a manual line, read back from ``txn``. Every
value is synthetic. The card payment's pairing (that it counts in neither year's
spending) arrives with the transfer stages; here the purchase's year is pinned.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from rheo_finance.storage.schema import txn

from .support import CARD_IDENTIFIER, CARD_STATEMENT, Ledger, csv_row

D = dt.date


def recognized(ledger: Ledger) -> dict[str, tuple[dt.date | None, int | None]]:
    rows = ledger.conn.execute(
        select(txn.c.description_norm, txn.c.recognition_date, txn.c.tax_year)
    )
    return {
        str(row.description_norm): (row.recognition_date, row.tax_year) for row in rows
    }


def test_a_card_purchase_on_30_december_paid_in_january_counts_in_the_earlier_year(
    ledger: Ledger, card_parsers: None
) -> None:
    ledger.card()
    ledger.file(
        "synthetic-card/2042-01-28.pdf",
        "\n".join(
            [
                CARD_STATEMENT,
                f"ACCOUNT {CARD_IDENTIFIER}",
                "PERIOD 2041-12-25 2042-01-28",
                "OPENING 0.00",
                "CLOSING 0.00",
                # Bought on 30 Dec, posted on 8 Jan.
                "LINE 2042-01-08 2041-12-30 -34.57 SAMPLE GROCER 99921",
                # The payment in January.
                "LINE 2042-01-21 - 34.57 PAID IN NOTED 99922",
            ]
        ),
    )

    ledger.derive()

    found = recognized(ledger)
    assert found["sample grocer 99921"] == (D(2041, 12, 30), 2041)
    # No transaction date: the posting date.
    assert found["paid in noted 99922"] == (D(2042, 1, 21), 2042)


def test_a_bank_line_is_recognized_on_its_posting_date(ledger: Ledger) -> None:
    ledger.chequing()
    ledger.file(
        "alder/export.csv",
        "\n".join(
            [
                csv_row(
                    "2041/12/31", "DEPOSIT 99931", into="109.02", balance="1199.22"
                ),
                csv_row("2042/01/08", "PURCHASE 99932", out="5.00", balance="1194.22"),
            ]
        ),
    )
    ledger.cash("paid-outside,2041-12-31,-11.50,CAD,coffee money,")

    ledger.derive()

    found = recognized(ledger)
    assert found["deposit 99931"] == (D(2041, 12, 31), 2041)
    assert found["purchase 99932"] == (D(2042, 1, 8), 2042)
    assert found["coffee money"] == (D(2041, 12, 31), 2041)
