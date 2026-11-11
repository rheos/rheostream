"""The card parsers through ``parse_document`` (FR-3, FR-8, FR-14, FR-19, FR-20;
AC-8 parser part): the Alder Neem statement, headerless CSV export and transaction
download, and the Sumac statement.

Seam: ``parse_document`` over a synthetic document filed into a temporary archive,
against accounts registered through the manual files. The statements tie out and
reject on a one-cent difference; the card fields are populated (a statement that
prints no minimum gives a null one); a foreign purchase keeps the issuer's CAD amount;
the cardholder's name never reaches a stored row. Every value is fake; the fixtures
are layout text, read as text where the real run calls pdftotext.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Table, func, select

from rheo_finance import identities
from rheo_finance.context import UnitOfWork
from rheo_finance.documents.registry import document_id, file_archive
from rheo_finance.manual.stage2 import account_id
from rheo_finance.parsers import alder_neem_csv
from rheo_finance.parsers.registry import (
    DOCUMENT_REJECTED,
    Parsed,
    parse_document,
    parsers_for,
)
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import (
    document,
    document_line,
    event,
    rejected_period,
    statement_period,
)

from .conftest import ManualAccount, register_accounts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
NOW = dt.datetime(2059, 3, 17, 12, 0, tzinfo=dt.UTC)
# The invented cardholder every fixture prints; no stored row may carry it.
CARDHOLDER = "sample cardholder"


def card(slug: str, institution: str, identifier: str) -> ManualAccount:
    return ManualAccount(slug, institution, "credit_card", (identifier,), "masked_card")


NEEM = card("neem", "Alder", "XXXX-9991")
SUMAC = card("sumac-card", "Sumac", "**** **** **** 9992")
OTHER_NEEM = card("other-neem", "Alder", "XXXX-9993")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def read_as_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class Setup:
    def __init__(self, conn: Connection, tmp_path: Path) -> None:
        self.conn = conn
        self.uow = UnitOfWork(conn)
        self.data = tmp_path / "data"
        self.archive = tmp_path / "archive"
        self.archive.mkdir()

    def register(self, *cards: ManualAccount) -> None:
        register_accounts(self.uow, self.data, *cards)

    def file(self, relative: str, text: str) -> uuid.UUID:
        target = self.archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        target.write_bytes(data)
        file_archive(self.uow, self.archive, self.data, NOW)
        return document_id(hashlib.sha256(data).hexdigest())

    def parse(self, doc: uuid.UUID) -> Parsed:
        return parse_document(self.uow, doc, self.archive, extract_pdf=read_as_text)

    def period(self, doc: uuid.UUID) -> Any:
        return self.conn.execute(
            select(statement_period).where(statement_period.c.document_id == doc)
        ).one()

    def lines(self, doc: uuid.UUID) -> list[Any]:
        return list(
            self.conn.execute(
                select(document_line)
                .where(document_line.c.document_id == doc)
                .order_by(document_line.c.line_no)
            ).all()
        )

    def count(self, table: Table, doc: uuid.UUID) -> int:
        query = select(func.count()).select_from(table)
        return int(self.conn.scalar(query.where(table.c.document_id == doc)) or 0)

    def rejected_reasons(self, doc: uuid.UUID) -> list[object]:
        ref = format_ref("finance", "document", doc)
        return [
            data["reason"]
            for data in self.conn.scalars(
                select(event.c.data).where(
                    event.c.type == DOCUMENT_REJECTED, event.c.subject_ref == ref
                )
            )
        ]

    def stored_text(self, doc: uuid.UUID) -> list[str]:
        """Every text value stored for the document: its row, its periods, its lines
        and its events, casefolded."""
        found: list[str] = []
        for table in (document, statement_period, document_line):
            key = table.c.id if table is document else table.c.document_id
            for row in self.conn.execute(select(table).where(key == doc)):
                found += [str(value).casefold() for value in row if value is not None]
        ref = format_ref("finance", "document", doc)
        for data in self.conn.scalars(
            select(event.c.data).where(event.c.subject_ref == ref)
        ):
            found.append(str(data).casefold())
        return found


@pytest.fixture
def setup(owner_conn: Connection, tmp_path: Path) -> Setup:
    return Setup(owner_conn, tmp_path)


def amounts(lines: list[Any]) -> list[Decimal]:
    return [line.amount for line in lines]


# --- the Alder Neem statement ----------------------------------------------------


NEEM_STATEMENT = "alder-neem/2050-01-24.pdf"


def test_the_neem_statement_ties_out_with_its_card_fields(setup: Setup) -> None:
    setup.register(NEEM)
    doc = setup.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=7)

    period = setup.period(doc)
    assert period.account_id == account_id("neem")
    # The period crosses the new year; its end is the statement date.
    assert (period.period_start, period.period_end) == (
        dt.date(2049, 12, 2), dt.date(2050, 1, 24),
    )  # fmt: skip
    assert (period.currency, period.control_kind, period.tied_out) == (
        "CAD", "summary", True,
    )  # fmt: skip
    # The balance owed, negated: the card's lines run from one to the other.
    assert (period.opening, period.closing) == (
        Decimal("-2443.00"),
        Decimal("-3088.66"),
    )
    assert (period.total_debits, period.total_credits) == (
        Decimal("1256.41"), Decimal("610.75"),
    )  # fmt: skip
    assert period.balance_due == Decimal("3088.66")
    assert period.minimum_due == Decimal("98.19")
    assert period.due_date == dt.date(2050, 1, 29)
    # The payment line, not the refund the summary folds into "Neem credits in".
    assert period.total_payments == Decimal("586.32")


def test_the_neem_statement_lines_dates_signs_and_hints(setup: Setup) -> None:
    setup.register(NEEM)
    doc = setup.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))
    setup.parse(doc)

    lines = setup.lines(doc)
    assert [(line.txn_date, line.posting_date) for line in lines] == [
        (dt.date(2049, 12, 3), dt.date(2049, 12, 6)),
        (dt.date(2049, 12, 18), dt.date(2049, 12, 19)),
        (dt.date(2049, 12, 23), dt.date(2049, 12, 23)),
        (dt.date(2049, 12, 25), dt.date(2049, 12, 26)),
        (dt.date(2049, 12, 28), dt.date(2049, 12, 29)),
        (dt.date(2050, 1, 16), dt.date(2050, 1, 16)),
        (dt.date(2050, 1, 20), dt.date(2050, 1, 20)),
    ]
    assert amounts(lines) == [
        Decimal(each)
        for each in (
            "-56.58",
            "-54.38",
            "586.32",
            "24.43",
            "-981.77",
            "-96.08",
            "-67.60",
        )
    ]
    # The right-hand column's text is not part of a description.
    assert lines[1].description_raw == "SAMPLE CLOUD HOSTING SAMPLE.TEST ZZ"
    assert lines[2].description_raw == "CARD PAID IN -- NOTED"
    assert {line.currency for line in lines} == {"CAD"}
    assert {line.line_kind for line in lines} == {"transaction"}

    payment = lines[2]
    assert (payment.transfer_hint, payment.hint_account_slug) == (True, "alder")
    assert [line.fee_type for line in lines] == [
        None, None, None, None, None, "annual_fee", "interest",
    ]  # fmt: skip
    assert [line.transfer_hint for line in lines].count(True) == 1
    assert not any(line.reversal_hint or line.etransfer_hint for line in lines)
    # The cardholder heading is read past: the name reaches no stored row.
    assert "sample cardholder:" in fixture("alder_neem_statement.txt").casefold()
    assert not any(CARDHOLDER in value for value in setup.stored_text(doc))


def test_a_foreign_purchase_keeps_the_issuers_cad_amount(setup: Setup) -> None:
    setup.register(NEEM)
    doc = setup.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))
    setup.parse(doc)

    foreign = [
        (line.amount, line.currency, line.original_amount, line.original_currency,
         line.stated_cad)
        for line in setup.lines(doc)
        if line.original_currency is not None
    ]  # fmt: skip
    # 46.69 USD at the printed rate is not exactly 54.38: the stated CAD stands.
    assert foreign == [
        (Decimal("-54.38"), "CAD", Decimal("-46.69"), "USD", Decimal("-54.38")),
        (Decimal("-981.77"), "CAD", Decimal("-195.44"), "USD", Decimal("-981.77")),
    ]


@pytest.mark.parametrize(
    ("printed", "changed"),
    [
        ("$3,088.66", "$3,088.67"),  # the statement balance, everywhere it prints
        (
            "SAMPLE GROCER SAMPLE CITY ZZ   $56.58",
            "SAMPLE GROCER SAMPLE CITY ZZ   $56.59",
        ),
        ("Neem interest   +   $67.60", "Neem interest   +   $67.61"),
    ],
)
def test_a_neem_statement_a_cent_off_is_rejected_with_zero_lines(
    setup: Setup, printed: str, changed: str
) -> None:
    setup.register(NEEM)
    text = fixture("alder_neem_statement.txt")
    assert printed in text
    doc = setup.file(NEEM_STATEMENT, text.replace(printed, changed))

    assert setup.parse(doc) == Parsed("rejected", "summary_mismatch")

    assert setup.count(document_line, doc) == 0
    assert setup.count(statement_period, doc) == 0
    assert setup.count(rejected_period, doc) == 1
    assert setup.rejected_reasons(doc) == ["summary_mismatch"]


def test_a_neem_statement_with_no_printed_minimum_has_a_null_minimum(
    setup: Setup,
) -> None:
    setup.register(NEEM)
    doc = setup.file(
        "alder-neem/2050-03-12.pdf", fixture("alder_neem_statement_no_minimum.txt")
    )

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=2)

    period = setup.period(doc)
    assert period.minimum_due is None
    assert (period.balance_due, period.due_date, period.total_payments) == (
        Decimal("48.86"), dt.date(2050, 3, 21), Decimal("781.76"),
    )  # fmt: skip
    assert (period.period_end, period.tied_out) == (dt.date(2050, 3, 12), True)


def test_a_neem_statement_for_an_unregistered_card_is_rejected(setup: Setup) -> None:
    setup.register(OTHER_NEEM)
    doc = setup.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")
    assert setup.count(document_line, doc) == 0


def test_text_no_neem_parser_reads_is_unparsed(setup: Setup) -> None:
    setup.register(NEEM)
    doc = setup.file(NEEM_STATEMENT, "A PAGE OF TEXT THAT IS NOT A STATEMENT\n" * 9)

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


# --- the Alder Neem CSV export -----------------------------------------------------


def test_the_neem_export_loads_oldest_first_with_control_none(setup: Setup) -> None:
    setup.register(NEEM)
    doc = setup.file("alder-neem/2048-02-22.csv", fixture("alder_neem_export.csv"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=5)

    period = setup.period(doc)
    assert (period.control_kind, period.tied_out, period.account_id) == (
        "none", False, account_id("neem"),
    )  # fmt: skip
    assert (period.period_start, period.period_end) == (
        dt.date(2048, 2, 4), dt.date(2048, 2, 22),
    )  # fmt: skip
    assert period.opening is None and period.minimum_due is None

    lines = setup.lines(doc)
    assert [(line.txn_date, line.posting_date) for line in lines] == [
        (dt.date(2048, 2, 4), dt.date(2048, 2, 4)),
        (dt.date(2048, 2, 10), dt.date(2048, 2, 12)),
        (dt.date(2048, 2, 13), dt.date(2048, 2, 15)),
        (dt.date(2048, 2, 19), dt.date(2048, 2, 19)),
        (dt.date(2048, 2, 21), dt.date(2048, 2, 22)),
    ]
    # A debit is spending, a credit a payment or a refund.
    assert amounts(lines) == [
        Decimal(each) for each in ("-67.60", "-56.58", "24.43", "488.60", "-6.21")
    ]
    assert [line.transfer_hint for line in lines] == [False, False, False, True, False]
    assert lines[3].hint_account_slug == "alder"
    assert lines[0].fee_type == "interest"


def test_a_neem_column_read_past_is_stored_nowhere(
    setup: Setup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An owner layout that marks a column ``=`` (here a name, added to each row)
    reads past it: the export parses and the name is stored nowhere."""
    owner = tmp_path / "identities.toml"
    columns = ", ".join(f'"{each}"' for each in (*alder_neem_csv.ROLES, "=name"))
    owner.write_text(
        f'[layout]\n"alder_neem.csv.columns" = [{columns}]\n', encoding="utf-8"
    )
    monkeypatch.setattr(identities, "identities_path", lambda: owner)
    setup.register(NEEM)
    rows = fixture("alder_neem_export.csv").splitlines()
    text = "".join(f"{row},SAMPLE CARDHOLDER\n" for row in rows)
    assert text.casefold().count(CARDHOLDER) == 5
    doc = setup.file("alder-neem/2048-02-22.csv", text)
    assert setup.parse(doc).status == "parsed"

    assert all(line.counterparty is None for line in setup.lines(doc))
    stored = setup.stored_text(doc)
    assert stored
    assert not any(CARDHOLDER in value for value in stored)


