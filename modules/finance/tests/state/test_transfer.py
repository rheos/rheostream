"""State export and import over two scratch databases (D11): every state table
non-empty in A, exported, imported into a fresh B, both rebuilt: the derived digests
agree and B's export is A's, byte for byte. A second import and a wrong head are
refused, so is an import into a ledger whose document set is not empty; one after
``finance init-data`` and an empty rebuild is accepted. The export takes the writer
lock before its first read (also shown without a database, over a stand-in unit of
work). Values are synthetic.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn, cast

import pytest
from sqlalchemy import Table, create_engine, insert, text
from sqlalchemy.pool import NullPool

from rheo_finance import catalogue, db, operations, queue
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext, FinancePaths, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.derive.digest import digested_tables
from rheo_finance.derive.ops import DigestOut
from rheo_finance.derive.run import CLEARED, FILE_OWNED, REWRITTEN, STATE
from rheo_finance.init_data import init_data
from rheo_finance.operations import OperationError, dispatch
from rheo_finance.settings import parse_settings
from rheo_finance.state import transfer
from rheo_finance.state.transfer import ExportIn, ExportOut, ImportOut
from rheo_finance.storage import schema
from tests.finance_support import (
    SCRATCH_DATABASES,
    DatabaseUnderTest,
    cluster_url,
    drop_database,
    no_snapshot,
    provision_database,
)

T0 = dt.datetime(2063, 1, 18, 12, 0, tzinfo=dt.UTC)
DECISION = uuid.UUID("01a1b2c3-0000-7000-8000-0000000000d1")
# Line refs in the ref grammar (``finance.txn:<UUIDv7>``): the rebuild parses every
# unreversed decision's target. No line carries these keys, so the decision is an
# orphan, which the rebuild lists and derives the same way in both ledgers.
LINE_ONE = "finance.txn:01a1b2c3-0000-7000-8000-0000000000e1"
LINE_TWO = "finance.txn:01a1b2c3-0000-7000-8000-0000000000e2"


@pytest.fixture
def two_databases(
    test_database: DatabaseUnderTest,
) -> Iterator[tuple[DatabaseUnderTest, DatabaseUnderTest]]:
    """Two fresh scratch databases, dropped after the test. ``test_database`` first,
    so an unreachable cluster fails here the way it fails everywhere else."""
    cluster = cluster_url()
    built: list[DatabaseUnderTest] = []
    try:
        for _ in range(2):
            each = provision_database(cluster)
            SCRATCH_DATABASES.add(each.owner_url.database or "")
            built.append(each)
        yield built[0], built[1]
    finally:
        for each in built:
            name = each.owner_url.database or ""
            SCRATCH_DATABASES.discard(name)
            drop_database(cluster, name)


def context_for(database: DatabaseUnderTest, root: Path) -> FinanceContext:
    archive = root / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    ctx = FinanceContext(
        settings=parse_settings(
            f'[finance.paths]\narchive = "{archive}"\n'
            f'downloads = "{root / "downloads-unset"}"\n'
        ),
        clock=FakeClock(T0),
        paths=FinancePaths(data=root / "data"),
        role=Role.OWNER,
        snapshot_provider=no_snapshot,
    )
    return operations.with_databases(
        ctx, owner_url=database.owner_url, reader_url=database.reader_url
    )


def seed(database: DatabaseUnderTest) -> None:
    """One synthetic row in every state table, committed."""
    engine = create_engine(database.owner_url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(schema.decision).values(
                    id=DECISION,
                    kind="include",
                    target_ref=LINE_ONE,
                    decided_at=T0,
                )
            )
            conn.execute(
                insert(schema.decision_item).values(
                    decision_id=DECISION,
                    seq=1,
                    role="leg",
                    target_ref=LINE_TWO,
                )
            )
            conn.execute(
                insert(schema.decision_target).values(
                    decision_id=DECISION,
                    seq=1,
                    target_ref=LINE_ONE,
                    account_slug="ledger",
                    amount=Decimal("-8.50"),
                    currency="CAD",
                    posting_date=dt.date(2063, 1, 2),
                )
            )
            conn.execute(
                insert(schema.rate).values(
                    rate_date=dt.date(2063, 1, 2),
                    base="USD",
                    quote="CAD",
                    rate=Decimal("1.2500"),
                    source="rowan",
                    fetched_at=T0,
                )
            )
            conn.execute(
                insert(schema.rate_coverage).values(
                    series="USD/CAD",
                    source="rowan",
                    covered_from=dt.date(2063, 1, 1),
                    covered_to=dt.date(2063, 1, 3),
                    fetched_at=T0,
                )
            )
            conn.execute(
                insert(schema.notice).values(
                    key="synthetic-notice",
                    kind="nudge",
                    event_date=dt.date(2063, 1, 5),
                    first_seen_at=T0,
                    state="pending",
                )
            )
            conn.execute(
                insert(schema.sink_ticket).values(
                    source_ref="finance:synthetic:ticket",
                    state="post_pending",
                    create_title="Synthetic reminder",
                    create_body="Synthetic body.",
                    create_due_on=dt.date(2063, 1, 9),
                    create_priority="normal",
                )
            )
            # seq is an identity column: the database numbers it.
            conn.execute(
                text(
                    "INSERT INTO finance.job_run"
                    " (id, job, started_at, finished_at, ok, detail_code)"
                    " VALUES (:id, 'finance.tick', :at, :at, true, 'completed')"
                ),
                {"id": uuid.UUID(int=99), "at": T0},
            )
    finally:
        engine.dispose()


def run(name: str, payload: object, ctx: FinanceContext) -> object:
    return dispatch(name, payload, Role.OWNER, ctx=ctx)  # type: ignore[arg-type]


def test_state_round_trips_into_a_fresh_ledger(
    two_databases: tuple[DatabaseUnderTest, DatabaseUnderTest], tmp_path: Path
) -> None:
    catalogue.load_all()
    source, target = two_databases
    seed(source)
    a = context_for(source, tmp_path / "a")
    b = context_for(target, tmp_path / "b")
    run("finance.ledger.rebuild", {}, a)
    exported = run("finance.state.export", {}, a)
    assert isinstance(exported, ExportOut)
    assert {t.name for t in exported.tables} == STATE
    assert all(t.rows > 0 for t in exported.tables), [
        (t.name, t.rows) for t in exported.tables
    ]
    payload: dict[str, object] = {
        "schema_head": exported.schema_head,
        "tables": [{"name": t.name, "lines": list(t.lines)} for t in exported.tables],
    }
    imported = run("finance.state.import", payload, b)
    assert isinstance(imported, ImportOut)
    assert {(t.name, t.rows) for t in imported.tables} == {
        (t.name, t.rows) for t in exported.tables
    }
    run("finance.ledger.rebuild", {}, b)
    again = run("finance.state.export", {}, b)
    assert isinstance(again, ExportOut)
    assert again.derived_digest == exported.derived_digest
    assert again.tables == exported.tables
    # The identity sequence moved past the imported rows: a new run still records.
    recorded = run(
        "finance.job.record",
        {
            "job": "finance.tick",
            "started_at": T0,
            "finished_at": T0,
            "ok": True,
            "detail_code": "completed",
        },
        b,
    )
    assert recorded is not None
    with pytest.raises(OperationError, match="state_not_empty"):
        run("finance.state.import", payload, b)


def test_an_import_at_another_head_is_refused(
    two_databases: tuple[DatabaseUnderTest, DatabaseUnderTest], tmp_path: Path
) -> None:
    catalogue.load_all()
    _, target = two_databases
    b = context_for(target, tmp_path / "b")
    payload = {
        "schema_head": "0001_finance_schema",
        "tables": [{"name": name, "lines": []} for name in sorted(STATE)],
    }
    with pytest.raises(OperationError, match="schema_mismatch"):
        run("finance.state.import", payload, b)


def test_the_import_needs_the_state_and_the_file_owned_tables_empty() -> None:
    names = [table.name for table in transfer.import_must_be_empty()]
    assert len(names) == len(set(names))
    assert names[: len(STATE)] == [table.name for table in transfer.state_tables()]
    assert set(names) == STATE | FILE_OWNED


def test_the_tables_an_empty_rebuild_writes_are_not_checked() -> None:
    """``queue_count`` (one row per queue kind) and ``sink_spec`` (one row per sink)
    get rows from any rebuild, an empty ledger's too; the import must accept them, as
    the rebuild after it clears or rewrites every digested table outside FILE_OWNED."""
    checked = {table.name for table in transfer.import_must_be_empty()}
    digested = {table.name for table in digested_tables()}
    assert queue.KINDS
    assert {"queue_count", "sink_spec"} <= digested - checked
    assert digested - checked == ({t.name for t in CLEARED} | REWRITTEN) - FILE_OWNED


FILE_ROWS = {
    "document": (
        "INSERT INTO finance.document"
        " (id, path, sha256, kind, issuer, format, status, imported_at)"
        " VALUES (:id, 'synthetic/a.pdf', :sha, 'bill', 'sample-issuer', 'pdf',"
        " 'filed', :at)"
    ),
    "archive_skip": (
        "INSERT INTO finance.archive_skip (directory, reason, count)"
        " VALUES ('synthetic', 'unmapped_directory', 1)"
    ),
}


@pytest.mark.parametrize("table", sorted(FILE_ROWS))
def test_an_import_into_a_ledger_with_files_is_refused(
    two_databases: tuple[DatabaseUnderTest, DatabaseUnderTest],
    tmp_path: Path,
    table: str,
) -> None:
    catalogue.load_all()
    _, target = two_databases
    engine = create_engine(target.owner_url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(FILE_ROWS[table]),
                {"id": uuid.UUID(int=7), "sha": "a" * 64, "at": T0},
            )
    finally:
        engine.dispose()
    b = context_for(target, tmp_path / "b")
    payload = {
        "schema_head": db.head_revision(),
        "tables": [{"name": name, "lines": []} for name in sorted(STATE)],
    }
    with pytest.raises(OperationError, match="state_not_empty"):
        run("finance.state.import", payload, b)


def test_an_import_after_init_data_and_an_empty_rebuild_is_accepted(
    two_databases: tuple[DatabaseUnderTest, DatabaseUnderTest], tmp_path: Path
) -> None:
    """A fresh install (the migration chain), ``finance init-data`` and a rebuild of
    the empty ledger leave derived rows (the queue snapshot) and no state or file
    rows; the import is accepted there."""
    catalogue.load_all()
    _, target = two_databases
    b = context_for(target, tmp_path / "b")
    init_data(tmp_path / "b" / "data")
    run("finance.ledger.rebuild", {}, b)
    found = run("finance.ledger.digest", {}, b)
    assert isinstance(found, DigestOut)
    counts = {each.name: each.count for each in found.tables}
    assert counts["queue_count"] == len(queue.KINDS)
    assert all(counts[name] == 0 for name in FILE_OWNED)
    payload = {
        "schema_head": db.head_revision(),
        "tables": [{"name": name, "lines": []} for name in sorted(STATE)],
    }
    imported = run("finance.state.import", payload, b)
    assert isinstance(imported, ImportOut)
    assert {(t.name, t.rows) for t in imported.tables} == {(n, 0) for n in STATE}


def test_the_export_takes_the_writer_lock_before_it_reads(
    test_database: DatabaseUnderTest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every writer takes the lock until it commits, so an export that cannot enter
    while another connection holds it reads its tables and its digest with no commit
    landing between them. Each read fails the test if it runs, so the export must
    raise ``WriterBusy`` before its first read, not merely at some point."""
    catalogue.load_all()
    ctx = context_for(test_database, tmp_path / "a")
    monkeypatch.setattr(transfer, "writer_lock", bounded)
    engine = create_engine(test_database.owner_url, poolclass=NullPool)
    try:
        with engine.connect() as holder:
            holder.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": db.WRITER_LOCK_KEY}
            )
            with monkeypatch.context() as reads:
                for name in EXPORT_READS:
                    reads.setattr(transfer, name, read_before_the_lock)
                with pytest.raises(db.WriterBusy):
                    run("finance.state.export", {}, ctx)
        # Closing the holder ended its session and its lock; the reads are real again.
        assert isinstance(run("finance.state.export", {}, ctx), ExportOut)
    finally:
        engine.dispose()


