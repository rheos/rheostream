"""The Sumac monthly statement (PDF layout text), control ``summary`` (FR-3, FR-8,
FR-19, FR-20).

The layout's wording (the cycle and closing-date labels, the summary labels, the
section headings, the section total, the payment-information labels) and the masked
card's shape come from the owner's identities file (``identities.real`` and
``identities.layout``); the names below are the invented ones the synthetic fixture
prints.

- The cycle line ``Sumac Cycle: MM/DD/YY to MM/DD/YY`` and ``Sumac Cycle Close Date
  <Month> <day>, <year>``, which is the period's end and the statement date. The
  masked card prints beside the cardholder's name, which is read past.
- The summary: ``Sumac Prior Cycle Balance``, the payments figure (``Sumac Paid In``)
  and ``Sumac New Total``, a credit printed ``-$<amount>``; the other summary lines are
  read past. The payment information prints ``Sumac Least Payment`` and ``Sumac Least
  Payment Date``; a statement may print no minimum.
- Transactions, under all-capitals section headings: ``MM/DD/YY   MM/DD/YY
  <description>   <reference>   $<amount>``, the transaction date then the posting
  date, a four-digit reference number in a column of its own where the line has one, a
  credit printed ``-$<amount>``. Each section ends with its total line (``Sumac Sum
  $<amount>``). Every other line is read past.

The reference is told from the description by its column gap (two or more spaces). A
store number the description carries after a single space stays in it. The limit: a
line with no reference whose description's last column is a four-digit number would
lose that number.

One CAD ``statement_period`` per statement, signed and filled as ``parsers.card`` says.
``total_payments`` is the printed payments figure. The registry ties the balances out to
the cent. The debit and credit totals are left unset, since a summary figure may net a
refund into its purchases. Instead the parser ties every printed section total to its
rows and the summary's payments figure to the payments section's rows; a difference of a
cent rejects the statement (``summary_mismatch``).

Text that is not this layout is ``NotRecognised``.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from rheo_finance.identities import layout
from rheo_finance.identities import real as _real
from rheo_finance.identities import real_re as _real_re
from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedLine,
    ParsedPeriod,
    ParseResult,
    Rejected,
)
from rheo_finance.parsers.card import (
    ACCOUNT_KIND,
    AMOUNT_TEXT,
    CURRENCY,
    amount,
    card_account,
    one_value,
    written_date,
)
from rheo_finance.parsers.hints import with_hints
from rheo_finance.parsers.registry import Parser, register_parser

ISSUER: Final = "sumac"
INSTITUTION: Final = "Sumac"

_SLASHED: Final = r"\d\d/\d\d/\d\d"
# Every label and its value sit on one line: [ \t] never crosses a line end.
_PERIOD: Final = re.compile(
    _real_re(rf"Sumac Cycle:[ \t]+({_SLASHED}) to ({_SLASHED})")
)
_CLOSING: Final = re.compile(
    _real_re(r"Sumac Cycle Close Date[ \t]+([A-Z][a-z]+ \d{1,2}, \d{4})")
)
_SIGNED: Final = rf"(-)?\$({AMOUNT_TEXT})\b"
# The reference is a column of its own: two or more spaces before it. A store number
# the description carries after a single space stays in the description.
_ROW: Final = re.compile(
    rf"^\s*({_SLASHED})\s+({_SLASHED})\s+(.*?)(?: {{2,}}\d{{4}})?\s+(-)?\$"
    rf"({AMOUNT_TEXT})\s*$"
)
_TOTAL: Final = re.compile(_real_re(rf"^[ \t]*Sumac Sum[ \t]+{_SIGNED}[ \t]*$"))
_HEADING: Final = re.compile(r"^[ \t]*([A-Z][A-Z &/-]*[A-Z])[ \t]*$")
_TABLE_START: Final = _real("Sumac prior cycle balance")
# Read through ``identities.real``.
PAYMENTS_SECTION: Final = "SUMAC CREDITS"
PAYMENTS_LABEL: Final = "Sumac Paid In"


def _card() -> re.Pattern[str]:
    masked = r"\*{4} \*{4} \*{4} (\d{4})\b"
    return re.compile(layout("sumac.statement.card", masked))


@dataclass(frozen=True)
class _Statement:
    start: dt.date
    end: dt.date
    four: str
    previous: Decimal
    balance: Decimal
    payments: Decimal
    minimum: Decimal | None
    due: dt.date


def _slashed(text: str) -> dt.date:
    return written_date(text, "%m/%d/%y")


def _signed(match: re.Match[str]) -> Decimal:
    value = amount(match.group(2))
    return -value if match.group(1) else value


def _labelled(text: str, label: str, *, optional: bool = False) -> Decimal | None:
    pattern = re.compile(rf"^[ \t]*{label}[ \t]+{_SIGNED}", re.MULTILINE)
    values = [_signed(match) for match in pattern.finditer(text)]
    if optional and not values:
        return None
    return one_value(values)


def _period(text: str) -> tuple[dt.date, dt.date]:
    found = [match.groups() for match in _PERIOD.finditer(text)]
    if not found:
        raise NotRecognised("not a Sumac statement")
    first, last = one_value(found)
    start, end = _slashed(first), _slashed(last)
    closing = [
        written_date(match.group(1), "%B %d, %Y") for match in _CLOSING.finditer(text)
    ]
    if start > end or one_value(closing) != end:
        raise NotRecognised("not a Sumac statement")
    return start, end


def _statement(text: str) -> _Statement:
    start, end = _period(text)
    previous = _labelled(text, _real("Sumac Prior Cycle Balance"))
    balance = _labelled(text, _real("Sumac New Total"))
    payments = _labelled(text, _real_re(PAYMENTS_LABEL))
    minimum = _labelled(text, _real_re(r"Sumac Least Payment"), optional=True)
    due = re.compile(
        _real_re(r"Sumac Least Payment Date[ \t]+([A-Z][a-z]+ \d{1,2}, \d{4})")
    )
    if previous is None or balance is None or payments is None:
        raise NotRecognised("not a Sumac statement")
    return _Statement(
        start=start,
        end=end,
        four=one_value([match.group(1) for match in _card().finditer(text)]),
        previous=previous,
        balance=balance,
        payments=-payments,
        minimum=minimum,
        due=written_date(
            one_value([match.group(1) for match in due.finditer(text)]), "%B %d, %Y"
        ),
    )


def _sections_tie(text: str, lines: list[ParsedLine], payments: Decimal) -> bool:
    """Whether every printed section ``Total`` is the sum of its rows as printed, and
    the summary's payments figure the sum of the payments section's rows.

    A section's rows are those since the table's start (``Sumac prior cycle
    balance``) or the previous ``Total``; its heading is the first all-capitals
    line after that point."""
    rows = iter(lines)
    section: list[Decimal] = []
    heading: str | None = None
    paid = Decimal(0)
    for row in text.splitlines():
        if _ROW.match(row) is not None:
            # As printed: a charge positive, a credit negative.
            section.append(-(next(rows).amount or Decimal(0)))
            continue
        total = _TOTAL.match(row)
        if total is not None:
            if sum(section, Decimal(0)) != _signed(total):
                return False
            if heading == _real(PAYMENTS_SECTION):
                paid -= sum(section, Decimal(0))
            section, heading = [], None
            continue
        if _TABLE_START in row:
            section, heading = [], None
            continue
        named = _HEADING.match(row)
        if named is not None and heading is None and not section:
            heading = named.group(1)
    return paid == payments


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    statement = _statement(text)
    period = (statement.start, statement.end)
    registered = card_account(accounts, INSTITUTION, statement.four, period)
    lines: list[ParsedLine] = []
    for row in text.splitlines():
        match = _ROW.match(row)
        if match is None:
            continue
        txn_date, posting_date, description, credit, printed = match.groups()
        value = amount(printed)
        line = ParsedLine(
            "transaction",
            account=registered,
            posting_date=_slashed(posting_date),
            txn_date=_slashed(txn_date),
            amount=value if credit else -value,
            currency=CURRENCY,
            description_raw=" ".join(description.split()),
        )
        lines.append(with_hints(line, account_kind=ACCOUNT_KIND, issuer=ISSUER))
    if not _sections_tie(text, lines, statement.payments):
        raise Rejected("summary_mismatch", period, (registered,))
    return ParseResult(
        periods=(
            ParsedPeriod(
                account=registered,
                currency=CURRENCY,
                period_start=statement.start,
                period_end=statement.end,
                control_kind="summary",
                opening=-statement.previous,
                closing=-statement.balance,
                total_payments=statement.payments,
                balance_due=statement.balance,
                minimum_due=statement.minimum,
                due_date=statement.due,
            ),
        ),
        lines=tuple(lines),
        control_kind="summary",
    )


def filing_date(text: str) -> dt.date:
    """The statement's closing date, its period end."""
    return _period(text)[1]


PARSER: Final = register_parser(
    Parser(
        parser_id="sumac.statement",
        issuer=ISSUER,
        format="pdf",
        version="1",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
