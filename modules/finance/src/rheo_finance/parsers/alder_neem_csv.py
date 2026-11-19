"""The Alder Neem transaction export: a headerless CSV, control ``none`` (FR-3, FR-8).

The synthetic fixture's invented layout has six columns: the description, the
posting date, the credit, the debit, the masked card number and the transaction date
(``2024-01-31``). Exactly one of the debit and the credit holds an amount. The owner's
identities file gives his own exports' order and shapes (``identities.layout``): its
column list names each role, may add a ``currency`` column (which must read CAD),
and marks a column the parser reads past with ``=``. The export lists rows newest
first by transaction date; lines are kept oldest first.

- The masked card is restricted, and a column read past is stored nowhere: not on a
  line, not as a counterparty, not in a message. The card is matched by its last four
  at ``INSTITUTION`` (``parsers.card.card_account``) and goes no further. A card the
  registry does not hold rejects the file (``unknown_account``).
- Each account gives one CAD ``statement_period`` from its earliest posting date to
  its latest, control kind ``none``: the export has no totals and no balance, and its
  date range proves nothing about completeness.
- A debit is negative, a credit positive (``parsers.card``'s sign convention).

A file that is not this layout (another column count, a date or an amount that does
not read, a row with both or neither amount, a currency column other than CAD, rows in
neither transaction-date order, a CSV the reader cannot read) is ``NotRecognised``.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from rheo_finance.identities import layout
from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedLine,
    ParsedPeriod,
    ParseResult,
    RegisteredAccount,
)
from rheo_finance.parsers.card import (
    ACCOUNT_KIND,
    CURRENCY,
    amount,
    card_account,
    written_date,
)
from rheo_finance.parsers.hints import with_hints
from rheo_finance.parsers.registry import Parser, register_parser

ISSUER: Final = "alder-neem"
INSTITUTION: Final = "Alder"
# What each column holds, in the invented order of the synthetic fixture.
ROLES: Final = ("description", "posted", "credit", "debit", "card", "txn_date")
# A role the owner's layout may add.
EXTRA: Final = ("currency",)
_SHAPES: Final = {
    "alder_neem.csv.card": r"XXXX-(\d{4})",
    "alder_neem.csv.posted": r"(\d{4}-\d\d-\d\d)",
}


def _shape(key: str) -> re.Pattern[str]:
    return re.compile(layout(key, _SHAPES[key]))


def _roles() -> tuple[dict[str, int], int]:
    """Each role's column index, and the column count."""
    order = layout("alder_neem.csv.columns", ROLES)
    named = [each for each in order if not each.startswith("=")]
    if len(set(named)) != len(named) or not set(ROLES) <= set(named) <= {
        *ROLES,
        *EXTRA,
    }:
        raise NotRecognised("not an Alder Neem export")
    return {role: index for index, role in enumerate(order) if role in named}, len(
        order
    )


@dataclass(frozen=True)
class _Row:
    four: str
    txn_date: dt.date
    posting_date: dt.date
    description: str
    amount: Decimal


def _row(cells: list[str], at: dict[str, int]) -> _Row:
    card = _shape("alder_neem.csv.card").fullmatch(cells[at["card"]].strip())
    posted = _shape("alder_neem.csv.posted").fullmatch(cells[at["posted"]].strip())
    currency = cells[at["currency"]].strip() if "currency" in at else CURRENCY
    if card is None or posted is None or currency != CURRENCY:
        raise NotRecognised("not an Alder Neem export")
    debit, credit = cells[at["debit"]].strip(), cells[at["credit"]].strip()
    if bool(debit) == bool(credit):
        raise NotRecognised("not an Alder Neem export")
    value = amount(credit) if credit else -amount(debit)
    return _Row(
        four=card.group(1),
        txn_date=written_date(cells[at["txn_date"]], "%Y-%m-%d"),
        posting_date=written_date(posted.group(1), "%Y-%m-%d"),
        description=" ".join(cells[at["description"]].split()),
        amount=value,
    )


def _rows(text: str) -> list[_Row]:
    try:
        # A cell over the reader's size limit raises csv.Error, not ValueError.
        found = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        raise NotRecognised("not an Alder Neem export") from None
    rows: list[_Row] = []
    at, width = _roles()
    for cells in found:
        if not any(cell.strip() for cell in cells):
            continue
        if len(cells) != width:
            raise NotRecognised("not an Alder Neem export")
        rows.append(_row(cells, at))
    if not rows:
        raise NotRecognised("not an Alder Neem export")
    dates = [row.txn_date for row in rows]
    if dates == sorted(dates, reverse=True):
        return rows[::-1]
    if dates == sorted(dates):
        return rows
    raise NotRecognised("not an Alder Neem export")


def _span(rows: list[_Row]) -> tuple[dt.date, dt.date]:
    dates = [row.posting_date for row in rows]
    return min(dates), max(dates)


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    rows = _rows(text)
    by_four: dict[str, list[_Row]] = defaultdict(list)
    for row in rows:
        by_four[row.four].append(row)
    by_account: dict[RegisteredAccount, list[_Row]] = defaultdict(list)
    for four, own in by_four.items():
        by_account[card_account(accounts, INSTITUTION, four, _span(own))] += own
    periods: list[ParsedPeriod] = []
    lines: list[ParsedLine] = []
    for registered, own in by_account.items():
        own.sort(key=lambda row: row.txn_date)
        start, end = _span(own)
        periods.append(
            ParsedPeriod(
                account=registered,
                currency=CURRENCY,
                period_start=start,
                period_end=end,
                control_kind="none",
            )
        )
        lines += [
            with_hints(
                ParsedLine(
                    "transaction",
                    account=registered,
                    posting_date=row.posting_date,
                    txn_date=row.txn_date,
                    amount=row.amount,
                    currency=CURRENCY,
                    description_raw=row.description,
                ),
                account_kind=ACCOUNT_KIND,
                issuer=ISSUER,
            )
            for row in own
        ]
    return ParseResult(periods=tuple(periods), lines=tuple(lines), control_kind="none")


def filing_date(text: str) -> dt.date:
    """The latest posting date, the date the filer names a new copy by."""
    return _span(_rows(text))[1]


PARSER: Final = register_parser(
    Parser(
        parser_id="alder_neem.csv",
        issuer=ISSUER,
        format="csv",
        version="1",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
