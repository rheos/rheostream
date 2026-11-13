"""Receipts and invoices in derive stage 3 (FR-8, FR-16; AC-8 matching clauses).

Seam: ``derive.run`` over a synthetic chequing export (the payment lines) beside the
synthetic patient receipt or invoice, read back through ``txn`` and ``txn_source``. A
receipt or invoice line that matches a payment line (payee, amount, date window) is an
``evidence`` source of that payment and adds no txn; an unmatched receipt is one txn
on "Paid outside recorded accounts" at its date; an unmatched invoice, however old,
adds no txn and nothing to any spending total. Every value is synthetic.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from rheo_finance.derive.run import Derived, run
from rheo_finance.derive.stage3 import build_lines
from rheo_finance.manual.cash_lines import CashLines
from rheo_finance.manual.payees import Payees
from rheo_finance.manual.stage2 import account_id
from rheo_finance.storage.schema import document, document_line, txn, txn_source

from .support import FIXTURES, D, Ledger, csv_row, read_as_text

RECEIPT = "receipts/sample-clinic/2036-05-24.pdf"
INVOICE = "invoices-received/sample-vendor/2047-06-03.pdf"
PAID_OUTSIDE = account_id("paid-outside")
CLINIC = "POS PURCHASE   SAMPLE DENTAL CLINIC   99907"
CONTRACTOR = "E-SEND SENT   SAMPLE CONTRACTOR   99908"
OTHER_SHOP = "POS PURCHASE   SAMPLE HARDWARE   99904"
CASH_ROW = "22-May-36   Cash   Sample Provider #999123 - Sample Visit   $230.80\n"
DEBIT_ROW = "23-May-36   Debit   Sample Provider #999124 - Sample Visit   $31.97\n"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def changed(text: str, printed: str, replacement: str) -> str:
    assert text.count(printed) == 1, printed
    return text.replace(printed, replacement)


def debit_only_receipt() -> str:
    """The receipt with its 31.97 debit payment of 23 May alone."""
    text = changed(fixture("patient_receipt.txt"), CASH_ROW, "")
    return changed(text, "Slip paid total   $262.77", "Slip paid total   $31.97")


def invoice(issued: str = "Mon, Jun 3, 2047") -> str:
    return changed(fixture("invoice_received.txt"), "Mon, Jun 3, 2047", issued)


def clinic_invoice(total: str, issued: str) -> str:
    """The clinic's invoice of one item for ``total``, paid in full."""
    text = changed(invoice(issued), "Sample Contractor", "Sample Dental Clinic")
    for printed, replacement in (
        ("$75.55   $552.00", f"$75.55   ${total}"),
        ("Inv subtotal   $828.00", f"Inv subtotal   ${total}"),
        ("Inv total   $828.00", f"Inv total   ${total}"),
        ("Inv paid   $110.40", f"Inv paid   ${total}"),
        ("Inv owing   $717.60", "Inv owing   $0.00"),
        ("  Rsit tco Sumd 99th to Ipis 99th   3.50   $75.55   $276.00\n", ""),
    ):
        text = changed(text, printed, replacement)
    return text


def receipt(*rows: tuple[str, str, str, str]) -> str:
    """The patient receipt with these ``(day, type, amount, payment number)``
    payments in place of its own two."""
    text = changed(fixture("patient_receipt.txt"), CASH_ROW + DEBIT_ROW, "".join(
        f"{day}   {kind}   Pmt. Payment #{number} - Patient, Sample   ${amount}\n"
        for day, kind, amount, number in rows
    ))  # fmt: skip
    total = sum((Decimal(amount) for _, _, amount, _ in rows), Decimal(0))
    return changed(text, "Slip paid total   $262.77", f"Slip paid total   ${total}")


