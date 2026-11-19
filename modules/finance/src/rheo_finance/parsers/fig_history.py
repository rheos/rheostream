"""The Fig transaction history PDF (FR-2 ``official``, FR-8, FR-15(c)).

A transaction history is not a statement of record, so every document it reads has
``official=false``.

**Control kind: ``none``.** The history prints no balance anywhere: no running
balance, no opening or closing, no totals. A parser cannot verify a balance that is
not printed, so its lines load unverified (``none``, one ``statement_period`` per
currency over the printed date range, never tied out). A Fig statement that prints
balances would be another layout with its own parser.

The layout's wording and shapes (the title, the holder and number labels, the date
range, the separator, the anchor line, the verbs and the transaction kinds, any page
stamp) come from the owner's identities file (``identities.layout`` and
``identities.real``); the defaults below describe the invented layout of the
synthetic fixture. As pdftotext prints it: a header with the account holder and the
wallet number, the printed date range, then the transactions newest first under
month headings. Each transaction is a description line (a verb or none, the
separator, a name), an anchor line holding the date and the kind, and three
amounts the layout scatters over those lines: a fee, the amount that arrived (the top
of the amount cell) and the amount that left the balance (its bottom, the fee
included). Amounts print without a sign; the fee is in the bottom's currency. A
cell made only of a page stamp's letters (``fig.history.watermark``), which a text
extractor can drop between cells, is ignored.

- Which side: the send verb (a transfer or a direct debit) and a card transaction
  take money out, a transfer with no verb brings it in, the swap verb converts
  between two balances, and a card check moves nothing and gives no line. Anything
  else is ``NotRecognised``.
- Roles. The bottom holds the fee, so it is never smaller than the fee or the
  arrived amount. With one currency the bottom is the largest amount, and the fee is
  read by its cell: the fee column sits left of the amount column, so on a line
  holding two amounts the left one is the fee (the top and the bottom of the amount
  cell never share a line). With no such line, a zero among the other two is the
  fee; anything else cannot be told apart and is ``NotRecognised``. The arrived
  amount plus the fee must be the bottom. With two currencies the lone amount is the
  arrived one, and of the pair in the fee's currency the larger is the bottom (it is
  the fee plus what was sent). A row with two equal amounts and no fee printed has
  a zero fee.
- Lines: money out is one line of the bottom less the fee, in the bottom's currency
  (a card paid in another currency keeps that amount as ``original_amount``), plus a
  fee line when the fee is not zero (``fee_type`` ``fee``, or ``conversion_fee``
  across currencies). Money in is one line of the amount; an incoming row with a fee
  is ``NotRecognised``. A conversion is an out-leg in the source currency (bottom less
  fee), an in-leg in the target currency (the arrived amount) and its printed fee
  line, so the parser's fee line is the only one for that date (FR-15(c)).
- Hints from ``reference.patterns.classify`` on every line but the fee lines: the
  conversion legs carry ``conversion_hint``, a Juniper payout its ``juniper`` hint. A
  transfer's printed name is its restricted ``counterparty``; a card merchant is not
  a counterparty.
- The account is matched by the wallet number among the registry's ``number``
  identifiers at ``INSTITUTION``; it is stored nowhere.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from rheo_finance.identities import layout
from rheo_finance.identities import real as _real
from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedLine,
    ParsedPeriod,
    ParseResult,
    RegisteredAccount,
    Rejected,
)
from rheo_finance.parsers.card import written_date
from rheo_finance.parsers.hints import with_hints
from rheo_finance.parsers.registry import Parser, register_parser
from rheo_finance.parsers.text import squeezed as _squeezed

INSTITUTION: Final = "Fig"
ISSUER: Final = "fig"
ACCOUNT_KIND: Final = "wallet"
_DATE: Final = r"[A-Z][a-z]+ \d{1,2}, \d{4}"
_MONTH: Final = re.compile(r"[A-Z][a-z]+ \d{4}(?:\s.*)?")
# An amount and its currency; a stray stamp letter may sit right before the digits.
_MONEY: Final = re.compile(r"(?<![\d.,])(\d[\d,]*(?:\.\d+)?) ([A-Z]{3})\b")
# The layout's shapes: the owner's identities file gives his own histories', these
# defaults are the synthetic fixture's invented ones.
_SHAPES: Final = {
    "fig.history.description": (
        r"(?:(?P<verb>[A-Z][a-z]+(?: [a-z]+)?) )?»(?! ?Fig tag:)(?P<rest>.*)"
    ),
    "fig.history.anchor": rf"(?P<date>{_DATE}) » (?P<type>fig[a-z]+) ».*",
    "fig.history.range": rf"Fig span (?P<start>{_DATE}) to (?P<end>{_DATE})",
    "fig.history.target": r"(?P<currency>[A-Z]{3}) bought",
    "fig.history.holder": r"Figwallet.*Fignumber",
    "fig.history.wallet_number": r"W-\d{4}-[A-Z]{2}",
}
# Verbs and transaction kinds, read through ``identities.real``.
SWAP_VERB: Final = "Swapped"
SEND_VERB: Final = "Pushed"
CHECK_VERB: Final = "Card probed"
SWAP: Final = "figswap"
SHOP: Final = "figshop"
PULL: Final = "figpull"
SEND: Final = "figsend"


def _shape(key: str) -> re.Pattern[str]:
    return re.compile(layout(key, _SHAPES[key]))


def _watermark() -> str:
    return layout("fig.history.watermark", "Fig sample copy for tests only, no value.")


def _separator() -> str:
    return layout("fig.history.separator", "»")


Side = Literal["in", "out", "conversion", "check"]


@dataclass(frozen=True)
class _Money:
    value: Decimal
    currency: str


@dataclass(frozen=True)
class _Block:
    verb: str | None
    name: str
    kind: str
    day: dt.date
    # The amounts each line of the transaction prints, line by line.
    cells: tuple[tuple[_Money, ...], ...]

    @property
    def money(self) -> tuple[_Money, ...]:
        return tuple(each for line in self.cells for each in line)

    @property
    def description(self) -> str:
        return f"{self.verb} {_separator()} {self.name}" if self.verb else self.name


def _noise(cell: str) -> bool:
    """A cell made only of the page stamp's fragments."""
    watermark = _watermark()
    return all(token in watermark for token in cell.split())


