"""The Boxwood energy bill in the owner's two layouts (PDF) (FR-3 ``bill_identity``,
FR-8, FR-9).

``boxwood.bill`` reads the invented layout SPEC Assumptions names. The owner's own
bills come in two other layouts, read here, one parser each:

- ``boxwood.bill_columns``, the older one: labelled rows with their amounts in
  columns, and values printed beside a label (a due date, the amount due) sitting at
  the end of the line below it;
- ``boxwood.bill_leaders``, the current one: each bill-detail amount follows its
  label after a leader (a run of underscores), and the billing period prints its
  start without a year.

Every shape comes from the owner's identities file (``identities.layout``), one
pattern per field under the parser id (``boxwood.bill_leaders.total`` and so on),
plus the date forms (``.date_format``, ``.start_format``). The defaults below
describe the invented layouts of the synthetic fixtures. The patterns are searched
line by line (``re.MULTILINE``), and each names the group the field is read from:

- ``title``: present, or the text is not this layout (``NotRecognised``);
- ``account`` (group ``number``), ``bill_date`` and ``due`` (group ``date``),
  ``period`` (groups ``start`` and ``end``), ``total`` (group ``amount``): each is
  required, and every match must agree (an account number's digits, whatever
  spaces or dashes split them), so a value the bill prints twice (the payment
  stub's copy) is checked against the other;
- ``previous``, ``forward``, ``current``, ``arrears`` (group ``amount``): optional,
  every match agreeing; ``payment`` (group ``amount``): every match, summed.

An amount reads as ``12,345.67``, with an optional ``$``; a leading ``-`` or a
trailing ``CR`` makes it a credit (negative). A period start printed without a year
(``.start_format`` holds no ``%Y``, and the start does not read in ``.date_format``)
takes the end's year, or the year before when that would put it after the end; a
layout may print the start's year only when the period crosses New Year.

Each bill gives one CAD ``statement_period``, control ``bill_identity``, over the
printed billing period, with no ledger lines, as ``boxwood.bill`` does:
``balance_due`` the amount due, ``due_date``, ``bill_date`` (which also names the
filed copy), ``current_charges`` and ``arrears`` when printed. The identity: the
previous bill amount plus the payments is the balance forward (when printed), and the
previous amount plus the payments plus the current charges is the amount due. A bill
that breaks it is rejected (``identity_mismatch``); one that prints no previous
amount or no current charges loads as printed with ``tied_out`` false.

The bill is matched by the last four digits of its account number at
``INSTITUTION`` (``AccountLookup.by_last_four``); the number is stored nowhere. An
unregistered account rejects it (``unknown_account``).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from rheo_finance.identities import layout
from rheo_finance.manual.accounts import last_four
from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedPeriod,
    ParseResult,
    Rejected,
)
from rheo_finance.parsers.card import AMOUNT_TEXT, card_account
from rheo_finance.parsers.card import amount as _amount
from rheo_finance.parsers.card import written_date as _written_date
from rheo_finance.parsers.registry import Parser, register_parser

INSTITUTION: Final = "Boxwood"
ISSUER: Final = "boxwood"
CURRENCY: Final = "CAD"
_MONEY: Final = re.compile(
    rf"(?P<minus>-)?\$?(?P<amount>{AMOUNT_TEXT})[ \t]*(?P<credit>CR)?"
)
_NUMBER: Final = r"\d[\d -]*\d"
_AMOUNT: Final = r"-?\$?[\d,]+\.\d\d(?:[ \t]?CR)?"
REQUIRED: Final = ("title", "account", "bill_date", "period", "due", "total")
OPTIONAL: Final = ("previous", "payment", "forward", "current", "arrears")


@dataclass(frozen=True)
class _Layout:
    parser_id: str
    shapes: Mapping[str, str]
    date_format: str
    start_format: str

    def shape(self, field: str) -> re.Pattern[str]:
        key = f"{self.parser_id}.{field}"
        return re.compile(layout(key, self.shapes[field]), re.MULTILINE)

    def form(self, name: str) -> str:
        default: str = getattr(self, name)
        return layout(f"{self.parser_id}.{name}", default)


_COLUMN_DATE: Final = r"[A-Z]{3}-\d{2}-\d{4}"
COLUMNS: Final = _Layout(
    parser_id="boxwood.bill_columns",
    shapes={
        "title": r"^Sumd Name:",
        "account": rf"Boxwood ref no:?[ \t]*\n?[ \t]*(?P<number>{_NUMBER})",
        "bill_date": rf"^Bill Day:[ \t]+(?P<date>{_COLUMN_DATE})",
        "period": (
            rf"^Bill Span:[ \t]+(?P<start>{_COLUMN_DATE}) to (?P<end>{_COLUMN_DATE})"
        ),
        "due": rf"Pay By[ \t]*\n[^\n]*?(?P<date>{_COLUMN_DATE})",
        "total": r"Owing Now[ \t]*\n?[^\n$]*\$(?P<amount>[\d,]+\.\d\d)",
        "previous": rf"^Earlier statement[ \t]+(?P<amount>{_AMOUNT})",
        "payment": rf"^Payment got .*?[ \t](?P<amount>{_AMOUNT})(?=[ \t]|$)",
        "forward": rf"^Carried over[ \t]+(?P<amount>{_AMOUNT})",
        "current": rf"^New charges total[ \t]+(?P<amount>{_AMOUNT})",
        "arrears": rf"^Lorem arrears[ \t]+(?P<amount>{_AMOUNT})",
    },
    date_format="%b-%d-%Y",
    start_format="%b-%d-%Y",
)

_LEADER_DATE: Final = r"[A-Z][a-z]{2} \d{1,2}, \d{4}"
_LEADER: Final = r"[ \t]*_*[ \t]*"
LEADERS: Final = _Layout(
    parser_id="boxwood.bill_leaders",
    shapes={
        "title": r"^[ \t]*Boxwood return slip",
        "account": rf"Ref number: (?P<number>{_NUMBER})",
        "bill_date": rf"Bill day: (?P<date>{_LEADER_DATE})",
        "period": (
            rf"Bill span: (?P<start>[A-Z][a-z]{{2}} \d{{1,2}}(?:, \d{{4}})?)"
            rf"-(?P<end>{_LEADER_DATE})"
        ),
        "due": rf"(?:Due [A-Z][a-z]+day, |Pay by: )(?P<date>{_LEADER_DATE})",
        "total": rf"(?:Settle{_LEADER}|Owing: )(?P<amount>{_AMOUNT})",
        "previous": rf"Prior bill \([^)\n]*\){_LEADER}(?P<amount>{_AMOUNT})",
        "payment": rf"Paid in \([^)\n]*\){_LEADER}(?P<amount>{_AMOUNT})",
        "forward": rf"Carried from prior bill{_LEADER}(?P<amount>{_AMOUNT})",
        "current": rf"This bill total{_LEADER}(?P<amount>{_AMOUNT})",
        "arrears": rf"Lorem arrears{_LEADER}(?P<amount>{_AMOUNT})",
    },
    date_format="%b %d, %Y",
    start_format="%b %d",
)


def _money(text: str) -> Decimal:
    match = _MONEY.fullmatch(text.strip())
    if match is None or (match["minus"] and match["credit"]):
        raise NotRecognised("not a Boxwood amount")
    value = _amount(match["amount"])
    return -value if match["minus"] or match["credit"] else value


def _values(bill: _Layout, text: str, field: str, group: str) -> list[str]:
    try:
        return [m[group] for m in bill.shape(field).finditer(text)]
    except IndexError:
        raise NotRecognised(f"a Boxwood {field} pattern names no {group}") from None


def _one(bill: _Layout, text: str, field: str, group: str) -> str:
    found = _values(bill, text, field, group)
    if not found:
        raise NotRecognised(f"no Boxwood {field}")
    if any(each != found[0] for each in found):
        raise NotRecognised(f"the Boxwood {field} values disagree")
    return found[0]


def _optional(bill: _Layout, text: str, field: str) -> Decimal | None:
    found = {_money(each) for each in _values(bill, text, field, "amount")}
    if len(found) > 1:
        raise NotRecognised(f"the Boxwood {field} values disagree")
    return found.pop() if found else None


def _date(bill: _Layout, value: str) -> dt.date:
    return _written_date(value, bill.form("date_format"))


def _has_year(text: str, form: str) -> bool:
    try:
        _written_date(text, form)
    except NotRecognised:
        return False
    return True


def _recognised(bill: _Layout, text: str) -> None:
    if bill.shape("title").search(text) is None:
        raise NotRecognised(f"not a {bill.parser_id} layout")


def _bill_date(bill: _Layout, text: str) -> dt.date:
    _recognised(bill, text)
    return _date(bill, _one(bill, text, "bill_date", "date"))


def _period(bill: _Layout, text: str) -> tuple[dt.date, dt.date]:
    matches = {(m["start"], m["end"]) for m in bill.shape("period").finditer(text)}
    if len(matches) != 1:
        raise NotRecognised("no single Boxwood billing period")
    ((start_text, end_text),) = matches
    end = _date(bill, end_text)
    start_format = bill.form("start_format")
    if "%Y" in start_format:
        start = _written_date(start_text, start_format)
    elif _has_year(start_text, bill.form("date_format")):
        start = _written_date(start_text, bill.form("date_format"))
    else:
        start = _written_date(f"{start_text} {end.year}", f"{start_format} %Y")
        if start > end:
            start = _written_date(f"{start_text} {end.year - 1}", f"{start_format} %Y")
    if end < start:
        raise NotRecognised("a billing period that ends before it starts")
    return start, end


def _tied_out(bill: _Layout, text: str, total: Decimal) -> bool | None:
    """True when the bill's identity holds, None when it prints none, False when it
    is broken."""
    previous = _optional(bill, text, "previous")
    current = _optional(bill, text, "current")
    if previous is None or current is None:
        return None
    payments = (_money(each) for each in _values(bill, text, "payment", "amount"))
    forward = previous + sum(payments, Decimal(0))
    printed_forward = _optional(bill, text, "forward")
    if printed_forward is not None and printed_forward != forward:
        return False
    return forward + current == total


def _parse(bill: _Layout, text: str, accounts: AccountLookup) -> ParseResult:
    bill_date = _bill_date(bill, text)
    period = _period(bill, text)
    total = _money(_one(bill, text, "total", "amount"))
    due = _date(bill, _one(bill, text, "due", "date"))
    numbers = {
        re.sub(r"\D", "", each) for each in _values(bill, text, "account", "number")
    }
    if len(numbers) != 1:
        raise NotRecognised("no single Boxwood account number")
    four = last_four(numbers.pop())
    if four is None:
        raise NotRecognised("no account number")
    registered = card_account(accounts, INSTITUTION, four, period)
    tied_out = _tied_out(bill, text, total)
    if tied_out is False:
        raise Rejected("identity_mismatch", period, (registered,))
    return ParseResult(
        periods=(
            ParsedPeriod(
                account=registered,
                currency=CURRENCY,
                period_start=period[0],
                period_end=period[1],
                control_kind="bill_identity",
                balance_due=total,
                due_date=due,
                bill_date=bill_date,
                current_charges=_optional(bill, text, "current"),
                arrears=_optional(bill, text, "arrears"),
                tied_out=bool(tied_out),
            ),
        ),
        lines=(),
        control_kind="bill_identity",
    )


def parse_columns(text: str, accounts: AccountLookup) -> ParseResult:
    return _parse(COLUMNS, text, accounts)


def parse_leaders(text: str, accounts: AccountLookup) -> ParseResult:
    return _parse(LEADERS, text, accounts)


def filing_date_columns(text: str) -> dt.date:
    """The bill date, the date the filer names a new copy by."""
    return _bill_date(COLUMNS, text)


def filing_date_leaders(text: str) -> dt.date:
    """The bill date, the date the filer names a new copy by."""
    return _bill_date(LEADERS, text)


PARSER_COLUMNS: Final = register_parser(
    Parser(
        parser_id=COLUMNS.parser_id,
        issuer=ISSUER,
        format="pdf",
        version="4",
        parse=parse_columns,
        filing_date=filing_date_columns,
    )
)
PARSER_LEADERS: Final = register_parser(
    Parser(
        parser_id=LEADERS.parser_id,
        issuer=ISSUER,
        format="pdf",
        version="5",
        parse=parse_leaders,
        filing_date=filing_date_leaders,
    )
)