def payments(
    ledger: Ledger, *rows: tuple[str, str, str], opening: str = "2055/01/01"
) -> None:
    """A chequing export of ``(day, description, amount out)`` rows, newest first,
    above an opening deposit of 1192.07 on ``opening``, a day older than every row."""
    balance = Decimal("1192.07") - sum((Decimal(out) for _, _, out in rows), Decimal(0))
    lines = []
    for day, description, out in rows:
        lines.append(csv_row(day, description, out=out, balance=f"{balance:.2f}"))
        balance += Decimal(out)
    lines.append(
        csv_row(opening, "DEPOSIT   SAMPLE PAYROLL   99906", into="1192.07",
                balance="1192.07")
    )  # fmt: skip
    ledger.chequing()
    ledger.file("alder/export.csv", "\n".join(lines) + "\n")


def spending(ledger: Ledger) -> Decimal:
    """Every outflow in the ledger: the sum of the negative txns."""
    total = ledger.conn.scalar(select(func.sum(txn.c.amount)).where(txn.c.amount < 0))
    return Decimal(total or 0)


def sources(ledger: Ledger, role: str) -> list[Any]:
    return list(
        ledger.conn.execute(
            select(txn_source, document_line.c.line_kind, document_line.c.amount)
            .join(document_line, document_line.c.id == txn_source.c.document_line_id)
            .where(txn_source.c.role == role)
            .order_by(document_line.c.posting_date)
        ).all()
    )


def txn_of(ledger: Ledger, description: str) -> uuid.UUID:
    norm = " ".join(description.casefold().split())
    (found,) = [row["id"] for row in ledger.txns() if row["description_norm"] == norm]
    assert isinstance(found, uuid.UUID)
    return found


def counts(out: Derived) -> tuple[int, int, int, int]:
    lines = out.lines
    return (
        lines.from_documents, lines.evidence_links, lines.from_receipts,
        lines.unmatched_invoices,
    )  # fmt: skip


# --- receipts -------------------------------------------------------------------------


def test_a_matched_receipt_is_evidence_and_adds_no_txn(ledger: Ledger) -> None:
    payments(ledger, ("2036/05/23", CLINIC, "31.97"))
    ledger.file(RECEIPT, debit_only_receipt())

    out = ledger.derive()

    assert counts(out) == (2, 1, 0, 0)
    assert len(ledger.txns()) == 2
    assert spending(ledger) == Decimal("-31.97")
    (link,) = sources(ledger, "evidence")
    assert (link.txn_id, link.line_kind, link.amount) == (
        txn_of(ledger, CLINIC), "receipt", Decimal("-31.97"),
    )  # fmt: skip
    assert [r for r in ledger.txns() if r["account_id"] == PAID_OUTSIDE] == []


def test_an_unmatched_receipt_is_one_txn_on_paid_outside(ledger: Ledger) -> None:
    """The cash payment of 22 May has no payment line: it is its own txn on the
    internal account at its date. The debit of 23 May matches the card line and
    counts once."""
    payments(ledger, ("2036/05/23", CLINIC, "31.97"))
    ledger.file(RECEIPT, fixture("patient_receipt.txt"))

    out = ledger.derive()

    assert counts(out) == (2, 1, 1, 0)
    assert out.lines.txns == 3
    (outside,) = [r for r in ledger.txns() if r["account_id"] == PAID_OUTSIDE]
    assert (outside["posting_date"], outside["amount"], outside["currency"]) == (
        D(2036, 5, 22), Decimal("-230.80"), "CAD",
    )  # fmt: skip
    assert (outside["origin"], outside["counterparty"]) == (
        "document_expense", "sample dental clinic",
    )  # fmt: skip
    (primary,) = [
        row for row in sources(ledger, "primary") if row.txn_id == outside["id"]
    ]
    assert (primary.line_kind, primary.line_key_id) == ("receipt", outside["id"])
    assert spending(ledger) == Decimal("-262.77")