def _money(line: str) -> list[_Money]:
    return [
        _Money(Decimal(digits.replace(",", "")), currency)
        for digits, currency in _MONEY.findall(line)
    ]


def _name(rest: str) -> str:
    """The description's text: its cells without amounts or stamp letters."""
    cells = re.split(r"\s{2,}", _MONEY.sub("  ", rest).strip())
    return " ".join(cell for cell in cells if cell and not _noise(cell))


def _block(group: list[str]) -> _Block:
    head = _shape("fig.history.description").fullmatch(group[0])
    anchor = _shape("fig.history.anchor")
    anchors = [m for m in map(anchor.fullmatch, group) if m is not None]
    if head is None or len(anchors) != 1:
        raise NotRecognised("a Fig transaction without exactly one anchor line")
    return _Block(
        verb=head["verb"],
        name=_name(head["rest"]),
        kind=anchors[0]["type"],
        day=written_date(anchors[0]["date"], "%B %d, %Y"),
        cells=tuple(tuple(_money(line)) for line in group),
    )


def _blocks(lines: list[str]) -> list[_Block]:
    """Each transaction: its description line and the lines after it, up to the next
    description line or month heading."""
    groups: list[list[str]] = []
    current: list[str] | None = None
    description = _shape("fig.history.description")
    for line in lines:
        if description.fullmatch(line):
            current = [line]
            groups.append(current)
        elif _MONTH.fullmatch(line):
            current = None
        elif current is not None:
            current.append(line)
    return [_block(group) for group in groups]


def _side(block: _Block) -> Side:
    verb, kind = block.verb, block.kind
    if verb == _real(SWAP_VERB) and kind == _real(SWAP):
        return "conversion"
    if verb == _real(CHECK_VERB) and kind == _real(SHOP):
        return "check"
    if verb == _real(SEND_VERB) and kind in (_real(SEND), _real(PULL)):
        return "out"
    if verb is None and kind == _real(SHOP):
        return "out"
    if verb is None and kind == _real(SEND):
        return "in"
    raise NotRecognised("a Fig transaction kind this layout has not shown")


def _roles(cells: tuple[tuple[_Money, ...], ...]) -> tuple[_Money, _Money, _Money]:
    """(fee, arrived, bottom) of a transaction's printed amounts, given line by line."""
    money = [each for line in cells for each in line]
    if len(money) == 2 and money[0] == money[1]:
        return _Money(Decimal(0), money[0].currency), money[0], money[1]
    if len(money) != 3:
        raise NotRecognised("a Fig transaction without its fee and amounts")
    currencies = {each.currency for each in money}
    if len(currencies) == 1:
        return _one_currency(cells)
    if len(currencies) == 2:
        lone = [
            each for each in money
            if sum(other.currency == each.currency for other in money) == 1
        ]  # fmt: skip
        pair = sorted((each for each in money if each not in lone), key=_value)
        return pair[0], lone[0], pair[1]
    raise NotRecognised("Fig amounts in three currencies")


def _one_currency(
    cells: tuple[tuple[_Money, ...], ...],
) -> tuple[_Money, _Money, _Money]:
    # Each amount by its position: (line, cell), so equal values stay apart.
    placed = {
        (row, column): each
        for row, line in enumerate(cells)
        for column, each in enumerate(line)
    }
    # The bottom is the largest; on a tie, the lower one in reading order.
    bottom = max(placed, key=lambda at: (placed[at].value, at))
    others = [at for at in placed if at != bottom]
    left = [at for at in others if at[1] == 0 and len(cells[at[0]]) == 2]
    zero = [at for at in others if placed[at].value == 0]
    if len(left) == 1:
        fee = left[0]
    elif not left and zero:
        fee = zero[0]
    else:
        raise NotRecognised("a Fig fee that cannot be told from the amount")
    (arrived,) = [at for at in others if at != fee]
    if placed[arrived].value + placed[fee].value != placed[bottom].value:
        raise NotRecognised("Fig amounts that do not add up")
    return placed[fee], placed[arrived], placed[bottom]


