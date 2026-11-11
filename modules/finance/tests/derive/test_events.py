"""Events and state notices through derive.run, using only synthetic layouts."""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal
from importlib import import_module

import pytest
from sqlalchemy import func, select, text, update

from rheo_finance.clock import FakeClock
from rheo_finance.derive.fingerprint import fingerprint
from rheo_finance.derive.run import run
from rheo_finance.derive.stage16 import (
    OBLIGATION_CREATED,
    OBLIGATION_SUPERSEDED,
    PAYMENT_REVERSED,
    STATEMENT_AVAILABLE,
    STATEMENT_OVERDUE,
    StatementReady,
    statement_ready_text,
)
from rheo_finance.manual.stage2 import account_id
from rheo_finance.parsers.docextract_adapter import alder_statement
from rheo_finance.storage.schema import (
    document_line,
    event,
    notice,
    obligation,
    overdrawn_spell,
    parsed_overdrawn,
)

from .support import CSV_IDENTIFIER, FIXTURES, D, Ledger, read_as_text
from .test_payment_application import bills, with_settings
from .test_presumed_paid import presume, rows
from .test_reversals import export

STATEMENT = (FIXTURES / "alder_statement_split.txt").read_text(encoding="utf-8")


def clock(ledger: Ledger, day: str) -> FakeClock:
    fake = FakeClock(dt.datetime.fromisoformat(day).replace(tzinfo=dt.UTC))
    ledger.ctx = dataclasses.replace(ledger.ctx, clock=fake)
    return fake


def statement(ledger: Ledger) -> None:
    ledger.chequing()
    ledger.file("alder/2055-06-12.pdf", STATEMENT)
    with_settings(ledger, '[finance.locale]\ntimezone = "Etc/GMT-9"\n')


def ids(ledger: Ledger, kind: str) -> set[uuid.UUID]:
    return set(ledger.conn.scalars(select(event.c.id).where(event.c.type == kind)))


def test_grace_boundary_uses_local_day_without_data_change(ledger: Ledger) -> None:
    statement(ledger)
    fake = clock(ledger, "2054-02-21T14:59:00")  # local 21 February, 23:59
    ledger.derive()
    assert not ids(ledger, STATEMENT_OVERDUE)
    before = fingerprint(ledger.uow, ledger.ctx)
    fake.advance(dt.timedelta(minutes=1))
    after = fingerprint(ledger.uow, ledger.ctx)
    assert {key for key in before.parts if before.parts[key] != after.parts[key]} == {
        "local_date"
    }
    ledger.derive()
    first = ids(ledger, STATEMENT_OVERDUE)
    assert len(first) == 1
    first_notices = set(ledger.conn.scalars(select(notice.c.key)))
    ledger.derive()
    assert ids(ledger, STATEMENT_OVERDUE) == first
    assert set(ledger.conn.scalars(select(notice.c.key))) == first_notices
    assert ledger.conn.scalar(
        select(event.c.occurred_on).where(event.c.type == STATEMENT_OVERDUE)
    ) == D(2054, 2, 22)


def test_rebuild_restores_event_ids_and_retains_notice_delivery_state(
    ledger: Ledger,
) -> None:
    statement(ledger)
    fake = clock(ledger, "2054-02-22T12:00:00")
    ledger.derive()
    expected = set(ledger.conn.scalars(select(event.c.id)))
    first_seen = fake.now_utc()
    # A delivery outcome must survive just as first_seen does, even on a reparse.
    ledger.conn.execute(update(notice).values(state="sent", sent_at=first_seen))
    fake.advance(dt.timedelta(days=2))
    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, reparse=True)
    assert set(ledger.conn.scalars(select(event.c.id))) == expected
    found = ledger.conn.execute(select(notice)).all()
    assert len(found) == 2
    assert all(
        row.first_seen_at == first_seen
        and row.sent_at == first_seen
        and row.state == "sent"
        for row in found
    )


