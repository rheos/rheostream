"""The Juniper balance history CSV: each payout with the charges, refunds and fees it
pays out (FR-12(a), FR-3, FR-4, FR-15(c); SPEC "Derivation" stage 1, "Fiat CSV
exports"; edge case 22).

``COLUMNS`` is the parser's own, invented layout, the one the synthetic fixture
prints: one row per balance transaction in either date order, ``Jn Day`` as
``YYYY-MM-DD`` (a time after it is allowed), a currency code, a signed ``Jn Amount``,
a ``Jn Fee`` that is never negative, and ``Jn Group`` naming the payout the row belongs
to (a payout's own row names itself). The owner's identities file gives the order and
the wording of his own exports (``identities.layout`` and ``identities.real``),
including an optional ``Jn Net`` (checked as the amount less the fee) and ``Jn Ref``
(the row's own id, which then names a payout instead of its group). A file whose
header is not exactly the expected one is ``NotRecognised`` (``unparsed``, no line).

**Control kind: ``none``.** The export prints no balance, so nothing in it verifies a
line: each currency is one unverified period from its first row's date to its last's.
Should an export print a balance column, the parser moves to ``running_balance``
through ``parsers.balance_csv``.

A payout (row type ``PAYOUT``, a negative amount) and the rows whose ``Jn Group``
names it give, on the Juniper account, three lines dated on the payout's day:

- the gross: the sum of its rows' amounts (``GROSS_TYPES``), a credit;
- the fees: the sum of every fee in it, the payout's own included, as one fee line
  (``fee_type`` ``fee``), left out when zero;
- the payout: its amount, a transfer-like debit whose hints come from
  ``reference.patterns.classify`` (account kind ``payment_processor``): the Juniper side
  of ``juniper_payout``, naming the Fig account (``hint_account_slug`` ``fig``), so
  stage 9 can pair it with the Fig deposit.

So gross less fees is the payout, and gross income, fees and net are all visible. A
payout is complete when its rows' net and its own sum to zero (it pays out exactly
what its rows put in). A payout whose rows are only partly in the file (an export cut
by date) gives its payout line alone; a fuller export later matches that line one to
one under stage 3 and adds the gross and the fee. A row whose payout is not in the
file, or that no payout has paid out yet, gives no line: the export holding its payout
carries it.

Row ids, payout ids and any column the parser does not read are stored nowhere; the
lines' descriptions are the parser's own words and no line has a counterparty. A row
whose net is not its gross less its fee, a negative fee, a row of a type outside
``PAYOUT`` and ``GROSS_TYPES``, a payout that is not a debit, two payouts with one id,
or a payout whose rows are in another currency is not this layout. An unknown type makes
the whole file ``unparsed`` rather than joining a gross it does not belong to.

The export prints no account identifier, so the account is the one
``payment_processor`` account with a ``number`` identifier registered at
``INSTITUTION`` over the file's dates (``AccountLookup.by_institution``, which names
the residual risk): none is ``unknown_account``, several are ``ambiguous_account``.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from rheo_finance.identities import layout
from rheo_finance.parsers import balance_csv
from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedLine,
    ParsedPeriod,
    ParseResult,
    RegisteredAccount,
    Rejected,
)
from rheo_finance.parsers.hints import with_hints
from rheo_finance.parsers.registry import Parser, register_parser

INSTITUTION: Final = "Juniper"
ISSUER: Final = "juniper"
ACCOUNT_KIND: Final = "payment_processor"
WHAT: Final = "not a Juniper balance history CSV"
# The parser's own column names, in the invented order of the synthetic fixture. The
# owner's identities file gives the order and wording of his own exports.
COLUMNS: Final = ("Jn Fee", "Jn Kind", "Jn Day", "Jn Group", "Jn Unit", "Jn Amount")
# Columns the owner's identities file may add (see the module docstring).
EXTRA: Final = ("Jn Net", "Jn Ref")
COLUMNS_KEY: Final = "juniper.balance_csv.columns"
# Row types. The owner's identities file gives his exports' own words
# (``identities.layout``): the payout's, and the ones a payout's gross is made of.
PAYOUT: Final = "BANKED"
GROSS_TYPES: Final = ("TAKE", "UNDO")
GROSS_TEXT: Final = "Gross charges"
FEE_TEXT: Final = "Juniper fees"
PAYOUT_TEXT: Final = "Payout to bank"
_CREATED: Final = re.compile(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?")


@dataclass(frozen=True)
class _Row:
    kind: str
    source: str
    amount: Decimal
    fee: Decimal
    net: Decimal
    currency: str
    created: dt.datetime
    payout: str

    @property
    def day(self) -> dt.date:
        return self.created.date()


def _created(cell: str) -> dt.datetime:
    text = " ".join(cell.split())
    if _CREATED.fullmatch(text) is None:
        raise NotRecognised(WHAT)
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        raise NotRecognised(WHAT) from None


def _rows(text: str) -> list[_Row]:
    found: list[_Row] = []
    payout = layout("juniper.balance_csv.payout_type", PAYOUT)
    gross_types = layout("juniper.balance_csv.gross_types", GROSS_TYPES)
    rows = balance_csv.named_rows(text, COLUMNS_KEY, COLUMNS, WHAT, extra=EXTRA)
    for cells in rows:
        amount = balance_csv.signed(cells["Jn Amount"], WHAT)
        fee = balance_csv.signed(cells["Jn Fee"] or "0", WHAT)
        net = amount - fee
        printed = cells["Jn Kind"].strip()
        stated = cells.get("Jn Net")
        if stated is not None and balance_csv.signed(stated, WHAT) != net:
            raise NotRecognised(WHAT)
        if fee < 0 or printed not in (payout, *gross_types):
            raise NotRecognised(WHAT)
        kind = PAYOUT if printed == payout else printed
        group = cells["Jn Group"].strip()
        found.append(
            _Row(
                kind=kind,
                source=cells.get("Jn Ref", group).strip(),
                amount=amount,
                fee=fee,
                net=net,
                currency=balance_csv.currency(cells["Jn Unit"].upper(), WHAT),
                created=_created(cells["Jn Day"]),
                payout=group,
            )
        )
    if not found:
        raise NotRecognised(WHAT)
    return found


@dataclass(frozen=True)
class _Payout:
    row: _Row
    paid: tuple[_Row, ...]

    @property
    def complete(self) -> bool:
        return self.row.net + sum((each.net for each in self.paid), Decimal(0)) == 0

    def lines(self, registered: RegisteredAccount) -> list[ParsedLine]:
        def line(amount: Decimal, text: str, fee_type: str | None = None) -> ParsedLine:
            return ParsedLine(
                "transaction",
                account=registered,
                posting_date=self.row.day,
                amount=amount,
                currency=self.row.currency,
                description_raw=text,
                fee_type=fee_type,
            )

        hinted = {"account_kind": ACCOUNT_KIND, "issuer": ISSUER}
        payout = with_hints(line(self.row.amount, PAYOUT_TEXT), **hinted)
        if not self.complete:
            return [payout]
        gross = sum((each.amount for each in self.paid), Decimal(0))
        fees = self.row.fee + sum((each.fee for each in self.paid), Decimal(0))
        found = [with_hints(line(gross, GROSS_TEXT), **hinted)]
        if fees:
            found.append(line(-fees, FEE_TEXT, "fee"))
        return [*found, payout]


def _payouts(rows: list[_Row]) -> list[_Payout]:
    """Every payout in the file with the rows it pays out, oldest first."""
    by_payout: dict[str, list[_Row]] = defaultdict(list)
    heads: dict[str, _Row] = {}
    for row in rows:
        if row.kind != PAYOUT:
            if row.payout:
                by_payout[row.payout].append(row)
            continue
        if not row.source or row.source in heads or row.amount >= 0:
            raise NotRecognised(WHAT)
        heads[row.source] = row
    payouts: list[_Payout] = []
    for key, head in heads.items():
        paid = tuple(by_payout[key])
        if any(each.currency != head.currency for each in paid):
            raise NotRecognised(WHAT)
        payouts.append(_Payout(head, paid))
    return sorted(payouts, key=lambda each: each.row.created)


def _span(rows: list[_Row]) -> tuple[dt.date, dt.date]:
    days = [row.day for row in rows]
    return min(days), max(days)


def _account(accounts: AccountLookup, rows: list[_Row]) -> RegisteredAccount:
    period = _span(rows)
    found = accounts.by_institution(INSTITUTION, ACCOUNT_KIND, *period)
    if not found:
        raise Rejected("unknown_account", period)
    if len(found) > 1:
        raise Rejected("ambiguous_account", period)
    return found[0]


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    rows = _rows(text)
    payouts = _payouts(rows)
    registered = _account(accounts, rows)
    by_currency: dict[str, list[_Row]] = defaultdict(list)
    for row in rows:
        by_currency[row.currency].append(row)
    periods = tuple(
        ParsedPeriod(
            account=registered,
            currency=code,
            period_start=_span(by_currency[code])[0],
            period_end=_span(by_currency[code])[1],
            control_kind="none",
        )
        for code in sorted(by_currency)
    )
    lines = tuple(line for each in payouts for line in each.lines(registered))
    return ParseResult(periods=periods, lines=lines, control_kind="none")


def filing_date(text: str) -> dt.date:
    """The latest row's date, the date the filer names a new copy by."""
    return _span(_rows(text))[1]


PARSER: Final = register_parser(
    Parser(
        parser_id="juniper.balance_csv",
        issuer=ISSUER,
        format="csv",
        version="1",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