@pytest.mark.parametrize(
    ("day", "matched"),
    [("2036/05/26", True), ("2036/05/27", False), ("2036/05/19", False)],
    ids=["three_days_after", "four_days_after", "four_days_before"],
)
def test_a_receipt_matches_inside_its_window_only(
    ledger: Ledger, day: str, matched: bool
) -> None:
    payments(ledger, (day, CLINIC, "31.97"))
    ledger.file(RECEIPT, debit_only_receipt())

    out = ledger.derive()

    assert (out.lines.evidence_links, out.lines.from_receipts) == (
        (1, 0) if matched else (0, 1)
    )
    assert spending(ledger) == Decimal("-31.97") * (1 if matched else 2)


def test_a_receipt_needs_its_payee_on_the_payment_line(ledger: Ledger) -> None:
    payments(ledger, ("2036/05/23", OTHER_SHOP, "31.97"))
    ledger.file(RECEIPT, debit_only_receipt())

    out = ledger.derive()

    assert counts(out) == (2, 0, 1, 0)
    assert spending(ledger) == Decimal("-63.94")


def test_a_receipt_needs_the_same_amount(ledger: Ledger) -> None:
    payments(ledger, ("2036/05/23", CLINIC, "36.15"))
    ledger.file(RECEIPT, debit_only_receipt())

    assert counts(ledger.derive()) == (2, 0, 1, 0)


def test_two_receipt_payments_never_share_one_payment_line(ledger: Ledger) -> None:
    """Two debits of 31.97 a day apart and one card line of 31.97 on the second day:
    the exact date takes the line, and the other debit is paid outside."""
    text = changed(
        fixture("patient_receipt.txt"),
        CASH_ROW,
        CASH_ROW.replace("Cash", "Debit").replace("$230.80", "$31.97"),
    )
    payments(ledger, ("2036/05/23", CLINIC, "31.97"))
    ledger.file(RECEIPT, changed(text, "$262.77", "$63.94"))

    out = ledger.derive()

    assert counts(out) == (2, 1, 1, 0)
    (outside,) = [r for r in ledger.txns() if r["account_id"] == PAID_OUTSIDE]
    assert outside["posting_date"] == D(2036, 5, 22)
    assert spending(ledger) == Decimal("-63.94")


def test_a_second_copy_of_a_receipt_adds_nothing(ledger: Ledger) -> None:
    """A re-saved copy (other bytes, the same lines) is the same receipt: its lines
    are evidence of the txns the first copy reached, and nothing counts twice."""
    payments(ledger, ("2036/05/23", CLINIC, "31.97"))
    text = fixture("patient_receipt.txt")
    ledger.file(RECEIPT, text)
    ledger.file(
        "receipts/sample-clinic/2036-05-24-copy.pdf", text.replace("\n", "\r\n")
    )

    out = ledger.derive()

    assert out.documents == {"parsed": 3}
    assert counts(out) == (2, 3, 1, 0)
    assert out.lines.txns == 3
    assert spending(ledger) == Decimal("-262.77")


# --- invoices -------------------------------------------------------------------------


@pytest.mark.parametrize("issued", ["Mon, Jun 3, 2047", "Wed, Dec 1, 2055"])
def test_an_unmatched_invoice_adds_no_txn_and_no_spending(
    ledger: Ledger, issued: str
) -> None:
    """However old, an invoice no payment line matches is not spending."""
    payments(ledger, ("2036/05/23", OTHER_SHOP, "31.97"))
    ledger.file(INVOICE, invoice(issued))

    out = ledger.derive()

    assert counts(out) == (2, 0, 0, 1)
    assert len(ledger.txns()) == 2
    assert spending(ledger) == Decimal("-31.97")
    assert sources(ledger, "evidence") == []
    line_count = select(func.count()).select_from(txn_source)
    line_count = line_count.join(
        document_line, document_line.c.id == txn_source.c.document_line_id
    ).where(document_line.c.line_kind == "invoice")
    assert ledger.conn.scalar(line_count) == 0