def test_availability_fallback_uses_global_default_lag(ledger: Ledger) -> None:
    statement(ledger)
    fake = clock(ledger, "2054-02-16T14:59:00")
    ledger.derive()
    assert not ids(ledger, STATEMENT_AVAILABLE)
    fake.advance(dt.timedelta(minutes=1))
    ledger.derive()
    assert len(ids(ledger, STATEMENT_AVAILABLE)) == 1
    assert not ids(ledger, STATEMENT_OVERDUE)
    assert ledger.conn.scalar(
        select(event.c.occurred_on).where(event.c.type == STATEMENT_AVAILABLE)
    ) == D(2054, 2, 17)
    assert (
        statement_ready_text("Sample daily")
        == "Sample daily statement is ready to download"
    )


def test_ready_notice_is_immediate_and_deduplicates_fallback(ledger: Ledger) -> None:
    statement(ledger)
    fake = clock(ledger, "2054-02-15T12:00:00")
    fact = StatementReady(account_id("chequing"), D(2054, 2, 15), D(2054, 2, 15))

    def ready(_uow: object) -> tuple[StatementReady, ...]:
        return (fact, fact)

    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, statement_ready=ready)
    original = ids(ledger, STATEMENT_AVAILABLE)
    assert len(original) == 1
    first_seen = ledger.conn.scalar(select(notice.c.first_seen_at))
    fake.advance(dt.timedelta(days=3))
    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, statement_ready=ready)
    assert ids(ledger, STATEMENT_AVAILABLE) == original
    # A rebuild without the mail input still has the same fallback identity.
    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, reparse=True)
    assert ids(ledger, STATEMENT_AVAILABLE) == original
    assert ledger.conn.scalar(select(func.count()).select_from(notice)) == 1
    assert ledger.conn.scalar(select(notice.c.first_seen_at)) == first_seen


def test_ready_without_history_but_not_for_unregistered_or_filed_period(
    ledger: Ledger,
) -> None:
    statement(ledger)
    clock(ledger, "2055-06-15T12:00:00")

    def ready(_uow: object) -> tuple[StatementReady, ...]:
        return (
            StatementReady(account_id("chequing"), D(2055, 6, 12), D(2055, 6, 13)),
            StatementReady(account_id("absent"), D(2055, 6, 12), D(2055, 6, 13)),
            StatementReady(account_id("chequing"), D(2055, 6, 20), D(2055, 6, 21)),
        )

    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, statement_ready=ready)
    assert not ids(ledger, STATEMENT_AVAILABLE)
    (ledger.archive / "alder/2055-06-12.pdf").unlink()

    def no_history(_uow: object) -> tuple[StatementReady, ...]:
        return (StatementReady(account_id("chequing"), D(2055, 6, 12), D(2055, 6, 13)),)

    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, statement_ready=no_history)
    assert len(ids(ledger, STATEMENT_AVAILABLE)) == 1


def test_new_period_document_stops_missing_statement_events(ledger: Ledger) -> None:
    statement(ledger)
    clock(ledger, "2055-07-20T12:00:00")
    ledger.derive()
    assert len(ids(ledger, STATEMENT_OVERDUE)) == 1
    new = (
        STATEMENT.replace("12 JUN 2055", "13 JUL 2055")
        .replace(" 9 MAY 2055", "13 JUN 2055")
        .replace("15 MAY", "15 JUN")
        .replace("20 MAY", "20 JUN")
    )
    ledger.file("alder/2055-07-13.pdf", new)
    ledger.derive()
    assert not ids(ledger, STATEMENT_OVERDUE)
    assert not ids(ledger, STATEMENT_AVAILABLE)


@pytest.mark.parametrize(
    "cadence,expected",
    [
        ("monthly", D(2054, 2, 17)),
        ("quarterly", D(2054, 4, 17)),
        ("yearly", D(2055, 1, 17)),
    ],
)
def test_calendar_cadences_and_configured_lag(
    ledger: Ledger,
    cadence: str,
    expected: dt.date,
) -> None:
    statement(ledger)
    path = ledger.data / "manual" / "accounts.csv"
    path.write_text(path.read_text().replace(",monthly,", f",{cadence},"))
    with_settings(ledger, "[finance.statements]\navailability_lag_days = 1\n")
    clock(ledger, f"{expected - dt.timedelta(days=1)}T12:00:00")
    ledger.derive()
    assert len(ids(ledger, STATEMENT_AVAILABLE)) == 1