def _value(money: _Money) -> Decimal:
    return money.value


class _Lines:
    """The lines of one transaction on one registered account."""

    def __init__(self, block: _Block, registered: RegisteredAccount) -> None:
        self.block = block
        self.registered = registered

    def line(
        self,
        value: Decimal,
        currency: str,
        *,
        counterparty: str | None = None,
        original: _Money | None = None,
    ) -> ParsedLine:
        parsed = ParsedLine(
            "transaction",
            account=self.registered,
            posting_date=self.block.day,
            amount=value,
            currency=currency,
            original_amount=None if original is None else original.value,
            original_currency=None if original is None else original.currency,
            description_raw=self.block.description,
            counterparty=counterparty,
        )
        return with_hints(parsed, account_kind=ACCOUNT_KIND, issuer=ISSUER)

    def fee(self, fee: _Money, fee_type: str) -> ParsedLine:
        """A printed fee: no hints, so it is never taken for a conversion leg."""
        return ParsedLine(
            "transaction",
            account=self.registered,
            posting_date=self.block.day,
            amount=-fee.value,
            currency=fee.currency,
            description_raw=f"{self.block.description} (fee)",
            fee_type=fee_type,
        )


def _lines(block: _Block, registered: RegisteredAccount) -> list[ParsedLine]:
    side = _side(block)
    if side == "check":
        if any(each.value for each in block.money):
            raise NotRecognised("a card check that moves money")
        return []
    fee, arrived, bottom = _roles(block.cells)
    make = _Lines(block, registered)
    name = block.name or None
    crossed = arrived.currency != bottom.currency
    if side == "in":
        if fee.value or crossed:
            raise NotRecognised("an incoming Fig transfer with a fee")
        return [make.line(bottom.value, bottom.currency, counterparty=name)]
    principal = bottom.value - fee.value
    if side == "conversion":
        target = _shape("fig.history.target").fullmatch(block.name)
        if target is None or target["currency"] != arrived.currency or not crossed:
            raise NotRecognised("a Fig conversion without its target currency")
        found = [
            make.line(-principal, bottom.currency),
            make.line(arrived.value, arrived.currency),
        ]
    else:
        found = [
            make.line(
                -principal,
                bottom.currency,
                counterparty=None if block.kind == _real(SHOP) else name,
                original=_Money(-arrived.value, arrived.currency) if crossed else None,
            )
        ]
    if fee.value:
        found.append(make.fee(fee, "conversion_fee" if crossed else "fee"))
    return found


def _wallet_number(lines: list[str]) -> str:
    holder = _shape("fig.history.holder")
    for index, line in enumerate(lines):
        if holder.fullmatch(_squeezed(line)):
            following = [each for each in lines[index + 1 :] if each.strip()]
            cells = re.split(r"\s{2,}", following[0].strip()) if following else []
            if cells and _shape("fig.history.wallet_number").fullmatch(cells[-1]):
                return cells[-1]
    raise NotRecognised("no Fig wallet number")


def _period(text: str) -> tuple[dt.date, dt.date]:
    found = {(m["start"], m["end"]) for m in _shape("fig.history.range").finditer(text)}
    if len(found) != 1:
        raise NotRecognised("no single Fig date range")
    ((start, end),) = found
    return written_date(start, "%B %d, %Y"), written_date(end, "%B %d, %Y")


def _read(text: str) -> tuple[str, tuple[dt.date, dt.date], list[_Block]]:
    lines = text.splitlines()
    if not any(_squeezed(line).startswith(_real("Figmovementlist")) for line in lines):
        raise NotRecognised("not a Fig transaction history")
    return _wallet_number(lines), _period(text), _blocks(lines)


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    number, period, blocks = _read(text)
    found = accounts.by_identifier(INSTITUTION, number, *period)
    if not found:
        raise Rejected("unknown_account", period)
    if len(found) > 1:
        raise Rejected("ambiguous_account", period)
    registered = found[0]
    # The history lists newest first; lines are kept oldest first.
    lines = [each for block in reversed(blocks) for each in _lines(block, registered)]
    currencies = sorted({each.currency for each in lines if each.currency})
    periods = tuple(
        ParsedPeriod(
            account=registered,
            currency=currency,
            period_start=period[0],
            period_end=period[1],
            control_kind="none",
        )
        for currency in currencies
    )
    return ParseResult(
        periods=periods, lines=tuple(lines), control_kind="none", official=False
    )


def filing_date(text: str) -> dt.date:
    """The end of the printed date range, the date the filer names a new copy by."""
    return _read(text)[1][1]


PARSER: Final = register_parser(
    Parser(
        parser_id="fig.history",
        issuer=ISSUER,
        format="pdf",
        version="1",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
