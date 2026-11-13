"""The Boxwood energy bill (PDF) (FR-3 ``bill_identity``, FR-8, FR-9).

Built on a synthetic layout, the one SPEC Assumptions names: an invented fixture with
the fields any energy bill states. A photo of a bill stays ``needs_manual_entry`` (no
image is ever read), and a PDF in a layout this parser does not read stays ``unparsed``
until a version for it is added.

The layout: a ``Boxwood`` title line, then labelled lines (a label, two or more
spaces, the value): ``Account number``, ``Bill date``, ``Billing period`` (``<date>
to <date>``), an account summary (``Previous bill amount``, one ``Box paid
in`` line per payment, ``Balance forward``, ``Current charges``), ``Box total
owing`` and ``Due date``. The payment stub repeats the account number, the
amount due (``Box stub owing``) and the due date; every value printed twice must agree.

- The bill gives one CAD ``statement_period``, control ``bill_identity``, over the
  printed billing period, with ``balance_due`` the total amount due and ``due_date``
  the printed due date, and no ledger lines (a bill feeds obligations, not the
  ledger). The period keeps the bill date (``bill_date``), the obligation's issue
  date, which also names the filed copy (``filing_date``), the current charges and an
  ``Arrears`` line when the bill prints one (stage 14's arrangement rule).
- The identity: the previous bill amount plus the payments is the balance forward
  (when printed), and the previous bill amount plus the payments plus the current
  charges is the total amount due. A bill that breaks it is rejected
  (``identity_mismatch``) with zero lines. A bill that prints no previous bill
  amount or no current charges states no identity and loads as printed, with
  ``tied_out`` false.
- The bill is matched by the last four digits of its account number at
  ``INSTITUTION`` (``AccountLookup.by_last_four``); the number is stored nowhere. An
  unregistered account rejects it (``unknown_account``).

Text without the title, a bill date, a billing period, an account number, the total
amount due or the due date is ``NotRecognised``.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from decimal import Decimal
from typing import Final

from rheo_finance.identities import real
from rheo_finance.identities import real as _real
from rheo_finance.manual.accounts import last_four
from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedPeriod,
    ParseResult,
    Rejected,
)
from rheo_finance.parsers.card import AMOUNT_TEXT, amount, card_account, one_value
from rheo_finance.parsers.card import written_date as _written_date
from rheo_finance.parsers.registry import Parser, register_parser

INSTITUTION: Final = "Boxwood"
ISSUER: Final = "boxwood"
CURRENCY: Final = "CAD"
# The bill's first line starts with the issuer's own name (``identities.real``).
TITLE_NAME: Final = "Boxwood"
_LABELLED: Final = re.compile(r"\s*(?P<label>\S.*?)\s{2,}(?P<value>\S.*?)\s*")
_MONEY: Final = re.compile(rf"(?P<minus>-)?\$(?P<amount>{AMOUNT_TEXT})")
_PERIOD: Final = re.compile(
    r"(?P<start>[A-Z][a-z]{2} \d{1,2}, \d{4}) to (?P<end>[A-Z][a-z]{2} \d{1,2}, \d{4})"
    r"(?:\s{2,}\d+ days)?"
)
_NUMBER: Final = re.compile(r"\d[\d -]*\d")
PAYMENT: Final = "Box paid in"


def _written(text: str) -> dt.date:
    return _written_date(text, "%B %d, %Y")


def _money(text: str) -> Decimal:
    match = _MONEY.fullmatch(text)
    if match is None:
        raise NotRecognised("not a Boxwood amount")
    value = amount(match["amount"])
    return -value if match["minus"] else value


def _labelled(text: str) -> dict[str, list[str]]:
    """Every labelled line's values by label, in order. A payment line's label
    starts with ``Box paid in`` and carries its date; it is filed under that."""
    found: dict[str, list[str]] = defaultdict(list)
    for line in text.splitlines():
        match = _LABELLED.fullmatch(line)
        if match is None:
            continue
        label = match["label"]
        found[PAYMENT if label.startswith(_real(PAYMENT)) else label].append(
            match["value"]
        )
    return found


def _one(found: dict[str, list[str]], label: str) -> str:
    return one_value(found.get(label, []))


def _optional(found: dict[str, list[str]], label: str) -> Decimal | None:
    return _money(_one(found, label)) if label in found else None


def _bill_date(text: str) -> dt.date:
    lines = [line for line in text.splitlines() if line.strip()]
    title = re.compile(rf"\s*{re.escape(real(TITLE_NAME))}\b.*")
    if not lines or title.fullmatch(lines[0]) is None:
        raise NotRecognised("not a Boxwood bill")
    return _written(_one(_labelled(text), "Bill date"))


def _period(found: dict[str, list[str]]) -> tuple[dt.date, dt.date]:
    match = _PERIOD.fullmatch(_one(found, "Billing period"))
    if match is None:
        raise NotRecognised("not a Boxwood billing period")
    start = _written_date(match["start"], "%b %d, %Y")
    end = _written_date(match["end"], "%b %d, %Y")
    if end < start:
        raise NotRecognised("a billing period that ends before it starts")
    return start, end


def _tied_out(found: dict[str, list[str]], total: Decimal) -> bool | None:
    """True when the bill's identity holds, None when it prints none, False when it
    is broken."""
    previous = _optional(found, "Previous bill amount")
    current = _optional(found, "Current charges")
    if previous is None or current is None:
        return None
    forward = previous + sum(
        (_money(each) for each in found.get(PAYMENT, [])), Decimal(0)
    )
    printed_forward = _optional(found, "Balance forward")
    if printed_forward is not None and printed_forward != forward:
        return False
    return forward + current == total


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    bill_date = _bill_date(text)
    found = _labelled(text)
    period = _period(found)
    total = _money(_one(found, _real("Box total owing")))
    stub = _real("Box stub owing")
    if stub in found and _money(_one(found, stub)) != total:
        raise NotRecognised("the payment stub disagrees with the total amount due")
    due = _written(_one(found, "Due date"))
    number = _NUMBER.fullmatch(_one(found, "Account number"))
    four = last_four(number.group(0)) if number else None
    if four is None:
        raise NotRecognised("no account number")
    registered = card_account(accounts, INSTITUTION, four, period)
    tied_out = _tied_out(found, total)
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
                current_charges=_optional(found, "Current charges"),
                arrears=_optional(found, "Arrears"),
                tied_out=bool(tied_out),
            ),
        ),
        lines=(),
        control_kind="bill_identity",
    )


def filing_date(text: str) -> dt.date:
    """The bill date, the date the filer names a new copy by."""
    return _bill_date(text)


PARSER: Final = register_parser(
    Parser(
        parser_id="boxwood.bill",
        issuer=ISSUER,
        format="pdf",
        version="3",
        parse=parse,
        filing_date=filing_date,
    )
)