def test_obligations_created_and_overdue_notices(ledger: Ledger) -> None:
    ledger.chequing()
    ledger.manual("payees.csv", "net,Sample network,vendor,SAMPLE NET,,")
    bills(
        ledger,
        ("net", "21.08", D(2054, 1, 1), D(2054, 1, 8)),
        ("net", "117.96", D(2054, 2, 1), D(2054, 2, 8)),
    )
    clock(ledger, "2054-02-23T12:00:00")
    ledger.derive()
    assert len(ids(ledger, OBLIGATION_CREATED)) == 2
    assert not ids(ledger, OBLIGATION_SUPERSEDED)
    overdue = ledger.conn.execute(
        select(notice).where(notice.c.kind == "overdue")
    ).all()
    active = ledger.conn.execute(
        select(obligation).where(obligation.c.status == "open")
    ).all()
    assert {row.key for row in overdue} == {f"overdue:{row.id}" for row in active}
    assert {row.event_date for row in overdue} == {D(2054, 1, 8), D(2054, 2, 8)}
    assert all(row.state == "pending" for row in overdue)
    assert not set(ledger.conn.scalars(select(notice.c.kind))) - {"overdue"}


@pytest.mark.parametrize("excluded", ["presumed_paid", "before_floor"])
def test_presumed_paid_and_before_floor_never_make_overdue_notices(
    ledger: Ledger,
    excluded: str,
) -> None:
    ledger.chequing()
    ledger.manual("payees.csv", "net,Sample network,vendor,SAMPLE NET,,")
    bills(ledger, ("net", "21.08", D(2054, 1, 1), D(2054, 1, 8)))
    # Keep the first derive before the due date, so no prior notice exists.
    fake = clock(ledger, "2054-01-05T12:00:00")
    ledger.derive()
    if excluded == "presumed_paid":
        presume(ledger, "net bill-2054-01")
    else:
        with_settings(ledger, "[finance.reminders]\nmin_due_date = 2054-01-09\n")
    fake.advance(dt.timedelta(days=20))
    ledger.derive()
    row = rows(ledger)["net bill-2054-01"]
    assert row.remaining == Decimal("21.08")
    assert (
        row.status == "presumed_paid"
        if excluded == "presumed_paid"
        else row.before_floor
    )
    assert (
        ledger.conn.scalar(
            select(func.count()).select_from(notice).where(notice.c.kind == "overdue")
        )
        == 0
    )


def test_payment_reversal_has_actual_reversal_date_and_safe_refs(
    ledger: Ledger,
) -> None:
    ledger.chequing()
    ledger.manual("payees.csv", "net,Sample network,vendor,SAMPLE NET,,")
    ledger.file(
        "alder/payment.csv",
        export(
            (
                CSV_IDENTIFIER,
                D(2054, 1, 7),
                "CAROB E-SEND receiver = SAMPLE NET",
                Decimal("-21.08"),
            ),
            (
                CSV_IDENTIFIER,
                D(2054, 1, 9),
                "E-SEND OUT UNDONE BY SENDER",
                Decimal("21.08"),
            ),
        ),
    )
    ledger.derive()
    found = ledger.conn.execute(
        select(event).where(event.c.type == PAYMENT_REVERSED)
    ).one()
    assert found.occurred_on == D(2054, 1, 9)
    assert set(found.data) == {"payee_ref", "reversal_ref"}


def test_actual_parser_spells_survive_cached_derive_and_rebuild(ledger: Ledger) -> None:
    statement(ledger)
    # The terminal printed balance becomes negative on the second row.
    actual = (
        STATEMENT.replace("117.96", "645.55")
        .replace("124.56", "652.15")
        .replace("465.24", "-62.35")
    )
    ledger.file("alder/2055-06-12.pdf", actual)
    ledger.derive()
    expected = [(account_id("chequing"), "CAD", D(2055, 5, 20), None)]
    assert [
        tuple(row)
        for row in ledger.conn.execute(
            select(
                overdrawn_spell.c.account_id,
                overdrawn_spell.c.currency,
                overdrawn_spell.c.start_date,
                overdrawn_spell.c.end_date,
            )
        )
    ] == expected
    assert ledger.conn.scalar(select(func.count()).select_from(parsed_overdrawn)) == 1
    ledger.derive()
    assert [
        tuple(row)
        for row in ledger.conn.execute(
            select(
                overdrawn_spell.c.account_id,
                overdrawn_spell.c.currency,
                overdrawn_spell.c.start_date,
                overdrawn_spell.c.end_date,
            )
        )
    ] == expected
    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, reparse=True)
    assert [
        tuple(row)
        for row in ledger.conn.execute(
            select(
                overdrawn_spell.c.account_id,
                overdrawn_spell.c.currency,
                overdrawn_spell.c.start_date,
                overdrawn_spell.c.end_date,
            )
        )
    ] == expected
    (ledger.archive / "alder/2055-06-12.pdf").unlink()
    ledger.derive()
    assert ledger.conn.scalar(select(func.count()).select_from(overdrawn_spell)) == 0