def test_a_neem_export_for_an_unregistered_card_is_rejected(setup: Setup) -> None:
    setup.register(OTHER_NEEM)
    doc = setup.file("alder-neem/2048-02-22.csv", fixture("alder_neem_export.csv"))

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")
    assert setup.count(document_line, doc) == 0


@pytest.mark.parametrize(
    ("printed", "changed", "currency_column"),
    [
        (",,6.21,XXXX-9991,", ",1.05,6.21,XXXX-9991,", False),  # a credit and a debit
        (
            ",,6.21,XXXX-9991,CAD,",
            ",,6.21,XXXX-9991,USD,",
            True,
        ),  # a currency the export does not post
        (",,6.21,XXXX-9991,", ",6.21,XXXX-9991,", False),  # five columns
    ],
)
def test_a_csv_that_is_not_the_neem_export_is_unparsed(
    setup: Setup,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    printed: str,
    changed: str,
    currency_column: bool,
) -> None:
    setup.register(NEEM)
    text = fixture("alder_neem_export.csv")
    if currency_column:
        # The owner's layout adds a currency column after the card; every row of the
        # export reads CAD there.
        owner = tmp_path / "identities.toml"
        order = (*alder_neem_csv.ROLES[:5], "currency", alder_neem_csv.ROLES[5])
        columns = ", ".join(f'"{each}"' for each in order)
        owner.write_text(
            f'[layout]\n"alder_neem.csv.columns" = [{columns}]\n', encoding="utf-8"
        )
        monkeypatch.setattr(identities, "identities_path", lambda: owner)
        text = text.replace("XXXX-9991,", "XXXX-9991,CAD,")
    assert text.count(printed) == 1
    doc = setup.file("alder-neem/2048-02-22.csv", text.replace(printed, changed))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


