"""The Hazel activity CSV (FR-12(a), FR-3, FR-4, FR-15(c); SPEC "Derivation" stage 1,
"Fiat CSV exports").

``COLUMNS`` is the parser's own, invented layout, the one the synthetic fixture prints:
only the columns it reads (a balance, an amount and a fee, a currency, a kind, a date
and a counterpart name). The owner's identities file gives the order, the wording and
the date format of his own exports (``identities.layout`` and ``identities.real``),
including an optional ``Hz Net`` (checked as the amount plus the fee). A file whose
header is not exactly the expected one is ``NotRecognised`` (``unparsed``, no line).

**Control kind: ``running_balance``, per currency.** The balance column is the row's
currency's balance after the row (``parsers.balance_csv``). A row on hold moves no
balance and gives no line: the invented layout marks it with the kind ``SKIP``'s
word, and the owner's identities file may give his own rule
(``hazel.activity_csv.columns.skip``).

The lines are the activity statement's (``parsers.hazel_activity``), so stage 3
pairs the two one to one when they overlap: the matcher's key is account, currency,
signed amount and a date inside the tolerance, and each row gives

- a line of its amount in its own currency (a currency exchange is a row in each
  currency, each its own leg), and
- a fee line of the fee as printed when it is not zero (``fee_type`` ``fee``), with
  the statement's description plus `` (fee)``.
The description is the kind and the name, and the name alone is the restricted
``counterparty``. A column the parser does not read is stored nowhere. Hints come from
``reference.patterns.classify`` (account kind ``payment_processor``) on every line but
the fee lines: the exchange kind carries ``conversion_hint``, the bank funding kind its
``hazel_preauthorized`` transfer hint.

The export prints no account identifier, so the account is the one
``payment_processor`` account with a ``number`` identifier registered at
``INSTITUTION`` over the file's dates (``AccountLookup.by_institution``, which names
the residual risk): none is ``unknown_account``, several are ``ambiguous_account``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Final

from rheo_finance.identities import layout
from rheo_finance.parsers import balance_csv
from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedLine,
    ParseResult,
    RegisteredAccount,
)
from rheo_finance.parsers.card import written_date
from rheo_finance.parsers.hazel_activity import ACCOUNT_KIND, INSTITUTION, ISSUER
from rheo_finance.parsers.hints import with_hints
from rheo_finance.parsers.registry import Parser, register_parser

WHAT: Final = "not a Hazel activity CSV"
# The parser's own column names, in the invented order of the synthetic fixture. The
# owner's identities file gives the order and wording of his own exports.
COLUMNS: Final = (
    "Hz Unit", "Hz Party", "Hz Fee", "Hz Kind", "Hz Day", "Hz Balance", "Hz Amount",
)  # fmt: skip
COLUMNS_KEY: Final = "hazel.activity_csv.columns"
# A column the owner's identities file may add (see the module docstring).
EXTRA: Final = ("Hz Net",)
# The rows the invented layout leaves out: those of this kind.
SKIP: Final = ("Hz Kind", "HZ HELD")


def _date_format() -> str:
    return layout("hazel.activity_csv.date", "%Y-%m-%d")


def _lines(
    day: dt.date, code: str, cells: dict[str, str]
) -> Callable[[RegisteredAccount], tuple[ParsedLine, ...]]:
    gross = balance_csv.signed(cells["Hz Amount"], WHAT)
    fee = balance_csv.signed(cells["Hz Fee"] or "0", WHAT)
    stated = cells.get("Hz Net")
    if stated is not None and gross + fee != balance_csv.signed(stated, WHAT):
        raise NotRecognised(WHAT)
    kind = " ".join(cells["Hz Kind"].split())
    name = " ".join(cells["Hz Party"].split())
    if not kind:
        raise NotRecognised(WHAT)
    description = f"{kind} {name}".strip()

    def make(registered: RegisteredAccount) -> tuple[ParsedLine, ...]:
        line = ParsedLine(
            "transaction",
            account=registered,
            posting_date=day,
            amount=gross,
            currency=code,
            description_raw=description,
            counterparty=name or None,
        )
        found = [with_hints(line, account_kind=ACCOUNT_KIND, issuer=ISSUER)]
        if fee:
            found.append(
                ParsedLine(
                    "transaction",
                    account=registered,
                    posting_date=day,
                    amount=fee,
                    currency=code,
                    description_raw=f"{description} (fee)",
                    fee_type="fee",
                )
            )
        return tuple(found)

    return make


def _movements(text: str) -> list[balance_csv.Movement]:
    movements: list[balance_csv.Movement] = []
    rows = balance_csv.named_rows(
        text, COLUMNS_KEY, COLUMNS, WHAT, extra=EXTRA, skip=SKIP
    )
    for cells in rows:
        day = written_date(cells["Hz Day"], _date_format())
        code = balance_csv.currency(cells["Hz Unit"], WHAT)
        movements.append(
            balance_csv.Movement(
                day=day,
                currency=code,
                balance=balance_csv.signed(cells["Hz Balance"], WHAT),
                lines=_lines(day, code, cells),
            )
        )
    if not movements:
        raise NotRecognised(WHAT)
    return movements


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    movements = _movements(text)
    registered = balance_csv.one_account(accounts, INSTITUTION, ACCOUNT_KIND, movements)
    return balance_csv.running_balance_result(registered, movements, WHAT)


def filing_date(text: str) -> dt.date:
    """The latest row's date, the date the filer names a new copy by."""
    return balance_csv.span(_movements(text))[1]


PARSER: Final = register_parser(
    Parser(
        parser_id="hazel.activity_csv",
        issuer=ISSUER,
        format="csv",
        version="1",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