def test_split_intermediate_negative_balance_is_not_an_overdrawn_spell(
    ledger: Ledger,
) -> None:
    statement(ledger)
    split = (
        STATEMENT.replace("393.20", "  1.08")
        .replace("583.20", " 388.84")
        .replace("465.24", " 270.88")
    )
    ledger.file("alder/2055-06-12.pdf", split)
    ledger.derive()
    assert (
        ledger.conn.scalar(
            select(func.count())
            .select_from(document_line)
            .where(document_line.c.running_balance < 0)
        )
        == 1
    )
    assert ledger.conn.scalar(select(func.count()).select_from(overdrawn_spell)) == 0


def test_notice_output_does_not_dirty_the_input_fingerprint(ledger: Ledger) -> None:
    statement(ledger)
    clock(ledger, "2054-02-22T12:00:00")
    before = fingerprint(ledger.uow, ledger.ctx)
    ledger.derive()
    after = fingerprint(ledger.uow, ledger.ctx)
    assert before == after


def test_superseded_obligation_event_names_the_replacement(ledger: Ledger) -> None:
    from .test_obligations import NEEM_STATEMENT, NEWER_NEEM_STATEMENT, neem

    neem(ledger)
    ledger.file(
        NEWER_NEEM_STATEMENT,
        (FIXTURES / "alder_neem_statement_no_minimum.txt").read_text(),
    )
    ledger.file(NEEM_STATEMENT, (FIXTURES / "alder_neem_statement.txt").read_text())
    ledger.derive()
    found = ledger.conn.execute(
        select(event).where(event.c.type == OBLIGATION_SUPERSEDED)
    ).one()
    older = ledger.conn.execute(
        select(obligation).where(obligation.c.status == "superseded")
    ).one()
    newer = ledger.conn.execute(
        select(obligation).where(obligation.c.supersedes_id == older.id)
    ).one()
    assert found.subject_ref == f"finance.obligation:{older.id}"
    assert found.data == {"replacement_ref": f"finance.obligation:{newer.id}"}


def test_payment_alert_notice_is_written_while_all_sinks_are_disabled(
    ledger: Ledger,
) -> None:
    ledger.chequing()
    ledger.manual("payees.csv", "net,Sample network,vendor,SAMPLE NET,,")
    bills(ledger, ("net", "21.08", D(2054, 1, 1), D(2054, 1, 11)))
    ledger.file(
        "alder/paid.csv",
        export(
            (
                CSV_IDENTIFIER,
                D(2054, 1, 7),
                "CAROB E-SEND receiver = SAMPLE NET",
                Decimal("-21.08"),
            ),
        ),
    )
    ledger.derive()
    detected = ids(ledger, "finance.payment.detected")
    assert len(detected) == 1
    found = ledger.conn.execute(select(notice)).one()
    assert found.key == f"alert:{next(iter(detected))}"
    assert found.kind == "alert" and found.state == "pending"
    assert found.event_date == D(2054, 1, 7)
    assert found.first_seen_at == ledger.ctx.clock.now_utc()