# --- the Alder Neem transaction download -------------------------------------------


NEEM_DOWNLOAD = "alder-neem/2049-10-28-download.pdf"


def test_the_neem_download_loads_with_control_none(setup: Setup) -> None:
    setup.register(NEEM)
    doc = setup.file(NEEM_DOWNLOAD, fixture("alder_neem_transactions.txt"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=5)

    row = setup.conn.execute(
        select(document.c.parser_id, document.c.parser_version).where(
            document.c.id == doc
        )
    ).one()
    assert tuple(row) == ("alder_neem.transactions", "transactions-1")
    period = setup.period(doc)
    assert (period.control_kind, period.tied_out) == ("none", False)
    assert (period.period_start, period.period_end) == (
        dt.date(2049, 9, 8), dt.date(2049, 10, 28),
    )  # fmt: skip

    lines = setup.lines(doc)
    assert [line.posting_date for line in lines] == [
        dt.date(2049, 9, 20), dt.date(2049, 9, 25), dt.date(2049, 10, 8),
        dt.date(2049, 10, 11), dt.date(2049, 10, 17),
    ]  # fmt: skip
    assert amounts(lines) == [
        Decimal(each) for each in ("-86.21", "-56.58", "24.43", "586.32", "-981.77")
    ]
    # A description runs on to the next line; the page footer is not part of one.
    assert [line.description_raw for line in lines] == [
        "OVERLIMIT FEE",
        "SAMPLE GROCER SAMPLE CITY ZZ 99902",
        "SAMPLE BOOKSHOP SAMPLE CITY ZZ",
        "CARD PAID IN -- NOTED",
        "SAMPLE AI SUBSCRIPTION SAMPLE.TEST ZZ 99901",
    ]
    assert lines[3].transfer_hint and lines[3].hint_account_slug == "alder"
    assert lines[0].fee_type == "overlimit_fee"
    stored = setup.stored_text(doc)
    assert not any(CARDHOLDER in value for value in stored)


def test_the_older_unsigned_download_is_unparsed(setup: Setup) -> None:
    """The older download prints no sign: a refund would read as a purchase."""
    setup.register(NEEM)
    text = (
        fixture("alder_neem_transactions.txt")
        .replace("XXXX-9991", "XXXX 9991")
        .replace("-$", "$")
    )
    doc = setup.file(NEEM_DOWNLOAD, text)

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


# --- the Sumac statement --------------------------------------------------------------


SUMAC_STATEMENT = "sumac/2042-10-22.pdf"


def test_the_sumac_statement_ties_out_with_its_card_fields(setup: Setup) -> None:
    setup.register(SUMAC)
    doc = setup.file(SUMAC_STATEMENT, fixture("sumac_statement.txt"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=5)

    period = setup.period(doc)
    assert period.account_id == account_id("sumac-card")
    assert (period.period_start, period.period_end) == (
        dt.date(2042, 9, 18), dt.date(2042, 10, 22),
    )  # fmt: skip
    assert (period.control_kind, period.tied_out) == ("summary", True)
    assert (period.opening, period.closing) == (
        Decimal("-1002.30"),
        Decimal("-1064.18"),
    )
    assert period.balance_due == Decimal("1064.18")
    assert period.minimum_due == Decimal("70.46")
    assert period.due_date == dt.date(2042, 11, 13)
    assert period.total_payments == Decimal("627.66")

    lines = setup.lines(doc)
    assert amounts(lines) == [
        Decimal(each) for each in ("627.66", "-7.74", "-646.95", "29.78", "-64.63")
    ]
    assert [(line.txn_date, line.posting_date) for line in lines][:2] == [
        (dt.date(2042, 10, 2), dt.date(2042, 10, 2)),
        (dt.date(2042, 9, 24), dt.date(2042, 9, 25)),
    ]
    # The reference number is not part of the description; the columns' gaps close.
    assert lines[1].description_raw == "SAMPLE STREAMING SAMPLE CITY ZZ"
    assert (lines[0].description_raw, lines[0].transfer_hint) == ("SUMAC PAID", True)
    assert lines[4].fee_type == "interest"
    assert [line.transfer_hint for line in lines].count(True) == 1
    assert not any(CARDHOLDER in value for value in setup.stored_text(doc))


@pytest.mark.parametrize(
    ("printed", "changed"),
    [
        ("9913   $646.95", "9913   $646.96"),
        ("Sumac New Total   $1,064.18", "Sumac New Total   $1,064.19"),
    ],
)
def test_an_sumac_statement_a_cent_off_is_rejected_with_zero_lines(
    setup: Setup, printed: str, changed: str
) -> None:
    setup.register(SUMAC)
    text = fixture("sumac_statement.txt")
    assert text.count(printed) == 1
    doc = setup.file(SUMAC_STATEMENT, text.replace(printed, changed))

    assert setup.parse(doc) == Parsed("rejected", "summary_mismatch")
    assert setup.count(document_line, doc) == 0
    assert setup.rejected_reasons(doc) == ["summary_mismatch"]


@pytest.mark.parametrize(
    ("printed", "changed"),
    [
        # A section total a cent off its rows; the balances still tie.
        ("Sumac Sum   $624.91", "Sumac Sum   $624.92"),
        ("Sumac Sum   -$627.66", "Sumac Sum   -$627.67"),
        # The summary's payments figure a cent off the payments section's rows.
        ("Sumac Paid In   -$627.66", "Sumac Paid In   -$627.67"),
    ],
)
def test_an_sumac_section_or_payments_total_a_cent_off_is_rejected(
    setup: Setup, printed: str, changed: str
) -> None:
    setup.register(SUMAC)
    text = fixture("sumac_statement.txt")
    assert text.count(printed) == 1
    doc = setup.file(SUMAC_STATEMENT, text.replace(printed, changed))

    assert setup.parse(doc) == Parsed("rejected", "summary_mismatch")
    assert setup.count(document_line, doc) == 0
    assert setup.count(statement_period, doc) == 0
    assert setup.count(rejected_period, doc) == 1
    assert setup.rejected_reasons(doc) == ["summary_mismatch"]


def test_an_sumac_store_number_stays_in_the_description(setup: Setup) -> None:
    """Only a four-digit number in a column of its own is the reference."""
    setup.register(SUMAC)
    text = fixture("sumac_statement.txt").replace(
        "SUMAC INTEREST ADDED   $64.63", "SUMAC INTEREST ADDED 9917   $64.63"
    )
    doc = setup.file(SUMAC_STATEMENT, text)

    assert setup.parse(doc).status == "parsed"
    assert [line.description_raw for line in setup.lines(doc)] == [
        "SUMAC PAID",
        "SAMPLE STREAMING SAMPLE CITY ZZ",
        "SAMPLE HARDWARE SAMPLE TOWN ZZ",
        "SAMPLE HARDWARE SAMPLE TOWN ZZ",
        "SUMAC INTEREST ADDED 9917",
    ]


def test_an_sumac_statement_with_no_printed_minimum_has_a_null_minimum(
    setup: Setup,
) -> None:
    setup.register(SUMAC)
    text = "\n".join(
        line
        for line in fixture("sumac_statement.txt").splitlines()
        if "Sumac Least Payment   $" not in line
    )
    doc = setup.file(SUMAC_STATEMENT, text)

    assert setup.parse(doc).status == "parsed"
    period = setup.period(doc)
    assert period.minimum_due is None
    assert (period.balance_due, period.due_date) == (
        Decimal("1064.18"), dt.date(2042, 11, 13),
    )  # fmt: skip


def test_an_sumac_statement_for_an_unregistered_card_is_rejected(setup: Setup) -> None:
    setup.register(NEEM)
    doc = setup.file(SUMAC_STATEMENT, fixture("sumac_statement.txt"))

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")


# --- the last-four match's scope ------------------------------------------------------


def test_a_last_four_resolves_only_at_the_statements_institution(setup: Setup) -> None:
    """Another institution's card ending in the same four digits is no candidate."""
    same_four_elsewhere = card("sumac-card", "Sumac", "**** **** **** 9991")
    setup.register(NEEM, same_four_elsewhere)
    doc = setup.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))

    assert setup.parse(doc).status == "parsed"
    assert setup.period(doc).account_id == account_id("neem")