# What the export reads, in order: the schema version, each state table, the digest.
EXPORT_READS = ("_schema_version", "table_lines", "ledger_digest")


def bounded(connection: db.Statements) -> AbstractContextManager[None]:
    """The writer lock with no wait: a held lock is ``WriterBusy`` at once."""
    return db.writer_lock(connection, wait_seconds=0)


def read_before_the_lock(*_args: object) -> NoReturn:
    pytest.fail("the export read before it held the writer lock")


class LockOnly:
    """A stand-in unit of work that answers only the writer lock's statement, with
    ``granted``, and records each try in ``events``."""

    def __init__(self, events: list[str], *, granted: bool) -> None:
        self.events = events
        self.granted = granted

    def execute(self, statement: object, _parameters: object = None) -> LockOnly:
        assert "pg_try_advisory_xact_lock" in str(statement)
        self.events.append("lock")
        return self

    def scalar_one(self) -> bool:
        return self.granted


def export_with(uow: LockOnly) -> ExportOut:
    return transfer.export_state(
        cast(FinanceContext, None), cast(UnitOfWork, uow), ExportIn()
    )


def test_a_busy_lock_stops_the_export_before_any_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transfer, "writer_lock", bounded)
    for name in EXPORT_READS:
        monkeypatch.setattr(transfer, name, read_before_the_lock)
    events: list[str] = []
    with pytest.raises(db.WriterBusy):
        export_with(LockOnly(events, granted=False))
    assert events == ["lock"]


def test_the_export_takes_the_lock_first_then_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def version(_uow: object) -> str:
        events.append("version")
        return "head"

    def lines(_uow: object, table: Table) -> list[str]:
        events.append(f"table {table.name}")
        return []

    def digest(_uow: object) -> SimpleNamespace:
        events.append("digest")
        return SimpleNamespace(digest="0" * 64)

    monkeypatch.setattr(transfer, "_schema_version", version)
    monkeypatch.setattr(transfer, "table_lines", lines)
    monkeypatch.setattr(transfer, "ledger_digest", digest)
    out = export_with(LockOnly(events, granted=True))
    assert events == [
        "lock",
        "version",
        *(f"table {table.name}" for table in transfer.state_tables()),
        "digest",
    ]
    assert out.schema_head == "head"
    assert {table.name for table in out.tables} == STATE