@pytest.mark.parametrize("closed_spells", [1, 2])
def test_same_day_native_parser_spells_preserve_cardinality(
    ledger: Ledger,
    closed_spells: int,
) -> None:
    # Separate recoveries on the same date must remain separate, even when both
    # printed dates are equal. The last negative run has no recovery yet.
    repeat = (
        "23 DEC SAMPLE CREDIT  390.88  195.44\n"
        "23 DEC SAMPLE DEBIT AGAIN  390.88  -195.44\n"
    )
    withdrawals = 423 * (closed_spells + 1)
    deposits = 423 * closed_spells
    layout = (
        "JUNE 12, 2055\n"
        "ALDER CYCLE 9 MAY 2055 UNTIL 12 JUN 2055\n"
        "ALDER DAILY CHEQUING No 999000000022  "
        f"195.44  {withdrawals}.00  {deposits}.00  -195.44\n\n"
        "ALDER DAILY CHEQUING No 999000000022\n"
        "ALDER START FIGURE  195.44\n"
        "23 DEC SAMPLE DEBIT  390.88  -195.44\n" + repeat * closed_spells
    )
    parsed = alder_statement(layout)
    assert parsed.failed_checks == 0
    spells = parsed.accounts[0].overdrawn
    expected = [(D(2053, 12, 23), D(2053, 12, 23))] * closed_spells + [
        (D(2053, 12, 23), None)
    ]
    assert [(spell.start, spell.end) for spell in spells] == expected
    ledger.chequing()
    source = ledger.file("alder/same-day-spells.pdf", layout)

    def stored() -> None:
        for table in (parsed_overdrawn, overdrawn_spell):
            found = ledger.conn.execute(select(table).order_by(table.c.spell_no)).all()
            assert [(row.start_date, row.end_date) for row in found] == expected
            assert [row.spell_no for row in found] == list(range(1, len(expected) + 1))
            assert {row.document_id for row in found} == {source}

    ledger.derive()
    stored()
    cached = ledger.derive()
    assert cached.parsed_now == 0 and cached.kept == 1
    stored()
    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, reparse=True)
    stored()


def test_forward_spell_migration_preserves_legacy_cache(ledger: Ledger) -> None:
    ledger.chequing()
    actual = (
        "JUNE 12, 2055\n"
        "ALDER CYCLE 9 MAY 2055 UNTIL 12 JUN 2055\n"
        "ALDER DAILY CHEQUING No 999000000022  195.44  781.76  390.88  -195.44\n\n"
        "ALDER DAILY CHEQUING No 999000000022\n"
        "ALDER START FIGURE  195.44\n"
        "23 DEC SAMPLE DEBIT  390.88  -195.44\n"
        "23 DEC SAMPLE CREDIT  390.88  195.44\n"
        "24 DEC SAMPLE DEBIT AGAIN  390.88  -195.44\n"
    )
    source = ledger.file("alder/2055-06-12.pdf", actual)
    ledger.derive()
    # Recreate the populated 0030 shapes inside the rolled-back test transaction.
    for sql in (
        "ALTER TABLE finance.overdrawn_spell "
        "DROP CONSTRAINT overdrawn_spell_pkey, DROP COLUMN document_id, "
        "DROP COLUMN spell_no, ADD PRIMARY KEY (account_id, currency, start_date)",
        "ALTER TABLE finance.parsed_overdrawn "
        "DROP CONSTRAINT parsed_overdrawn_pkey, DROP COLUMN spell_no, "
        "ADD PRIMARY KEY (document_id, account_id, currency, start_date)",
    ):
        ledger.conn.execute(text(sql))
    revision = import_module("rheo_finance.migrations.versions.0031_overdrawn_identity")
    for sql in revision.STATEMENTS:
        ledger.conn.execute(text(sql))
    cached = ledger.conn.execute(
        select(parsed_overdrawn).order_by(parsed_overdrawn.c.spell_no)
    ).all()
    assert [(row.spell_no, row.start_date, row.end_date) for row in cached] == [
        (1, D(2053, 12, 23), D(2053, 12, 23)),
        (2, D(2053, 12, 24), None),
    ]
    assert {row.document_id for row in cached} == {source}
    assert ledger.conn.scalar(select(func.count()).select_from(overdrawn_spell)) == 0
    derived = ledger.derive()
    assert derived.parsed_now == 0 and derived.kept == 1
    restored = ledger.conn.execute(
        select(overdrawn_spell).order_by(overdrawn_spell.c.spell_no)
    ).all()
    assert [tuple(row) for row in restored] == [tuple(row) for row in cached]
