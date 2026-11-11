"""The one door to the vendored ``docextract`` modules (FR-7, Key decision 1).

The Alder and Yew parsing rules live in four modules vendored from
``doc-extraction-api`` (``rheo_finance.parsers.vendored``, pinned by their bytes in
``tests/guards/test_vendored_bytes.py``). This is the only module that imports them,
and it imports only ``alder.parse``, ``AlderParseError``, ``reference.parse``,
``pdftext.pdf_to_text``, ``NotTextPDF`` and ``schema.to_money``: upstream's
``validate``, ``extractor`` and ``monitor`` modules were never vendored, because
sending a bill that has a working parser to a model is what FR-47 forbids.

- ``pdf_text(path)`` is the PDF text every fixed-rule parser reads (pdftotext
  ``-layout`` through the vendored ``pdftext``, so it is in the outbound allowlist),
  so a parser takes extracted text, never a path. A file with no text layer raises
  ``NoTextLayer``.
- ``alder_statement(text)`` is the vendored ``alder.parse`` with its bare dict
  turned into typed values, every amount through ``to_money``. The ``checks`` list
  becomes a count: its messages carry account names and amounts, and go no further.
- ``reference_bill(text)`` and ``money(value)`` pass the vendored ``reference.parse``
  and ``to_money`` through for the Yew bill parser; text it cannot read as a bill
  raises ``NotRecognised``.
- ``docextract_version()`` names the vendored code: the upstream version and commit,
  the same string the pinned git install reported. A PDF's parse depends on it (the
  text extraction and the rules), so ``derive.stage1.parse_inputs`` and the input
  fingerprint hash it, and keeping the string keeps every fingerprint.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from rheo_finance.parsers.base import NoTextLayer, NotRecognised
from rheo_finance.parsers.vendored import UPSTREAM_COMMIT, UPSTREAM_VERSION
from rheo_finance.parsers.vendored.alder import AlderParseError
from rheo_finance.parsers.vendored.alder import parse as _alder_parse
from rheo_finance.parsers.vendored.pdftext import NotTextPDF, pdf_to_text
from rheo_finance.parsers.vendored.reference import parse as _reference_parse
from rheo_finance.parsers.vendored.schema import to_money


def docextract_version() -> str:
    """The vendored ``docextract`` code: ``<version>+<commit>`` of its upstream."""
    return f"{UPSTREAM_VERSION}+{UPSTREAM_COMMIT}"


def pdf_text(path: Path) -> str:
    """The layout text of the PDF at ``path``."""
    try:
        return pdf_to_text(path)
    except NotTextPDF:
        raise NoTextLayer("the PDF has no usable text layer") from None


def money(value: object) -> Decimal | None:
    """``docextract.schema.to_money``: an exact two-decimal ``Decimal``, or None."""
    return to_money(value)


def reference_bill(text: str) -> Mapping[str, Any]:
    """The vendored ``reference.parse`` over a Yew bill's text;
    ``NotRecognised`` when it cannot read the text as a bill (it fails on a missing
    bill or due date rather than returning nothing)."""
    try:
        return _reference_parse(text)
    # The vendored reference.parse fails in two shapes on text it cannot read:
    # AttributeError from ``.group`` on a None match (no bill-date line, or no
    # "Yew settle by" date in either the summary or the payment stub), and
    # ValueError from ``datetime.strptime`` on a date that matched but does not parse
    # (a word in the month position that is not a month).
    except (AttributeError, ValueError):
        raise NotRecognised("not a Yew bill layout") from None


@dataclass(frozen=True)
class AlderTransaction:
    date: dt.date
    description: str
    withdrawal: Decimal | None
    deposit: Decimal | None
    # None where the statement prints no balance and docextract could not settle one.
    balance: Decimal | None
    fee_type: str | None


@dataclass(frozen=True)
class AlderSpell:
    start: dt.date
    end: dt.date | None


@dataclass(frozen=True)
class AlderAccount:
    """One account section. ``ref`` is the account number's last four digits; the
    printed name is dropped (an account can be renamed and keep its number)."""

    ref: str
    opening: Decimal
    withdrawals: Decimal
    deposits: Decimal
    closing: Decimal
    overdrawn: tuple[AlderSpell, ...]
    transactions: tuple[AlderTransaction, ...]


@dataclass(frozen=True)
class AlderStatement:
    period_start: dt.date
    period_end: dt.date
    accounts: tuple[AlderAccount, ...]
    # How many of docextract's checks failed; any at all rejects the document.
    failed_checks: int


def _amount(value: object) -> Decimal:
    converted = to_money(value)
    if converted is None:
        raise ValueError("docextract returned no amount where one is required")
    return converted


def _date(value: object) -> dt.date:
    return dt.date.fromisoformat(str(value))


def alder_statement(text: str) -> AlderStatement:
    """Parse an Alder statement's layout text; ``NotRecognised`` when docextract does
    not read it as one."""
    try:
        parsed = _alder_parse(text)
    except AlderParseError:
        raise NotRecognised("not an Alder statement layout") from None
    accounts = tuple(
        AlderAccount(
            ref=str(account["ref"]),
            opening=_amount(account["opening"]),
            withdrawals=_amount(account["withdrawals"]),
            deposits=_amount(account["deposits"]),
            closing=_amount(account["closing"]),
            overdrawn=tuple(
                AlderSpell(
                    start=_date(spell["from"]),
                    end=None if spell["to"] is None else _date(spell["to"]),
                )
                for spell in account["overdrawn"]
            ),
            transactions=tuple(
                AlderTransaction(
                    date=_date(txn["date"]),
                    description=str(txn["description"]),
                    withdrawal=to_money(txn["withdrawal"]),
                    deposit=to_money(txn["deposit"]),
                    balance=to_money(txn["balance"]),
                    fee_type=None if txn["fee_type"] is None else str(txn["fee_type"]),
                )
                for txn in account["transactions"]
            ),
        )
        for account in parsed["accounts"]
    )
    return AlderStatement(
        period_start=_date(parsed["period_start"]),
        period_end=_date(parsed["period_end"]),
        accounts=accounts,
        failed_checks=len(parsed["checks"]),
    )
