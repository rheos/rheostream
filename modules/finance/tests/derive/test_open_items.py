"""Synthetic stage-17 subjects read through dispatch under the actual reader DSN."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import Connection, create_engine, delete, insert, select, update
from sqlalchemy.pool import NullPool

from rheo_finance import catalogue, composition, operations
from rheo_finance.cli.main import main
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext, FinancePaths, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.derive import open_items
from rheo_finance.derive.digest import ledger_digest
from rheo_finance.derive.run import run
from rheo_finance.reports.open_items import ListOut
from rheo_finance.settings import parse_settings
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import (
    account,
    coverage,
    decision,
    decision_orphan,
    decision_target,
    document,
    document_line,
    obligation,
    open_item,
    pair_edge,
    payee,
    payment,
    payment_application,
    payment_evidence,
    txn,
    txn_source,
)
from tests.finance_support import DatabaseUnderTest, no_snapshot

from .support import D, Ledger, read_as_text
from .test_receipts_invoices import INVOICE, clinic_invoice
from .test_receipts_invoices import fixture as source_fixture
from .test_receipts_invoices import invoice as invoice_text

DAY = D(2052, 2, 4)
NOW = dt.datetime(2055, 8, 3, 12, tzinfo=dt.UTC)


def key(label: str, day: dt.date = DAY) -> uuid.UUID:
    return derived_id(day, ("synthetic_open_item", label))


def ref(kind: str, label: str) -> str:
    return format_ref("finance", kind, key(label))


def seed(conn: Connection) -> None:
    for slug, first in [
        ("ledger", None),
        ("late", D(2052, 4, 1)),
        ("absent", None),
        ("covered", D(2052, 1, 1)),
    ]:
        conn.execute(
            insert(account).values(
                id=key(slug),
                slug=slug,
                label=f"Sample {slug.title()}",
                institution="Sample Institution",
                kind="chequing",
                currencies=["CAD", "USD", "BTC"],
                cadence="monthly",
                sourced_by="documents",
                first_known_on=first,
            )
        )
    for label, status in [("parsed", "parsed"), ("rejected", "rejected")]:
        conn.execute(
            insert(document).values(
                id=key(label),
                path=f"synthetic/{label}.txt",
                sha256=hashlib.sha256(label.encode()).hexdigest(),
                kind="statement",
                issuer="synthetic",
                format="txt",
                status=status,
                imported_at=NOW,
            )
        )
    for slug, day in [("late", D(2052, 4, 1)), ("covered", DAY)]:
        conn.execute(
            insert(document_line).values(
                id=key(f"source-{slug}"),
                document_id=key("parsed"),
                line_no=50 if slug == "late" else 51,
                line_kind="transaction",
                account_id=key(slug),
                posting_date=day,
                amount=Decimal("60"),
                currency="CAD",
            )
        )
    conn.execute(
        insert(coverage).values(
            account_id=key("covered"), month=DAY.replace(day=1), state="have"
        )
    )

    line_number = 100

    def line(label: str, amount: str, **values: object) -> uuid.UUID:
        nonlocal line_number
        line_number += 1
        identity = key(label)
        defaults: dict[str, object] = dict(
            id=identity,
            account_id=key("ledger"),
            posting_date=DAY,
            recognition_date=DAY,
            amount=Decimal(amount),
            currency="CAD",
            cad_amount=Decimal(amount),
            origin="document_line",
            basis="statement",
            movement_kind="fiat",
            conversion_state="native",
        )
        defaults.update(values)
        conn.execute(insert(txn).values(**defaults))
        source = key(f"source-{label}")
        conn.execute(
            insert(document_line).values(
                id=source,
                document_id=key("rejected" if label == "needs" else "parsed"),
                line_no=line_number,
                line_kind="crypto_movement"
                if defaults["movement_kind"] == "crypto_transfer"
                else "transaction",
                account_id=key("ledger"),
                posting_date=defaults["posting_date"],
                amount=Decimal(amount),
                currency=defaults["currency"],
                line_key_id=identity,
            )
        )
        conn.execute(
            insert(txn_source).values(
                txn_id=identity,
                document_line_id=source,
                line_key_id=identity,
                role="primary",
            )
        )
        return identity

    line(
        "late-pull",
        "-10.29",
        transfer_like=True,
        transfer_state="unresolved",
        hint_account_id=key("late"),
    )
    line(
        "absent-deposit",
        "14.87",
        transfer_like=True,
        transfer_state="unresolved",
        hint_account_id=key("absent"),
    )
    ambiguous = line(
        "ambiguous", "-12.13", transfer_like=True, transfer_state="ambiguous"
    )
    candidate = line(
        "candidate",
        "-8.00",
        posting_date=D(2052, 2, 5),
        recognition_date=D(2052, 2, 5),
        category="fuel",
        tag="untagged",
    )
    conn.execute(
        insert(pair_edge).values(
            id=key("edge"),
            kind="own_transfer",
            a_txn_id=ambiguous,
            b_txn_id=candidate,
            applied=False,
        )
    )
    line(
        "foreign",
        "-16.71",
        currency="USD",
        cad_amount=None,
        conversion_state="unconverted",
    )
    line("needs", "-13.97")
    invoice_key = key("invoice")
    conn.execute(
        insert(document_line).values(
            id=invoice_key,
            document_id=key("parsed"),
            line_no=900,
            line_kind="invoice",
            posting_date=DAY,
            amount=Decimal("-28"),
            currency="CAD",
            line_key_id=invoice_key,
        )
    )

    def orphan(
        label: str,
        day: dt.date,
        candidates: list[uuid.UUID],
        *,
        snapshot: bool = True,
        effective_on: dt.date | None = None,
    ) -> None:
        conn.execute(
            insert(decision).values(
                id=key(label),
                kind="category" if effective_on is None else "invoice_unpaid",
                target_ref=ref("txn", "old-" + label),
                category="groceries" if effective_on is None else None,
                tag="personal" if effective_on is None else None,
                effective_on=effective_on,
                decided_at=NOW,
            )
        )
        if snapshot:
            conn.execute(
                insert(decision_target).values(
                    decision_id=key(label),
                    seq=1,
                    target_ref=ref("txn", "old-" + label),
                    account_slug="ledger",
                    posting_date=day,
                    amount=Decimal("-8"),
                    currency="CAD",
                )
            )
        candidate_ids: list[uuid.UUID | None] = list(candidates) or [None]
        for candidate_id in candidate_ids:
            conn.execute(
                insert(decision_orphan).values(
                    decision_id=key(label), candidate_txn_id=candidate_id
                )
            )

    orphan("orphan", DAY, [candidate])
    offered = [
        line(f"multiple-{index}", "-8", posting_date=day, recognition_date=day)
        for index, day in enumerate([D(2053, 2, 5), D(2053, 2, 6)])
    ]
    orphan("multiple", D(2053, 2, 4), offered)
    orphan("no-candidate", D(2054, 2, 4), [])
    orphan("snapshotless", D(2055, 8, 3), [], snapshot=False)
    orphan(
        "effective-date", D(2056, 6, 5), [], snapshot=False, effective_on=D(2056, 6, 5)
    )
    # All stage-15 credit reasons remain visible with the two open-item codes.
    conn.execute(
        insert(payee).values(
            id=key("payee"),
            slug="sample-payee",
            display_label="Sample Payee",
            kind="vendor",
        )
    )
    for index, reason in enumerate(
        ["no_target", "excess", "confirm", "payment_unapply"]
    ):
        day = DAY if index == 0 else D(2057, 2, 4)
        money = line(
            "payment-" + reason,
            "-99.00" if reason == "excess" else "-47.00",
            posting_date=day,
            recognition_date=day,
        )
        conn.execute(
            insert(payment).values(
                id=key("payment-" + reason),
                payee_id=key("payee"),
                amount=Decimal("99" if reason == "excess" else "47"),
                currency="CAD",
                paid_on=day,
                source="money",
                state="active",
            )
        )
        conn.execute(
            insert(payment_evidence).values(
                id=key("evidence-" + reason),
                payment_id=money,
                txn_id=money,
                role="money",
                stream_key="synthetic",
            )
        )
        conn.execute(
            insert(payment_application).values(
                id=key("application-" + reason),
                payment_id=money,
                amount=Decimal("47"),
                reason=reason,
                bound_on=day if reason == "no_target" else None,
                auto=reason != "payment_unapply",
            )
        )
    conn.execute(
        insert(obligation).values(
            id=key("overpaid"),
            payee_id=key("payee"),
            kind="bill",
            period_label="bill-2057-02",
            issued_on=D(2057, 2, 1),
            amount_due=Decimal("52"),
            threshold_due=Decimal("52"),
            paid=Decimal("52"),
            remaining=Decimal(0),
            threshold_remaining=Decimal(0),
            status="paid",
            primary_source="finance.manual:synthetic",
        )
    )
    conn.execute(
        insert(payment_application).values(
            id=key("applied"),
            payment_id=key("payment-excess"),
            obligation_id=key("overpaid"),
            amount=Decimal("52"),
            auto=True,
        )
    )
    for label, amount, currency, movement in [
        ("mixed-fiat", "41", "CAD", "fiat"),
        ("mixed-foreign", "-22", "USD", "fiat"),
        ("mixed-crypto", "-0.29", "BTC", "crypto_transfer"),
    ]:
        line(
            label,
            amount,
            posting_date=D(2058, 2, 4),
            recognition_date=D(2058, 2, 4),
            currency=currency,
            cad_amount=Decimal(amount) if currency == "CAD" else None,
            movement_kind=movement,
            conversion_state="native" if currency == "CAD" else "unconverted",
            transfer_like=True,
            transfer_state="unresolved",
        )
    conn.execute(
        insert(coverage).values(
            account_id=key("covered"), month=D(2059, 2, 1), state="partial"
        )
    )
    conn.execute(
        insert(account).values(
            id=key("card"),
            slug="card",
            label="Sample Card",
            institution="Sample Institution",
            kind="credit_card",
            currencies=["CAD"],
            cadence="monthly",
            sourced_by="documents",
        )
    )
    line(
        "partial",
        "-18",
        posting_date=D(2059, 2, 4),
        recognition_date=D(2059, 1, 31),
        txn_date=D(2059, 1, 31),
        account_id=key("card"),
        transfer_like=True,
        transfer_state="unresolved",
        hint_account_id=key("covered"),
    )
    open_items.build(UnitOfWork(conn))


@pytest.fixture(scope="module")
def reader_context(
    scratch_database: DatabaseUnderTest, tmp_path_factory: pytest.TempPathFactory
) -> FinanceContext:
    catalogue.load_all()
    path = tmp_path_factory.mktemp("open-items-reader")
    ctx = FinanceContext(
        settings=parse_settings(""),
        clock=FakeClock(NOW),
        paths=FinancePaths(data=path),
        role=Role.MEMBER,
        snapshot_provider=no_snapshot,
    )
    engine = create_engine(scratch_database.owner_url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            seed(conn)
    finally:
        engine.dispose()
    return operations.with_databases(
        ctx,
        owner_url=scratch_database.owner_url.set(
            database="finance_unavailable_owner_test"
        ),
        reader_url=scratch_database.reader_url,
    )


def listed(ctx: FinanceContext, year: int, kind: str | None = None) -> ListOut:
    out = operations.dispatch(
        "finance.openitem.list", {"year": year, "kind": kind}, ctx.role, ctx=ctx
    )
    assert isinstance(out, ListOut)
    return out


def test_every_excluded_subject_is_readable_with_derived_reasons_and_exact_sums(
    reader_context: FinanceContext,
) -> None:
    out = listed(reader_context, 2052)
    assert len(out.items) == 8
    rows = {row.line_ref: row for row in out.items}
    assert rows[ref("txn", "late-pull")].reason == (
        "Sample Late side not loaded: no Sample Late statement before 2052-04-01"
    )
    assert rows[ref("txn", "absent-deposit")].reason == (
        "Sample Absent side not loaded: no Sample Absent export filed"
    )
    assert rows[ref("txn", "ambiguous")].reason == "1 candidates"
    assert rows[ref("txn", "foreign")].reason == "no exchange rate"
    assert rows[ref("txn", "needs")].reason == "no usable source"
    assert rows[ref("invoice", "invoice")].reason == "invoice has no matching payment"
    assert rows[ref("decision", "orphan")].candidate_count == 1
    assert "Sample Payee issued by 2052-02-04" in next(
        row.reason for row in out.items if row.kind == "unapplied_credit"
    )
    summary = {row.kind: row for row in out.summary}
    assert {kind: row.count for kind, row in summary.items()} == {
        "unresolved_transfer": 2,
        "ambiguous_transfer": 1,
        "unconverted": 1,
        "needs_source": 1,
        "unmatched_invoice": 1,
        "orphaned_decision": 1,
        "unapplied_credit": 1,
    }
    for kind, inward, outward, currency in [
        ("unresolved_transfer", "14.87", "10.29", "CAD"),
        ("ambiguous_transfer", "0", "12.13", "CAD"),
        ("unconverted", "0", "16.71", "USD"),
        ("needs_source", "0", "13.97", "CAD"),
        ("unmatched_invoice", "0", "28", "CAD"),
        ("orphaned_decision", "0", "8", "CAD"),
        ("unapplied_credit", "0", "47", "CAD"),
    ]:
        (sums,) = summary[kind].sums
        assert (sums.currency, sums.in_amount, sums.out_amount) == (
            currency,
            Decimal(inward),
            Decimal(outward),
        )
    assert listed(reader_context, 2052, "unconverted").items == [
        rows[ref("txn", "foreign")]
    ]
    assert listed(reader_context, 2051).items == []


def test_multiple_orphan_candidates_never_multiply_financial_rows(
    reader_context: FinanceContext,
) -> None:
    out = listed(reader_context, 2053)
    (row,), (summary,) = out.items, out.summary
    assert (row.kind, row.candidate_count, row.amount, row.category, row.tag) == (
        "orphaned_decision",
        2,
        Decimal("-8"),
        "groceries",
        "personal",
    )
    assert row.occurred_on == D(2053, 2, 4)
    assert row.reason == "decision orphaned: 2 replacement candidates"
    assert summary.count == 1 and summary.sums[0].out_amount == Decimal("8")


def test_orphans_without_candidates_keep_the_stored_date_and_never_invent_money(
    reader_context: FinanceContext,
) -> None:
    (snapshot,) = listed(reader_context, 2054).items
    assert snapshot.amount == Decimal("-8")
    assert snapshot.reason == "decision orphaned: no replacement candidate"
    for year, day in [(2055, D(2055, 8, 3)), (2056, D(2056, 6, 5))]:
        (row,) = listed(reader_context, year).items
        assert row.occurred_on == day and row.candidate_count == 0
        assert row.amount is None and row.account_label is None and row.currency is None
        assert (row.category, row.tag) == (
            ("groceries", "personal") if year == 2055 else (None, None)
        )


def test_all_existing_credit_reasons_keep_their_own_payment_identity(
    reader_context: FinanceContext,
) -> None:
    out = listed(reader_context, 2057)
    assert len(out.items) == 3 and len({row.line_ref for row in out.items}) == 3
    assert sum("credit exceeds obligation" in row.reason for row in out.items) == 1
    assert "(bill-2057-02)" in next(
        row.reason for row in out.items if "credit exceeds obligation" in row.reason
    )
    assert sum("credit not applied" in row.reason for row in out.items) == 2
    assert out.summary[0].sums[0].out_amount == Decimal("141")


def test_cli_prints_the_same_counts_and_sums_and_an_empty_year(
    reader_context: FinanceContext,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def context(role: Role) -> FinanceContext:
        return dataclasses.replace(reader_context, role=role)

    monkeypatch.setattr(composition, "build_context", context)
    assert main(["report", "open-items", "--year", "2052"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("2052: 8 open items\n")
    assert "unresolved_transfer: 2; in 14.87 CAD, out 10.29 CAD" in output
    assert "unconverted: 1; in 0 USD, out 16.71 USD" in output
    assert "Sample Late statement before 2052-04-01" in output
    assert "; category groceries; tag personal" in output
    assert len(output.split("\n\n", 1)[0].splitlines()) <= 6
    assert main(["report", "open-items", "--year", "2027"]) == 0
    assert capsys.readouterr().out == "2027: no open items\n"
    assert main(["report", "open-items"]) == 2
    assert main(["report", "open-items", "--year", "1850"]) == 1


def test_coverage_and_usable_sources_are_derived_not_typed(
    owner_conn: Connection,
) -> None:
    seed(owner_conn)
    owner_conn.execute(delete(open_item))
    owner_conn.execute(
        update(txn)
        .where(txn.c.id == key("late-pull"))
        .values(hint_account_id=key("covered"))
    )
    owner_conn.execute(
        update(txn_source)
        .where(txn_source.c.txn_id == key("needs"))
        .values(document_line_id=None, manual_ref="finance.manual:synthetic")
    )
    open_items.build(UnitOfWork(owner_conn))
    rows = owner_conn.execute(select(open_item)).all()
    assert (
        next(
            row for row in rows if row.subject_ref == ref("txn", "late-pull")
        ).reason_code
        == "no_match"
    )
    assert not any(row.kind == "needs_source" for row in rows)
    # Replacing the hint with an account whose current month is partial, but whose
    # first known date is earlier, produces a month-specific reason with no date.
    owner_conn.execute(delete(open_item))
    owner_conn.execute(update(coverage).values(state="partial"))
    open_items.build(UnitOfWork(owner_conn))
    row = owner_conn.execute(
        select(open_item).where(open_item.c.subject_ref == ref("txn", "late-pull"))
    ).one()
    assert row.reason_code == "counterpart_not_loaded" and row.reason_date is None


def test_full_derive_clears_items_repeats_ids_and_closes_a_cash_receipt_invoice(
    ledger: Ledger,
) -> None:
    ledger.file(INVOICE, invoice_text())
    ledger.derive()
    (original,) = ledger.conn.execute(select(open_item)).all()
    assert original.kind == "unmatched_invoice"
    digest = ledger_digest(ledger.uow).digest
    ledger.derive()
    assert ledger_digest(ledger.uow).digest == digest
    assert ledger.conn.scalar(select(open_item.c.id)) == original.id
    # A receipt's paid-outside txn is usable evidence for its invoice. No new txn
    # or open item is created, even with a later invoice_paid decision.
    ledger.file(
        "invoices-received/sample-clinic/invoice.pdf",
        clinic_invoice("230.80", "Thu, May 22, 2036"),
    )
    ledger.file(
        "receipts/sample-clinic/receipt.pdf", source_fixture("patient_receipt.txt")
    )
    ledger.derive()
    assert ledger.conn.scalar(select(open_item.c.id)) == original.id
    from rheo_finance.decision import invoice_ref
    from rheo_finance.manual.stage2 import account_id

    invoice_key = ledger.conn.scalar(
        select(document_line.c.line_key_id).where(
            document_line.c.line_kind == "invoice",
            document_line.c.amount == Decimal("-230.80"),
        )
    )
    assert isinstance(invoice_key, uuid.UUID)
    before = ledger.txns()
    operations.dispatch(
        "finance.decision.record",
        {
            "kind": "invoice_paid",
            "target_ref": invoice_ref(invoice_key),
            "account_ref": format_ref("finance", "account", account_id("paid-outside")),
            "effective_on": "2036-05-28",
        },
        Role.OWNER,
        ctx=ledger.ctx,
    )
    ledger.derive()
    assert ledger.txns() == before
    assert ledger.conn.scalar(select(open_item.c.id)) == original.id
    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, reparse=True)
    assert ledger.conn.scalar(select(open_item.c.id)) == original.id


def test_mixed_currencies_and_crypto_are_never_summed_together(
    reader_context: FinanceContext,
) -> None:
    out = listed(reader_context, 2058, "unresolved_transfer")
    assert len(out.items) == 3 and out.summary[0].count == 3
    assert {
        row.currency: (row.in_amount, row.out_amount) for row in out.summary[0].sums
    } == {
        "CAD": (Decimal("41"), Decimal(0)),
        "USD": (Decimal(0), Decimal("22")),
        "BTC": (Decimal(0), Decimal("0.29")),
    }
    assert all(row.reason == "no matching counterpart" for row in out.items)


@pytest.mark.parametrize("kind", ["invoice_paid", "invoice_unpaid"])
def test_invoice_decision_and_reversal_close_and_restore_one_open_item(
    ledger: Ledger,
    kind: str,
) -> None:
    catalogue.load_all()
    ledger.file(INVOICE, invoice_text())
    ledger.file("invoices-received/sample-vendor/copy.pdf", invoice_text() + "\n")
    ledger.derive()
    (row,) = ledger.conn.execute(select(open_item)).all()
    payload: dict[str, object] = {"kind": kind, "target_ref": row.subject_ref}
    if kind == "invoice_paid":
        from rheo_finance.manual.stage2 import account_id

        payload.update(
            account_ref=format_ref("finance", "account", account_id("paid-outside")),
            effective_on="2050-01-11",
        )
    out = operations.dispatch(
        "finance.decision.record", payload, Role.OWNER, ctx=ledger.ctx
    )
    from rheo_finance.decision import RecordOut

    assert isinstance(out, RecordOut)
    ledger.derive()
    assert ledger.conn.scalar(select(open_item.c.id)) is None
    operations.dispatch(
        "finance.decision.reverse",
        {"decision_id": str(out.decision_id)},
        Role.OWNER,
        ctx=ledger.ctx,
    )
    ledger.derive()
    assert ledger.conn.scalar(select(open_item.c.id)) == row.id


def test_partial_counterpart_month_names_coverage_without_inventing_a_first_date(
    reader_context: FinanceContext,
) -> None:
    (row,) = listed(reader_context, 2059).items
    assert row.occurred_on == D(2059, 1, 31)
    assert (
        row.reason
        == "Sample Covered side not loaded: no Sample Covered data for 2059-02"
    )


@pytest.mark.parametrize(
    ("year", "decision_label", "candidate_labels"),
    [(2052, "orphan", ["candidate"]), (2053, "multiple", ["multiple-0", "multiple-1"])],
)
def test_reader_exposes_candidate_line_refs_without_multiplying_money(
    reader_context: FinanceContext,
    year: int,
    decision_label: str,
    candidate_labels: list[str],
) -> None:
    out = listed(reader_context, year, "orphaned_decision")
    (row,), (summary,) = out.items, out.summary
    assert row.line_ref == ref("decision", decision_label)
    assert row.model_dump().get("candidate_refs") == sorted(
        ref("txn", label) for label in candidate_labels
    )
    assert row.candidate_count == len(candidate_labels)
    assert row.amount == Decimal("-8")
    assert summary.count == 1 and summary.sums[0].out_amount == Decimal("8")


@pytest.mark.parametrize(
    ("year", "decision_label", "candidate_labels"),
    [(2052, "orphan", ["candidate"]), (2053, "multiple", ["multiple-0", "multiple-1"])],
)
def test_cli_exposes_candidate_line_refs_on_one_financial_row(
    reader_context: FinanceContext,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    year: int,
    decision_label: str,
    candidate_labels: list[str],
) -> None:
    monkeypatch.setattr(
        composition,
        "build_context",
        lambda role: dataclasses.replace(reader_context, role=role),
    )
    assert main(["report", "open-items", "--year", str(year)]) == 0
    output = capsys.readouterr().out
    (line,) = [
        line
        for line in output.splitlines()
        if line.startswith(ref("decision", decision_label) + " ")
    ]
    for label in candidate_labels:
        assert ref("txn", label) in line
    assert "orphaned_decision: 1; in 0.00 CAD, out 29.27 CAD" in output
    assert line.count("-29.27 CAD") == 1


def test_invoice_decision_reads_do_not_grow_with_the_unmatched_backlog(
    ledger: Ledger,
) -> None:
    from sqlalchemy import event as db_event

    def decision_reads() -> int:
        statements: list[str] = []

        def observe(
            _conn: Connection,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _many: bool,
        ) -> None:
            normalized = " ".join(statement.casefold().split())
            if (
                normalized.startswith("select ")
                and "from finance.decision " in normalized
            ):
                statements.append(normalized)

        db_event.listen(ledger.conn, "before_cursor_execute", observe)
        try:
            ledger.derive()
        finally:
            db_event.remove(ledger.conn, "before_cursor_execute", observe)
        return len(statements)

    ledger.file(INVOICE, invoice_text())
    one = decision_reads()
    assert one > 0
    for index in range(5):
        ledger.file(
            f"invoices-received/sample-vendor/backlog-{index}.pdf",
            invoice_text().replace("SP0042", f"SP999{index}"),
        )
    many = decision_reads()
    assert len(ledger.conn.execute(select(open_item)).all()) == 6
    assert many == one


def test_invoice_batch_lookup_keeps_newest_and_bulk_reversal_outcomes(
    ledger: Ledger,
) -> None:
    from rheo_finance.decision import RecordOut, invoice_ref
    from rheo_finance.manual.stage2 import account_id

    from .test_invoice_decisions import call

    catalogue.load_all()
    ledger.file(INVOICE, invoice_text())
    ledger.derive()
    invoice_key = ledger.conn.scalar(select(document_line.c.line_key_id))
    assert isinstance(invoice_key, uuid.UUID)
    target = invoice_ref(invoice_key)
    older = call(
        ledger,
        "finance.decision.record",
        {
            "kind": "invoice_unpaid",
            "target_ref": target,
        },
    )
    assert isinstance(older, RecordOut)
    call(
        ledger,
        "finance.decision.record",
        {
            "kind": "invoice_paid",
            "target_ref": target,
            "account_ref": format_ref("finance", "account", account_id("paid-outside")),
            "effective_on": "2050-01-11",
            "bulk_id": str(key("invoice-bulk")),
        },
    )
    ledger.derive()
    assert ledger.conn.scalar(select(open_item.c.id)) is None
    assert len(ledger.txns()) == 1
    assert ledger.conn.scalar(select(obligation.c.id)) is None
    call(ledger, "finance.decision.reverse", {"bulk_id": str(key("invoice-bulk"))})
    ledger.derive()
    assert ledger.txns() == []
    assert len(ledger.conn.execute(select(obligation)).all()) == 1
    assert ledger.conn.scalar(select(open_item.c.id)) is None
    call(ledger, "finance.decision.reverse", {"decision_id": str(older.decision_id)})
    ledger.derive()
    assert ledger.conn.scalar(select(obligation.c.id)) is None
    assert len(ledger.conn.execute(select(open_item)).all()) == 1
