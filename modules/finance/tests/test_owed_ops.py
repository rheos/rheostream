"""Reader seams over committed synthetic scratch data, with no usable owner DSN."""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import Connection, create_engine, insert, select, text, update
from sqlalchemy.pool import NullPool

from rheo_finance import catalogue, operations
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext, FinancePaths
from rheo_finance.contracts import Role
from rheo_finance.documents import issuers
from rheo_finance.operations import dispatch
from tests.derive.support import Ledger
from tests.finance_support import DatabaseUnderTest, is_scratch_database

if TYPE_CHECKING:
    from rheo_finance.reports.owed import ListOut, TraceOut
from rheo_finance.settings import parse_settings
from rheo_finance.storage.ids import uuid7
from rheo_finance.storage.schema import (
    decision,
    document,
    document_line,
    obligation,
    open_item,
    payee,
    payment,
    payment_application,
    payment_evidence,
)

D = dt.date
DUE = D(2057, 5, 15)
NOW = dt.datetime(2057, 5, 16, 12, tzinfo=dt.UTC)
M = Decimal
Built = tuple[DatabaseUnderTest, Path]


def test_owed_list_and_trace_are_reader_operations(
    make_context: Callable[[Role], FinanceContext], test_database: DatabaseUnderTest
) -> None:
    catalogue.load_all()
    ctx = operations.with_databases(
        make_context(Role.MEMBER),
        owner_url=test_database.owner_url.set(port=1),
        reader_url=test_database.reader_url,
    )
    assert (
        dispatch("finance.owed.list", {}, Role.MEMBER, ctx=ctx).model_dump()["rows"]
        == []
    )
    assert (
        dispatch("finance.owed.trace", {}, Role.MEMBER, ctx=ctx).model_dump()[
            "obligations"
        ]
        == []
    )


@pytest.fixture(scope="module")
def built(
    scratch_database: DatabaseUnderTest, tmp_path_factory: pytest.TempPathFactory
) -> Built:
    catalogue.load_all()
    return scratch_database, tmp_path_factory.mktemp("owed")