@pytest.mark.parametrize(
    ("day", "matched"),
    [("2047/07/03", True), ("2047/07/04", False), ("2047/05/04", True)],
    ids=["thirty_days_after", "thirty_one_days_after", "thirty_days_before"],
)
def test_an_invoice_matches_its_payment_inside_thirty_days(
    ledger: Ledger, day: str, matched: bool
) -> None:
    """The invoice total, 828.00, paid to the contractor."""
    payments(ledger, (day, CONTRACTOR, "828.00"))
    ledger.file(INVOICE, invoice())

    out = ledger.derive()

    assert counts(out) == ((2, 1, 0, 0) if matched else (2, 0, 0, 1))
    assert spending(ledger) == Decimal("-828.00")
    links = sources(ledger, "evidence")
    assert [(each.txn_id, each.line_kind) for each in links] == (
        [(txn_of(ledger, CONTRACTOR), "invoice")] if matched else []
    )


def test_an_invoice_and_its_receipt_both_attach_to_one_payment(
    ledger: Ledger,
) -> None:
    """Matching is one to one within each kind: a paid invoice never pushes the
    receipt of the same payment out to the internal account."""
    payments(ledger, ("2036/05/23", CLINIC, "31.97"))
    ledger.file(RECEIPT, debit_only_receipt())
    ledger.file(INVOICE, clinic_invoice("31.97", "Fri, May 23, 2036"))

    out = ledger.derive()

    assert counts(out) == (2, 2, 0, 0)
    clinic = txn_of(ledger, CLINIC)
    links = sorted(
        (each.line_kind, each.txn_id) for each in sources(ledger, "evidence")
    )
    assert links == [("invoice", clinic), ("receipt", clinic)]
    assert spending(ledger) == Decimal("-31.97")


def test_receipt_and_invoice_lines_store_their_keys(ledger: Ledger) -> None:
    payments(ledger, ("2036/05/23", CLINIC, "31.97"))
    ledger.file(RECEIPT, fixture("patient_receipt.txt"))
    ledger.file(INVOICE, invoice())

    ledger.derive()

    rows = ledger.conn.execute(
        select(document_line.c.line_key_id, document_line.c.description_norm).where(
            document_line.c.line_kind.in_(("receipt", "invoice"))
        )
    ).all()
    assert len(rows) == 3
    assert all(row.line_key_id is not None for row in rows)
    assert len({row.line_key_id for row in rows}) == 3
    assert {row.description_norm for row in rows} == {
        "patient receipt: cash", "patient receipt: debit", "invoice sp0042",
    }  # fmt: skip


# --- maximal matching, order and re-derives -------------------------------------------


def test_matching_is_maximal_not_greedy(ledger: Ledger) -> None:
    """Receipt payments on 18 and 22 May, card lines on 15 and 19 May. The 18th
    is nearest the 19th, but taking it there would strand the 22nd: both match, the
    18th to the 15th and the 22nd to the 19th, and nothing is paid outside."""
    payments(
        ledger,
        ("2036/05/19", CLINIC, "31.97"),
        ("2036/05/15", CLINIC, "31.97"),
        opening="2036/05/14",
    )
    ledger.file(
        RECEIPT,
        receipt(
            ("18-May-36", "Debit", "31.97", "999131"),
            ("22-May-36", "Debit", "31.97", "999132"),
        ),
    )

    out = ledger.derive()

    assert counts(out) == (3, 2, 0, 0)
    assert spending(ledger) == Decimal("-63.94")
    pairs = ledger.conn.execute(
        select(document_line.c.posting_date, txn.c.posting_date)
        .join(txn_source, txn_source.c.document_line_id == document_line.c.id)
        .join(txn, txn.c.id == txn_source.c.txn_id)
        .where(txn_source.c.role == "evidence")
        .order_by(document_line.c.posting_date)
    ).all()
    assert [tuple(each) for each in pairs] == [
        (D(2036, 5, 18), D(2036, 5, 15)), (D(2036, 5, 22), D(2036, 5, 19)),
    ]  # fmt: skip


