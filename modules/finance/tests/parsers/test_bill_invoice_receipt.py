"""The Boxwood bill, the invoices received and the patient receipt through
``parse_document`` (FR-3, FR-8, FR-9; AC-8 parser part).

Seam: ``parse_document`` over a synthetic document filed into a temporary archive.
The bill is one ``bill_identity`` period matched by its account number's last four;
the invoice and the receipt give account-less ``invoice`` and ``receipt`` lines and no
period, and the registry's summary check leaves those lines alone. The invoice and
receipt parsers are registered for their nested directories and read every
sub-directory's files. A photo stays ``needs_manual_entry``. Every value is invented;
the fixtures are layout text, read as text where the real run calls pdftotext.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, func, select

from rheo_finance.context import UnitOfWork
from rheo_finance.documents.registry import document_id, file_archive
from rheo_finance.manual.stage2 import account_id
from rheo_finance.parsers.registry import Parsed, parse_document, parsers_for
from rheo_finance.storage.schema import (
    document,
    document_line,
    rejected_period,
    statement_period,
)

from .conftest import ManualAccount, register_accounts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
NOW = dt.datetime(2041, 3, 12, 12, 0, tzinfo=dt.UTC)
D = dt.date
BOXWOOD_NUMBER = "9990001234"


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

    def register_boxwood(self, identifier: str = BOXWOOD_NUMBER) -> None:
        register_accounts(
            self.uow,
            self.data,
            ManualAccount("utility", "Boxwood", "bill_vendor", (identifier,)),
        )

    def file(self, relative: str, data: bytes | str) -> uuid.UUID:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        target = self.archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        file_archive(self.uow, self.archive, self.data, NOW)
        return document_id(hashlib.sha256(raw).hexdigest())

    def parse(self, doc: uuid.UUID) -> Parsed:
        return parse_document(self.uow, doc, self.archive, extract_pdf=read_as_text)

    def document(self, doc: uuid.UUID) -> Any:
        return self.conn.execute(select(document).where(document.c.id == doc)).one()

    def periods(self, doc: uuid.UUID) -> list[Any]:
        query = select(statement_period).where(statement_period.c.document_id == doc)
        return list(self.conn.execute(query).all())

    def lines(self, doc: uuid.UUID) -> list[Any]:
        return list(
            self.conn.execute(
                select(document_line)
                .where(document_line.c.document_id == doc)
                .order_by(document_line.c.line_no)
            ).all()
        )

    def rejected_periods(self, doc: uuid.UUID) -> int:
        query = select(func.count()).select_from(rejected_period)
        query = query.where(rejected_period.c.document_id == doc)
        return int(self.conn.scalar(query) or 0)

    def stored_text(self, doc: uuid.UUID) -> str:
        found: list[str] = []
        for table in (document, statement_period, document_line):
            key = table.c.id if table is document else table.c.document_id
            for row in self.conn.execute(select(table).where(key == doc)):
                found += [str(value).casefold() for value in row if value is not None]
        return "\n".join(found)


@pytest.fixture
def setup(owner_conn: Connection, tmp_path: Path) -> Setup:
    return Setup(owner_conn, tmp_path)


def changed(name: str, printed: str, replacement: str) -> str:
    text = fixture(name)
    assert text.count(printed) == 1, printed
    return text.replace(printed, replacement)


# --- Boxwood -------------------------------------------------------------------------

BILL = "boxwood/2029-01-26.pdf"
BILL_TEXT = "boxwood_bill.txt"


def test_the_bill_is_one_tied_out_period_over_its_billing_period(setup: Setup) -> None:
    setup.register_boxwood()
    doc = setup.file(BILL, fixture(BILL_TEXT))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=0)
    row = setup.document(doc)
    assert (row.parser_id, row.parser_version) == ("boxwood.bill", "3")
    (period,) = setup.periods(doc)
    assert period.account_id == account_id("utility")
    assert (period.control_kind, period.tied_out, period.currency) == (
        "bill_identity", True, "CAD",
    )  # fmt: skip
    assert (period.period_start, period.period_end) == (D(2028, 12, 19), D(2029, 1, 22))
    assert (period.balance_due, period.due_date) == (
        Decimal("494.66"), D(2029, 2, 16),
    )  # fmt: skip
    assert BOXWOOD_NUMBER not in setup.stored_text(doc)
    assert "sample customer" not in setup.stored_text(doc)


@pytest.mark.parametrize(
    ("printed", "replacement"),
    [
        ("Current charges   $333.84", "Current charges   $333.85"),
        ("Balance forward   $160.82", "Balance forward   $160.83"),
        ("Jan 13, 2029   -$38.22", "Jan 13, 2029   -$38.23"),
    ],
)
def test_a_bill_whose_identity_breaks_is_rejected(
    setup: Setup, printed: str, replacement: str
) -> None:
    setup.register_boxwood()
    doc = setup.file(BILL, changed(BILL_TEXT, printed, replacement))

    assert setup.parse(doc) == Parsed("rejected", "identity_mismatch")
    assert setup.periods(doc) == []
    assert setup.rejected_periods(doc) == 1


def test_a_bill_that_prints_no_identity_loads_as_printed(setup: Setup) -> None:
    setup.register_boxwood()
    doc = setup.file(
        BILL, changed(BILL_TEXT, "   Previous bill amount   $313.70\n", "")
    )

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=0)
    (period,) = setup.periods(doc)
    assert (period.tied_out, period.balance_due) == (False, Decimal("494.66"))


def test_an_unregistered_boxwood_account_rejects_the_bill(setup: Setup) -> None:
    setup.register_boxwood("9990009999")
    doc = setup.file(BILL, fixture(BILL_TEXT))

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")


def test_a_payment_stub_that_disagrees_is_not_recognised(setup: Setup) -> None:
    setup.register_boxwood()
    doc = setup.file(
        BILL, changed(BILL_TEXT, "Box stub owing   $494.66", "Box stub owing   $494.67")
    )

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_a_bill_photo_stays_in_the_manual_entry_queue(setup: Setup) -> None:
    setup.register_boxwood()
    doc = setup.file("boxwood/2029-01-26-bill.jpeg", b"\xff\xd8 synthetic photo")

    assert setup.parse(doc) == Parsed("needs_manual_entry", None)
    assert setup.document(doc).status == "needs_manual_entry"
    assert setup.lines(doc) == []


def test_the_bill_is_filed_by_its_bill_date() -> None:
    (parser,) = parsers_for("boxwood", "pdf")
    assert parser.filing_date is not None
    assert parser.filing_date(fixture(BILL_TEXT)) == D(2029, 1, 26)


# --- invoices received ----------------------------------------------------------------

INVOICE_TEXT = "invoice_received.txt"


@pytest.mark.parametrize(
    "relative",
    [
        "invoices-received/sample-vendor/2047-06-03.pdf",
        "invoices-received/2047-06-03.pdf",
    ],
)
def test_an_invoice_gives_one_account_less_invoice_line(
    setup: Setup, relative: str
) -> None:
    """Filed in a vendor's sub-directory or directly in the directory, the invoice is
    read by the one parser registered for ``invoices-received``."""
    doc = setup.file(relative, fixture(INVOICE_TEXT))

    assert setup.parse(doc) == Parsed("parsed", None, periods=0, lines=1)
    row = setup.document(doc)
    assert (row.parser_id, row.parser_version, row.kind) == (
        "invoices_received.invoice", "3", "invoice",
    )  # fmt: skip
    (line,) = setup.lines(doc)
    assert (line.line_kind, line.account_id, line.posting_date) == (
        "invoice", None, D(2047, 6, 3),
    )  # fmt: skip
    # The fixture is part-paid (110.40 of 828.00): the line is the total all the same.
    assert (line.amount, line.currency) == (Decimal("-828.00"), "CAD")
    assert line.amount_due == Decimal("717.60")
    assert (line.description_raw, line.counterparty) == (
        "Inv number SP0042", "sample contractor",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("paid", "due"),
    [("828.00", "0.00"), ("110.40", "717.60"), ("0.00", "828.00")],
    ids=["paid_in_full", "part_paid", "unpaid"],
)
def test_an_invoice_line_is_its_total_whatever_is_still_due(
    setup: Setup, paid: str, due: str
) -> None:
    """The line's amount is the invoice total, so a payment of the whole invoice can
    match it even once the invoice prints itself paid in full; what is still due is
    stored beside it."""
    text = changed(INVOICE_TEXT, "Inv paid   $110.40", f"Inv paid   ${paid}")
    assert text.count("Inv owing   $717.60") == 1
    text = text.replace("Inv owing   $717.60", f"Inv owing   ${due}")
    doc = setup.file("invoices-received/sample-vendor/2047-06-03.pdf", text)

    assert setup.parse(doc) == Parsed("parsed", None, periods=0, lines=1)
    (line,) = setup.lines(doc)
    assert (line.amount, line.currency) == (Decimal("-828.00"), "CAD")
    assert line.amount_due == Decimal(due)


@pytest.mark.parametrize(
    ("printed", "replacement"),
    [
        ("Inv owing   $717.60", "Inv owing   $717.61"),
        ("$75.55   $276.00", "$75.55   $276.01"),
        ("Inv paid   $110.40", "Inv paid   $110.41"),
    ],
)
def test_an_invoice_whose_identity_breaks_is_rejected(
    setup: Setup, printed: str, replacement: str
) -> None:
    doc = setup.file(
        "invoices-received/sample-vendor/2047-06-03.pdf",
        changed(INVOICE_TEXT, printed, replacement),
    )

    assert setup.parse(doc) == Parsed("rejected", "identity_mismatch")
    assert setup.lines(doc) == []


@pytest.mark.parametrize(
    "inserted", ["   GST   $34.16\n", "  Extra work   1   $34.16   $34.16\n"]
)
def test_an_invoice_with_a_line_it_does_not_print_is_unparsed(
    setup: Setup, inserted: str
) -> None:
    """A tax line, or an item-shaped line, between the subtotal and the total is a
    layout this parser has not seen: the invoice is unparsed, never read with the
    line ignored or counted as an item."""
    doc = setup.file(
        "invoices-received/sample-vendor/2047-06-03.pdf",
        changed(
            INVOICE_TEXT,
            "   Inv total   $828.00\n",
            f"{inserted}   Inv total   $862.16\n",
        ),
    )

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_only_the_payee_and_the_number_of_an_invoice_are_stored(setup: Setup) -> None:
    doc = setup.file(
        "invoices-received/sample-vendor/2047-06-03.pdf", fixture(INVOICE_TEXT)
    )
    setup.parse(doc)

    stored = setup.stored_text(doc)
    for value in ("sample customer", "work from", "2047-06-17", "sample.test"):
        assert value not in stored


# --- receipts -------------------------------------------------------------------------

RECEIPT = "receipts/sample-clinic/2036-05-24.pdf"
RECEIPT_TEXT = "patient_receipt.txt"


def test_a_receipt_gives_one_receipt_line_per_payment(setup: Setup) -> None:
    doc = setup.file(RECEIPT, fixture(RECEIPT_TEXT))

    assert setup.parse(doc) == Parsed("parsed", None, periods=0, lines=2)
    row = setup.document(doc)
    assert (row.parser_id, row.parser_version, row.kind) == (
        "receipts.patient_receipt", "2", "receipt",
    )  # fmt: skip
    shape = [
        (
            line.line_kind, line.account_id, line.posting_date, line.amount,
            line.currency, line.description_raw, line.counterparty,
        )
        for line in setup.lines(doc)
    ]  # fmt: skip
    assert shape == [
        ("receipt", None, D(2036, 5, 22), Decimal("-230.80"), "CAD",
         "Patient receipt: Cash", "sample dental clinic"),
        ("receipt", None, D(2036, 5, 23), Decimal("-31.97"), "CAD",
         "Patient receipt: Debit", "sample dental clinic"),
    ]  # fmt: skip
    # Each payment's own number is stored as a digest only, one per payment.
    digests = [line.payment_ref_digest for line in setup.lines(doc)]
    assert all(len(each) == 16 for each in digests)
    assert len(set(digests)) == 2


def test_a_payment_with_no_number_has_no_reference_digest(setup: Setup) -> None:
    doc = setup.file(
        RECEIPT, changed(RECEIPT_TEXT, "Pmt. Payment #999123 - ", "Pmt. Payment - ")
    )

    assert setup.parse(doc) == Parsed("parsed", None, periods=0, lines=2)
    assert [line.payment_ref_digest is None for line in setup.lines(doc)] == [
        True, False,
    ]  # fmt: skip


def test_payments_that_miss_the_total_reject_the_receipt(setup: Setup) -> None:
    text = changed(
        RECEIPT_TEXT, "Slip paid total   $262.77", "Slip paid total   $262.78"
    )
    doc = setup.file(RECEIPT, text)

    assert setup.parse(doc) == Parsed("rejected", "checks_failed")
    assert setup.lines(doc) == []


def test_the_patient_and_the_treatments_are_stored_nowhere(setup: Setup) -> None:
    doc = setup.file(RECEIPT, fixture(RECEIPT_TEXT))
    setup.parse(doc)

    stored = setup.stored_text(doc)
    for value in ("sample patient", "patient, sample", "box 99", "exam", "999123"):
        assert value not in stored


def test_another_receipt_layout_is_unparsed(setup: Setup) -> None:
    doc = setup.file("receipts/sample-shop/2036-05-24.pdf", "SAMPLE SHOP\nTOTAL 9.31\n")

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_a_receipt_photo_stays_in_the_manual_entry_queue(setup: Setup) -> None:
    doc = setup.file(
        "receipts/sample-clinic/2036-05-24.png", b"\x89PNG synthetic photo"
    )

    assert setup.parse(doc) == Parsed("needs_manual_entry", None)
    assert setup.lines(doc) == []


def test_invoices_and_receipts_are_filed_by_their_dates() -> None:
    (invoice,) = parsers_for("invoices-received", "pdf")
    (receipt,) = parsers_for("receipts/sample-clinic", "pdf")
    assert invoice.filing_date is not None and receipt.filing_date is not None
    assert invoice.filing_date(fixture(INVOICE_TEXT)) == D(2047, 6, 3)
    assert receipt.filing_date(fixture(RECEIPT_TEXT)) == D(2036, 5, 24)