def test_a_last_four_two_accounts_hold_is_ambiguous_never_a_pick(setup: Setup) -> None:
    """Two distinct full numbers of two accounts may end alike (the loader keeps
    both); a statement matched by its last four then names neither."""
    setup.register(
        ManualAccount("neem", "Alder", "credit_card", ("999000009991",)),
        ManualAccount("other-neem", "Alder", "credit_card", ("999000019991",)),
    )
    doc = setup.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))

    assert setup.parse(doc) == Parsed("rejected", "ambiguous_account")
    assert setup.count(document_line, doc) == 0
    assert setup.rejected_reasons(doc) == ["ambiguous_account"]


def test_an_identifier_of_a_kind_outside_the_last_four_kinds_matches_nothing(
    setup: Setup,
) -> None:
    setup.register(dataclasses.replace(NEEM, identifier_kind="file_token"))
    doc = setup.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")


# --- registration --------------------------------------------------------------------


def test_the_card_parsers_register_as_hint_setting() -> None:
    """Their lines carry hints, so a patterns edit must parse their documents again."""
    registered = {
        parser.parser_id: (parser.version, parser.sets_hints)
        for issuer, fmt in (("alder-neem", "pdf"), ("alder-neem", "csv"),
                            ("sumac", "pdf"))
        for parser in parsers_for(issuer, fmt)
    }  # fmt: skip
    assert registered == {
        "alder_neem.statement": ("1", True),
        "alder_neem.transactions": ("transactions-1", True),
        "alder_neem.csv": ("1", True),
        "sumac.statement": ("1", True),
    }