def test_an_invoice_attaches_to_a_cash_receipt_paid_outside(ledger: Ledger) -> None:
    """The clinic's invoice for the cash payment its receipt shows: the receipt is
    the payment (on the internal account) and the invoice is its evidence, so
    nothing stays open and nothing counts twice."""
    payments(ledger, ("2036/05/23", CLINIC, "31.97"))
    ledger.file(RECEIPT, fixture("patient_receipt.txt"))
    ledger.file(INVOICE, clinic_invoice("230.80", "Thu, May 22, 2036"))

    out = ledger.derive()

    assert counts(out) == (2, 2, 1, 0)
    (outside,) = [r for r in ledger.txns() if r["account_id"] == PAID_OUTSIDE]
    invoice_links = [
        each.txn_id
        for each in sources(ledger, "evidence")
        if each.line_kind == "invoice"
    ]
    assert invoice_links == [outside["id"]]
    assert spending(ledger) == Decimal("-262.77")


def test_a_payment_lines_counterparty_never_names_the_payee(ledger: Ledger) -> None:
    """The interim payee rule reads a statement or export line's description only:
    a payment of the invoice total whose description does not name the contractor
    is not the invoice's payment, even when its counterparty does."""
    unnamed = "E-SEND SENT   99909"
    payments(ledger, ("2061/12/31", unnamed, "828.00"))
    ledger.file(INVOICE, invoice())
    assert counts(ledger.derive()) == (2, 0, 0, 1)
    ledger.conn.execute(
        document_line.update()
        .where(document_line.c.description_raw == unnamed)
        .values(counterparty="Sample Contractor")
    )

    lines = rerun_stage3(ledger)

    (payment,) = [r for r in ledger.txns() if r["amount"] == Decimal("-828.00")]
    assert payment["counterparty"] == "Sample Contractor"
    assert (lines.evidence_links, lines.unmatched_invoices) == (0, 1)
    assert sources(ledger, "evidence") == []


def test_a_payment_line_that_arrives_later_takes_the_receipt(ledger: Ledger) -> None:
    """Before the chequing export is filed the receipt is paid outside; once it is,
    the receipt is evidence of the card line and the internal txn is gone."""
    ledger.file(RECEIPT, debit_only_receipt())
    first = ledger.derive()
    assert counts(first) == (0, 0, 1, 0)
    assert spending(ledger) == Decimal("-31.97")

    payments(ledger, ("2036/05/23", CLINIC, "31.97"))
    second = ledger.derive()

    assert counts(second) == (2, 1, 0, 0)
    assert [r for r in ledger.txns() if r["account_id"] == PAID_OUTSIDE] == []
    assert spending(ledger) == Decimal("-31.97")


def test_derive_and_rebuild_give_the_same_links(ledger: Ledger) -> None:
    payments(ledger, ("2047/06/18", CONTRACTOR, "828.00"), ("2036/05/23", CLINIC,
             "31.97"), opening="2036/05/22")  # fmt: skip
    ledger.file(RECEIPT, fixture("patient_receipt.txt"))
    ledger.file(INVOICE, invoice())

    def snapshot() -> tuple[list[str], list[str]]:
        return (
            sorted(repr(tuple(row)) for row in ledger.conn.execute(select(txn))),
            sorted(repr(tuple(row)) for row in ledger.conn.execute(select(txn_source))),
        )

    derived = ledger.derive()
    after_derive = snapshot()
    rebuilt = run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, reparse=True)

    assert counts(derived) == counts(rebuilt) == (3, 2, 1, 0)
    assert snapshot() == after_derive