@pytest.fixture
def stored(built: Built) -> Iterator[Connection]:
    database, _ = built
    engine = create_engine(database.owner_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            try:
                yield conn
            finally:
                conn.rollback()
                # Only this synthetic scratch is cleared. Append-only decisions stay
                # until the module's scratch database is dropped.
                assert is_scratch_database(database.owner_url.database)
                conn.execute(
                    text(
                        "TRUNCATE finance.payee, finance.document, "
                        "finance.account CASCADE"
                    )
                )
                conn.commit()
    finally:
        engine.dispose()


def reader(built: Built, when: dt.datetime = NOW, settings: str = "") -> FinanceContext:
    database, root = built
    ctx = FinanceContext(
        settings=parse_settings(settings),
        clock=FakeClock(when),
        paths=FinancePaths(data=root),
        role=Role.MEMBER,
        snapshot_provider=lambda _uow, _ref: None,
    )
    return operations.with_databases(
        ctx, owner_url=database.owner_url.set(port=1), reader_url=database.reader_url
    )


def owed(built: Built, when: dt.datetime = NOW, settings: str = "") -> ListOut:
    from rheo_finance.reports.owed import ListOut

    out = dispatch(
        "finance.owed.list", {}, Role.MEMBER, ctx=reader(built, when, settings)
    )
    assert isinstance(out, ListOut)
    return out


def trace(built: Built, ref: str | None = None) -> TraceOut:
    from rheo_finance.reports.owed import TraceOut

    out = dispatch(
        "finance.owed.trace", {"obligation_ref": ref}, Role.MEMBER, ctx=reader(built)
    )
    assert isinstance(out, TraceOut)
    return out


def add(
    conn: Connection,
    slug: str = "sample-card",
    kind: str = "card_statement",
    due: dt.date | None = DUE,
    *,
    amount: str = "697.74",
    minimum: str | None = "21.90",
    paid: str = "0.00",
    status: str = "open",
    period: str = "card-2057-04-20",
    before_floor: bool = False,
    **extra: object,
) -> uuid.UUID:
    pid = conn.scalar(select(payee.c.id).where(payee.c.slug == slug))
    if pid is None:
        pid = uuid7(NOW)
        conn.execute(
            insert(payee).values(
                id=pid,
                slug=slug,
                display_label=slug.title(),
                kind="vendor",
                patterns=["SAMPLE"],
                paid_from=[],
            )
        )
    oid = uuid7(NOW)
    threshold = M(minimum) if kind == "card_statement" and minimum else M(amount)
    conn.execute(
        insert(obligation).values(
            id=oid,
            payee_id=pid,
            kind=kind,
            period_label=period,
            statement_date=D(2057, 4, 20),
            issued_on=D(2057, 4, 20),
            amount_due=M(amount),
            minimum_due=None if minimum is None else M(minimum),
            threshold_due=threshold,
            paid=M(paid),
            remaining=max(M(0), M(amount) - M(paid)),
            threshold_remaining=max(M(0), threshold - M(paid)),
            due_date=due,
            status=status,
            primary_source=f"sample-bills/{slug}.pdf",
            before_floor=before_floor,
            **extra,
        )
    )
    return oid


@pytest.mark.parametrize(
    "paid,overdue,remaining",
    [
        ("0.00", "21.90", "697.74"),
        ("21.90", "0.00", "675.84"),
        ("10.81", "11.09", "686.93"),
    ],
)
def test_card_minimum_and_carried_at_read_time_under_reader(
    built: Built,
    stored: Connection,
    paid: str,
    overdue: str,
    remaining: str,
) -> None:
    add(stored, paid=paid)
    stored.commit()
    out = owed(built)
    (row,) = out.rows
    assert row.overdue == M(overdue)
    assert row.carried == M("675.84")
    assert row.remaining == M(remaining)
    assert row.primary_source
    assert out.overdue == M(overdue) and out.carried == M("675.84")
    # Same stored bytes, different local day across the UTC/local midnight boundary.
    earlier = owed(built, when=dt.datetime(2057, 5, 16, 3, tzinfo=dt.UTC))
    assert earlier.overdue == 0
    assert earlier.rows[0].due_in_window == M("21.90") - M(paid)
    assert len(out.headline) == 5


def test_full_totals_order_cap_undated_and_missing_minimum(
    built: Built, stored: Connection
) -> None:
    add(stored, "card-no-minimum", minimum=None)
    for slug, kind in [
        ("sample-bill", "bill"),
        ("sample-plan", "arrangement_instalment"),
        ("sample-debt", "manual_debt"),
    ]:
        add(stored, slug, kind, amount="47.67", minimum=None)
    add(stored, "future", "bill", D(2057, 5, 20), amount="30.99", minimum=None)
    add(stored, "undated", "manual_debt", None, amount="35.44", minimum=None)
    stored.commit()
    capped = owed(built, settings="[finance.owed]\nmax_rows=2\nwindow_days=14\n")
    assert capped.overdue == M("833.52")
    assert capped.due_in_window == M("30.99")
    assert len(capped.rows) == 2 and capped.more == 4
    every = owed(built)
    assert every.rows[-1].payee_slug == "undated"
    assert every.rows[-2].payee_slug == "future"
    assert all(row.overdue > 0 for row in every.rows[:4])
    narrow = owed(built, settings="[finance.owed]\nwindow_days=2\n")
    assert narrow.due_in_window == 0


def test_presumed_paid_effective_money_floor_marker_and_basis(
    built: Built, stored: Connection
) -> None:
    add(stored, "old", "bill", amount="62.68", minimum=None, before_floor=True)
    oid = add(stored, "presumed", status="presumed_paid", before_floor=True)
    did = uuid7(NOW)
    stored.execute(
        insert(decision).values(
            id=did,
            kind="presumed_paid",
            obligation_ref=f"finance.obligation:{oid}",
            decided_at=NOW,
        )
    )
    stored.execute(
        update(obligation)
        .where(obligation.c.id == oid)
        .values(presumed_paid_decision_id=did)
    )
    stored.commit()
    out = owed(built)
    by = {r.payee_slug: r for r in out.rows}
    assert out.overdue == M("62.68") and out.carried == 0
    assert by["old"].before_floor and by["old"].remaining == M("62.68")
    assert not by["old"].reminder_eligible
    assert by["presumed"].paid == M("697.74") and by["presumed"].remaining == 0
    assert by["presumed"].recorded_remaining == M("697.74")
    assert stored.scalar(
        select(obligation.c.remaining).where(obligation.c.status == "presumed_paid")
    ) == M("697.74")
    (presumed,) = [
        r for r in trace(built).obligations if r.row.status == "presumed_paid"
    ]
    assert presumed.basis_class == "owner"
    assert presumed.row.presumed_paid_basis == f"finance.decision:{did}"
    assert (
        presumed.presumed_basis_kind == "presumed_paid" and presumed.presumed_at == NOW
    )


def doc(
    conn: Connection, path: str, kind: str, status: str, issuer: str = "sample-bills"
) -> uuid.UUID:
    ident = uuid7(NOW)
    conn.execute(
        insert(document).values(
            id=ident,
            path=path,
            sha256=ident.hex * 2,
            kind=kind,
            issuer=issuer,
            format="pdf",
            status=status,
            imported_at=NOW,
            extraction="none",
            official=True,
        )
    )
    return ident


def test_gaps_one_queue_cleared_by_manual_source_and_status_count(
    built: Built,
    stored: Connection,
    monkeypatch: pytest.MonkeyPatch,
    sink_settings: Callable[..., None],
) -> None:
    monkeypatch.setattr(
        issuers,
        "ISSUER_DIRS",
        MappingProxyType(
            {**issuers.ISSUER_DIRS, "sample-bills": issuers.IssuerDir("bill")}
        ),
    )
    doc(stored, "sample-bills/photo.jpg", "image", "needs_manual_entry")
    doc(stored, "sample-bills/unparsed.pdf", "bill", "unparsed")
    doc(stored, "sample-bills/cleared.jpg", "image", "needs_manual_entry")
    doc(
        stored,
        "sample-statements/photo.jpg",
        "image",
        "needs_manual_entry",
        "sample-statements",
    )
    doc(stored, "sample-bills/parsed.pdf", "bill", "parsed")
    oid = add(stored, "entered", "bill")
    stored.execute(
        update(obligation)
        .where(obligation.c.id == oid)
        .values(
            primary_source="sample-bills/cleared.jpg", manual_source="obligations.csv:2"
        )
    )
    stored.commit()
    out = owed(built)
    assert out.gap_count == 2
    assert {r.path for r in out.gaps} == {
        "sample-bills/photo.jpg",
        "sample-bills/unparsed.pdf",
    }
    sink_settings()
    status = dispatch("finance.status.get", {}, Role.MEMBER, ctx=reader(built))
    assert "2 bill gaps" in next(
        line["text"] for line in status.model_dump()["lines"] if line["line_no"] == 3
    )


def test_trace_chain_retains_superseded_applications_not_duplicate_notice_money(
    built: Built,
    stored: Connection,
) -> None:
    old = add(stored, status="superseded", paid="21.90", manual_mismatch=M("2.22"))
    current = add(
        stored,
        period="card-2057-05-20",
        amount="675.84",
        minimum="17.52",
        supersedes_id=old,
        payments_mismatch=M("4.38"),
    )
    pid = stored.scalar(select(payee.c.id))
    pay = uuid7(NOW)
    app = uuid7(NOW)
    stored.execute(
        insert(payment).values(
            id=pay,
            payee_id=pid,
            amount=M("21.90"),
            currency="CAD",
            paid_on=D(2057, 5, 13),
            source="money",
            state="active",
        )
    )
    stored.execute(
        insert(payment_application).values(
            id=app, payment_id=pay, obligation_id=old, amount=M("21.90"), auto=True
        )
    )
    for n in range(2):
        did = doc(
            stored,
            f"sample-notices/notice-{n}.eml",
            "email",
            "parsed",
            "sample-notices",
        )
        line = uuid7(NOW)
        stored.execute(
            insert(document_line).values(
                id=line,
                document_id=did,
                line_no=1,
                line_kind="payment_notice",
                posting_date=D(2057, 5, 13),
                amount=M("21.90"),
                currency="CAD",
            )
        )
        stored.execute(
            insert(payment_evidence).values(
                id=uuid7(NOW),
                payment_id=pay,
                stream_key=f"synthetic-{n}",
                role="notice",
                document_line_id=line,
            )
        )
    stored.commit()
    out = owed(built)
    assert len(out.rows) == 1 and out.rows[0].amount_due == M("675.84")
    assert out.carried == M("658.32")
    result = trace(built, f"finance.obligation:{current}")
    assert len(result.obligations) == 2
    (older,) = [
        r
        for r in result.obligations
        if r.row.obligation_ref == f"finance.obligation:{old}"
    ]
    (newer,) = [
        r
        for r in result.obligations
        if r.row.obligation_ref == f"finance.obligation:{current}"
    ]
    (application,) = older.applications
    assert application.applied_amount == M("21.90") and len(application.evidence) == 2
    assert all(e.role == "notice" and e.document_path for e in application.evidence)
    assert older.row.recorded_paid == M("21.90") and older.row.manual_mismatch == M(
        "2.22"
    )
    assert newer.row.payments_mismatch == M("4.38") and newer.applications == []
    assert {
        r.row.obligation_ref
        for r in trace(built, f"finance.obligation:{old}").obligations
    } == {f"finance.obligation:{old}", f"finance.obligation:{current}"}


def test_credit_rows_and_counts_share_kind_filter_keep_native_and_missing_cad(
    built: Built,
    stored: Connection,
) -> None:
    add(stored)
    pid = stored.scalar(select(payee.c.id))
    for currency, amount, cad in [("CAD", "5", "4.38"), ("USD", "7", None)]:
        pay = uuid7(NOW)
        app = uuid7(NOW)
        stored.execute(
            insert(payment).values(
                id=pay,
                payee_id=pid,
                amount=M(amount),
                currency=currency,
                paid_on=D(2057, 5, 13),
                source="notice_only",
                state="active",
            )
        )
        stored.execute(
            insert(payment_application).values(
                id=app, payment_id=pay, amount=M(amount), reason="excess", auto=True
            )
        )
        stored.execute(
            insert(open_item).values(
                id=uuid7(NOW),
                tax_year=2057,
                kind="unapplied_credit",
                subject_ref=f"finance.payment_application:{app}",
                payment_id=pay,
                occurred_on=D(2057, 5, 13),
                amount=-M(amount),
                direction="out",
                currency=currency,
                cad_amount=None if cad is None else -M(cad),
                state="unapplied",
                reason_code="excess",
                reason_payee_id=pid,
            )
        )
    stored.execute(
        insert(open_item).values(
            id=uuid7(NOW),
            tax_year=2057,
            kind="needs_source",
            subject_ref=f"finance.txn:{uuid7(NOW)}",
            occurred_on=D(2057, 5, 13),
            amount=M("953.62"),
            direction="in",
            currency="CAD",
            state="open",
            reason_code="no_source",
        )
    )
    stored.commit()
    out = owed(built)
    assert out.overdue == M("21.90") and out.carried == M("675.84")
    assert out.credits.count == 2 and len(out.credits.items) == 2
    assert out.credits.line == "credits not applied: 2, 4.38 CAD; 8.09 USD"
    assert [(r.currency, r.amount, r.cad_amount) for r in out.credits.sums] == [
        ("CAD", M("4.38"), M("4.38")),
        ("USD", M("8.09"), None),
    ]
    detail = trace(built).credits
    assert detail == out.credits
    assert all(r.payment_ref and r.payee_label for r in detail.items)
    # Zero-target excess has no period metadata. Keep its truthful payee-only reason.
    assert all(
        r.reason == "credit exceeds obligation for Sample-Card" for r in detail.items
    )


def test_patternless_invoice_flag_and_registered_vendor_rederive_clears_debt(
    built: Built,
    stored: Connection,
    tmp_path: Path,
) -> None:
    from rheo_finance.manual.payees import PAYEES_FILE
    from tests.derive.test_invoice_decisions import unpaid
    from tests.derive.test_receipts_invoices import (
        CONTRACTOR,
        INVOICE,
        invoice,
        payments,
    )

    database, _ = built
    ctx = dataclasses.replace(reader(built), role=Role.OWNER)
    ctx = operations.with_databases(
        ctx, owner_url=database.owner_url, reader_url=database.reader_url
    )
    ctx = operations.with_owner_connection(ctx, stored)
    ledger = Ledger(stored, ctx, tmp_path)
    payments(ledger, ("2057/03/05", CONTRACTOR, "620.85"))
    ledger.file(INVOICE, invoice())
    ledger.derive()
    unpaid(ledger)
    ledger.derive()
    old = stored.scalar(select(obligation.c.id))
    stored.commit()
    (row,) = owed(built).rows
    assert row.payee_slug == "invoices-received" and row.payments_unmatchable
    assert row.amount_due == M("620.85") and row.remaining == M("620.85")
    assert row.invoice_folder and row.invoice_folder.startswith("invoices-received/")
    ledger.manual(PAYEES_FILE, "sample-vendor,Sample Vendor,vendor,SAMPLE CONTRACTOR,,")
    ledger.derive()
    stored.commit()
    assert owed(built).rows == []
    assert stored.scalar(select(obligation.c.id).where(obligation.c.id == old)) is None
    (paid,) = trace(built).obligations
    assert paid.row.payee_slug == "sample-vendor" and not paid.row.payments_unmatchable
    assert paid.row.status == "paid" and paid.row.recorded_remaining == 0