@pytest.mark.parametrize("resaved_first", [True, False])
def test_filing_order_changes_neither_the_primary_copy_nor_the_totals(
    ledger: Ledger, resaved_first: bool
) -> None:
    """Two copies of one receipt: the copy of the larger sha256 is the primary source
    of the internal txn, whichever was filed first."""
    text = fixture("patient_receipt.txt")
    copies = [
        ("receipts/sample-clinic/2036-05-24.pdf", text),
        ("receipts/sample-clinic/2036-05-24-resaved.pdf", text.replace("\n", "\r\n")),
    ]
    if resaved_first:
        copies.reverse()
    docs = {}
    for relative, content in copies:
        docs[ledger.file(relative, content)] = content
        out = ledger.derive()

    larger = max(docs, key=lambda doc: hashlib.sha256(docs[doc].encode()).hexdigest())
    assert counts(out) == (0, 2, 2, 0)
    primaries = ledger.conn.execute(
        select(document_line.c.document_id)
        .join(txn_source, txn_source.c.document_line_id == document_line.c.id)
        .where(txn_source.c.role == "primary")
    ).scalars()
    assert set(primaries) == {larger}
    assert spending(ledger) == Decimal("-262.77")


def test_two_genuine_receipts_with_identical_payments_count_twice(
    ledger: Ledger,
) -> None:
    """Two receipts in two documents print the same date, type and amount but their
    own payment numbers: two payments. A re-saved copy of one of them is the same
    payment again and adds nothing."""
    one = receipt(("23-May-36", "Debit", "31.97", "999141"))
    two = receipt(("23-May-36", "Debit", "31.97", "999142"))
    ledger.file("receipts/sample-clinic/one.pdf", one)
    ledger.file("receipts/sample-clinic/two.pdf", two)
    ledger.file("receipts/sample-clinic/one-resaved.pdf", one.replace("\n", "\r\n"))

    out = ledger.derive()

    assert out.documents == {"parsed": 3}
    assert counts(out) == (0, 1, 2, 0)
    assert spending(ledger) == Decimal("-63.94")


# --- lines that never count -----------------------------------------------------------


def rerun_stage3(ledger: Ledger) -> Any:
    """Stage 3 again over the stored parse output, the txn tables emptied first."""
    ledger.conn.execute(txn_source.delete())
    ledger.conn.execute(txn.delete())
    return build_lines(
        ledger.uow, CashLines((), ()), {}, zone=ledger.ctx.settings.timezone,
        overlap_days=3, receipt_days=3, invoice_days=30,
        qty_epsilon=Decimal("1E-9"), payees=Payees((), ()),
    )  # fmt: skip


def test_a_receipt_in_a_document_not_parsed_is_ignored(ledger: Ledger) -> None:
    ledger.file(RECEIPT, fixture("patient_receipt.txt"))
    assert ledger.derive().lines.from_receipts == 2
    ledger.conn.execute(document.update().values(status="superseded"))

    lines = rerun_stage3(ledger)

    assert (lines.from_receipts, lines.evidence_links, lines.txns) == (0, 0, 0)
    assert ledger.txns() == []


def test_an_incomplete_receipt_line_is_counted(ledger: Ledger) -> None:
    ledger.file(RECEIPT, fixture("patient_receipt.txt"))
    ledger.derive()
    ledger.conn.execute(
        document_line.update()
        .where(document_line.c.posting_date == D(2036, 5, 22))
        .values(amount=None)
    )

    lines = rerun_stage3(ledger)

    assert (lines.incomplete_lines, lines.from_receipts) == (1, 1)
    assert spending(ledger) == Decimal("-31.97")


def test_an_unmatched_receipt_in_another_currency_is_refused(ledger: Ledger) -> None:
    """The internal account takes CAD only: an unmatched USD receipt is counted as
    refused and booked nowhere, never as CAD spending."""
    ledger.file(RECEIPT, fixture("patient_receipt.txt"))
    ledger.derive()
    ledger.conn.execute(
        document_line.update()
        .where(document_line.c.posting_date == D(2036, 5, 22))
        .values(currency="USD")
    )

    lines = rerun_stage3(ledger)

    assert (lines.receipts_currency_refused, lines.from_receipts) == (1, 1)
    assert [(r["amount"], r["currency"]) for r in ledger.txns()] == [
        (Decimal("-31.97"), "CAD"),
    ]  # fmt: skip
