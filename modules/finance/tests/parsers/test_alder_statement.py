"""The Alder statement through ``parse_document`` (FR-3, FR-7, AC-1, AC-3a).

Each test registers synthetic accounts through the manual files and
``manual.stage2.load_accounts``, files a synthetic statement into a temporary archive
with ``documents.registry.file_archive``, and parses it with ``parse_document``. The
fixtures are layout text (what pdftotext prints), so the tests read the archived file
as text where the real run calls pdftotext. Every value is fake.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Connection, Table, func, select

from rheo_finance.context import UnitOfWork
from rheo_finance.documents.registry import document_id, file_archive
from rheo_finance.manual.stage2 import account_id
from rheo_finance.parsers import alder_statement
from rheo_finance.parsers.base import OverdrawnSpell
from rheo_finance.parsers.registry import (
    DOCUMENT_REJECTED,
    Parsed,
    account_lookup,
    parse_document,
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
STATEMENT_OLDER = FIXTURES / "alder_statement_older.txt"
STATEMENT_2055 = FIXTURES / "alder_statement_newer.txt"
STATEMENT_SPLIT = FIXTURES / "alder_statement_split.txt"
NOW = dt.datetime(2043, 1, 17, 12, 0, tzinfo=dt.UTC)

# slug -> (label, the fake account number the fixtures print)
ACCOUNTS = {
    "bonus": ("Bonus pot", "999000000011"),
    "chequing": ("Chequing", "999000000022"),
    "savings": ("Savings", "999000000033"),
    "nest": ("Nest pot", "999000000044"),
    "spare-jar": ("Spare jar", "999000000055"),
}


def read_as_text(path: Path) -> str:
    """The fixtures are already layout text: what pdftotext would print."""
    return path.read_text(encoding="utf-8")


class Setup:
    def __init__(self, conn: Connection, tmp_path: Path) -> None:
        self.conn = conn
        self.uow = UnitOfWork(conn)
        self.data = tmp_path / "data"
        self.archive = tmp_path / "archive"
        self.archive.mkdir()

    def register(self, slugs: tuple[str, ...] = tuple(ACCOUNTS)) -> None:
        register_accounts(
            self.uow,
            self.data,
            *(
                ManualAccount(
                    slug,
                    "alder",  # compared without case
                    "chequing",
                    (ACCOUNTS[slug][1],),
                    label=ACCOUNTS[slug][0],
                )
                for slug in slugs
            ),
        )

    def file(self, name: str, text: str) -> uuid.UUID:
        target = self.archive / "alder" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        target.write_bytes(data)
        file_archive(self.uow, self.archive, self.data, NOW)
        return document_id(hashlib.sha256(data).hexdigest())

    def parse(self, doc: uuid.UUID) -> Parsed:
        return parse_document(self.uow, doc, self.archive, extract_pdf=read_as_text)

    def count(self, table: Table, doc: uuid.UUID) -> int:
        query = select(func.count()).select_from(table)
        return int(self.conn.scalar(query.where(table.c.document_id == doc)) or 0)

    def rejected_events(self, doc: uuid.UUID) -> list[dict[str, object]]:
        ref = format_ref("finance", "document", doc)
        return [
            dict(data)
            for data in self.conn.scalars(
                select(event.c.data).where(
                    event.c.type == DOCUMENT_REJECTED, event.c.subject_ref == ref
                )
            )
        ]

    def document_row(self, doc: uuid.UUID) -> dict[str, object]:
        row = self.conn.execute(select(document).where(document.c.id == doc)).one()
        return dict(row._mapping)


@pytest.fixture
def setup(owner_conn: Connection, tmp_path: Path) -> Setup:
    return Setup(owner_conn, tmp_path)


def test_a_tied_out_statement_commits_its_periods_and_lines(setup: Setup) -> None:
    setup.register()
    doc = setup.file("2055-06-11.pdf", STATEMENT_2055.read_text(encoding="utf-8"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=4, lines=12)

    row = setup.document_row(doc)
    assert (row["status"], row["parser_id"], row["parser_version"]) == (
        "parsed", "alder.statement", "2",
    )  # fmt: skip
    assert (row["extraction"], row["reject_reason"]) == ("fixed_rule", None)
    assert (row["period_start"], row["period_end"]) == (
        dt.date(2055, 5, 9), dt.date(2055, 6, 12),
    )  # fmt: skip
    assert setup.rejected_events(doc) == []

    chequing = account_id("chequing")
    period = setup.conn.execute(
        select(statement_period).where(
            statement_period.c.document_id == doc,
            statement_period.c.account_id == chequing,
        )
    ).one()
    assert (period.opening, period.closing) == (Decimal("589.80"), Decimal("74.76"))
    assert (period.total_debits, period.total_credits) == (
        Decimal("3031.80"), Decimal("2516.76"),
    )  # fmt: skip
    assert (period.currency, period.control_kind, period.tied_out) == (
        "CAD", "summary", True,
    )  # fmt: skip
    tied = setup.conn.scalars(
        select(statement_period.c.tied_out).where(statement_period.c.document_id == doc)
    ).all()
    assert tied == [True] * 4

    lines = setup.conn.execute(
        select(document_line)
        .where(
            document_line.c.document_id == doc,
            document_line.c.account_id == chequing,
        )
        .order_by(document_line.c.line_no)
    ).all()
    assert len(lines) == 11
    assert sum(line.amount for line in lines) == Decimal("74.76") - Decimal("589.80")
    assert lines[-1].running_balance == Decimal("74.76")
    assert [line.running_balance for line in lines[6:8]] == [
        Decimal("-141.78"), Decimal("-161.44"),
    ]  # fmt: skip
    assert [line.fee_type for line in lines if line.fee_type] == [
        "ALDER MONTH FEE", "ALDER COUNT FEES", "OVERDRAFT FEE",
    ]  # fmt: skip
    assert {line.line_kind for line in lines} == {"transaction"}
    assert lines[0].posting_date == dt.date(2055, 5, 11)
    assert lines[-1].posting_date == dt.date(2055, 6, 10)


def test_the_overdrawn_spell_rides_on_the_period(setup: Setup) -> None:
    setup.register()
    result = alder_statement.parse(
        STATEMENT_2055.read_text(encoding="utf-8"), account_lookup(setup.uow)
    )
    spells = {period.account.slug: period.overdrawn for period in result.periods}
    assert spells["chequing"] == (
        OverdrawnSpell(dt.date(2055, 6, 2), dt.date(2055, 6, 5)),
    )
    assert spells["savings"] == ()


def test_one_cent_off_is_rejected_with_zero_lines_and_an_event(setup: Setup) -> None:
    setup.register()
    text = STATEMENT_2055.read_text(encoding="utf-8")
    summary = "2,516.76          74.76"
    assert text.count(summary) == 1
    doc = setup.file("2055-06-11.pdf", text.replace(summary, "2,516.76          74.77"))

    assert setup.parse(doc) == Parsed("rejected", "checks_failed")

    assert setup.count(document_line, doc) == 0
    assert setup.count(statement_period, doc) == 0
    row = setup.document_row(doc)
    assert (row["status"], row["reject_reason"]) == ("rejected", "checks_failed")
    assert row["period_end"] == dt.date(2055, 6, 12)
    ref = format_ref("finance", "document", doc)
    assert setup.rejected_events(doc) == [
        {"document_ref": ref, "reason": "checks_failed"}
    ]


def test_reparsing_a_rejected_statement_rewrites_its_rejected_periods(
    setup: Setup,
) -> None:
    setup.register(("bonus", "chequing", "savings"))  # not the nest account
    text = STATEMENT_2055.read_text(encoding="utf-8")
    summary = "2,516.76          74.76"
    doc = setup.file("2055-06-11.pdf", text.replace(summary, "2,516.76          74.77"))

    for _ in range(2):
        assert setup.parse(doc) == Parsed("rejected", "checks_failed")
        # One row per registered account the statement names, never a second copy.
        assert setup.count(rejected_period, doc) == 3


def test_an_unregistered_account_rejects_the_statement(setup: Setup) -> None:
    setup.register(("bonus", "chequing", "savings"))  # not the nest account
    doc = setup.file("2055-06-11.pdf", STATEMENT_2055.read_text(encoding="utf-8"))

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")

    assert setup.count(document_line, doc) == 0
    assert setup.count(statement_period, doc) == 0
    assert [each["reason"] for each in setup.rejected_events(doc)] == [
        "unknown_account"
    ]


def alder(slug: str, *numbers: str) -> ManualAccount:
    return ManualAccount(slug, "alder", "chequing", numbers, label=slug.title())


def test_a_section_two_accounts_hold_is_ambiguous_before_a_later_unknown_one(
    setup: Setup,
) -> None:
    """Two distinct full numbers ending in the bonus pot section's last four both
    load; that section names two accounts. It comes before the unregistered nest
    section, and the first failing section decides the reason."""
    register_accounts(
        setup.uow,
        setup.data,
        *(alder(slug, ACCOUNTS[slug][1]) for slug in ("bonus", "chequing", "savings")),
        alder("other", "999000100011"),
    )
    doc = setup.file("2055-06-11.pdf", STATEMENT_2055.read_text(encoding="utf-8"))

    assert setup.parse(doc) == Parsed("rejected", "ambiguous_account")
    assert setup.count(document_line, doc) == 0
    assert [each["reason"] for each in setup.rejected_events(doc)] == [
        "ambiguous_account"
    ]


def test_two_sections_on_one_account_are_ambiguous(setup: Setup) -> None:
    """One account holding the numbers of two sections would take both: ambiguous."""
    register_accounts(
        setup.uow,
        setup.data,
        alder("bonus", ACCOUNTS["bonus"][1]),
        alder("chequing", ACCOUNTS["chequing"][1], ACCOUNTS["savings"][1]),
        alder("nest", ACCOUNTS["nest"][1]),
    )
    doc = setup.file("2055-06-11.pdf", STATEMENT_2055.read_text(encoding="utf-8"))

    assert setup.parse(doc) == Parsed("rejected", "ambiguous_account")
    assert setup.count(statement_period, doc) == 0


def test_registering_the_account_and_reparsing_loads_it_and_drops_the_event(
    setup: Setup,
) -> None:
    setup.register(("bonus", "chequing", "savings"))
    doc = setup.file("2055-06-11.pdf", STATEMENT_2055.read_text(encoding="utf-8"))
    assert setup.parse(doc).status == "rejected"

    setup.register()
    assert setup.parse(doc) == Parsed("parsed", None, periods=4, lines=12)
    assert setup.rejected_events(doc) == []
    assert setup.document_row(doc)["reject_reason"] is None


def test_a_renamed_account_still_matches_by_its_number(setup: Setup) -> None:
    """The chequing account prints one name in 2037 and another in 2055; both
    statements land on the one registered account."""
    setup.register()
    old = setup.file("2037-01-11.pdf", STATEMENT_OLDER.read_text(encoding="utf-8"))
    new = setup.file("2055-06-11.pdf", STATEMENT_2055.read_text(encoding="utf-8"))

    assert setup.parse(old) == Parsed("parsed", None, periods=5, lines=11)
    assert setup.parse(new).status == "parsed"

    chequing = account_id("chequing")
    on_chequing = set(
        setup.conn.scalars(
            select(statement_period.c.document_id).where(
                statement_period.c.account_id == chequing
            )
        )
    )
    assert on_chequing == {old, new}
    # The nicknamed account closed in the period: opening to zero, one notice line.
    side = setup.conn.execute(
        select(statement_period.c.opening, statement_period.c.closing).where(
            statement_period.c.document_id == old,
            statement_period.c.account_id == account_id("extra-pot"),
        )
    ).one()
    assert tuple(side) == (Decimal("9.46"), Decimal("0.00"))
    kinds = setup.conn.execute(
        select(document_line.c.line_kind, document_line.c.amount)
        .where(
            document_line.c.document_id == old,
            document_line.c.account_id == account_id("extra-pot"),
        )
        .order_by(document_line.c.line_no)
    ).all()
    assert [tuple(each) for each in kinds] == [
        ("notice_fact", None), ("transaction", Decimal("-9.46")),
    ]  # fmt: skip


def test_an_unrecognised_layout_is_unparsed_with_zero_lines(setup: Setup) -> None:
    setup.register()
    doc = setup.file("2055-06-11.pdf", "A PAGE OF TEXT THAT IS NOT A STATEMENT\n" * 9)

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")

    assert setup.count(document_line, doc) == 0
    assert setup.count(statement_period, doc) == 0
    assert setup.rejected_events(doc) == []
    row = setup.document_row(doc)
    assert (row["status"], row["parser_id"]) == ("unparsed", None)


def test_reparsing_gives_the_same_rows(setup: Setup) -> None:
    setup.register()
    doc = setup.file("2055-06-11.pdf", STATEMENT_2055.read_text(encoding="utf-8"))
    setup.parse(doc)
    first = setup.conn.scalars(
        select(document_line.c.id).where(document_line.c.document_id == doc)
    ).all()
    assert setup.parse(doc).lines == 12
    again = setup.conn.scalars(
        select(document_line.c.id).where(document_line.c.document_id == doc)
    ).all()
    assert sorted(first) == sorted(again) and len(set(first)) == 12


def test_a_line_with_a_withdrawal_and_a_deposit_becomes_two_lines(
    setup: Setup,
) -> None:
    """The withdrawal first, carrying the balance between the two, which the
    statement does not print."""
    setup.register(("chequing",))
    doc = setup.file("2055-06-11.pdf", STATEMENT_SPLIT.read_text(encoding="utf-8"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=3)

    lines = setup.conn.execute(
        select(
            document_line.c.posting_date,
            document_line.c.amount,
            document_line.c.running_balance,
        )
        .where(document_line.c.document_id == doc)
        .order_by(document_line.c.line_no)
    ).all()
    day = dt.date(2055, 5, 15)
    assert [tuple(each) for each in lines] == [
        (day, Decimal("-6.60"), Decimal("386.60")),
        (day, Decimal("196.60"), Decimal("583.20")),
        (dt.date(2055, 5, 20), Decimal("-117.96"), Decimal("465.24")),
    ]


# --- hints (FR-15, Phase 3a deliverable 1) --------------------------------------------


def hint_rows(setup: Setup, doc: uuid.UUID) -> list[tuple[object, ...]]:
    rows = setup.conn.execute(
        select(
            document_line.c.description_raw,
            document_line.c.transfer_hint,
            document_line.c.etransfer_hint,
            document_line.c.hint_account_slug,
            document_line.c.counterparty,
        )
        .where(document_line.c.document_id == doc)
        .order_by(document_line.c.line_no)
    ).all()
    return [tuple(each) for each in rows]


def test_the_chequing_leg_of_a_card_payment_names_the_card_issuer(
    setup: Setup,
) -> None:
    setup.register(("chequing",))
    text = STATEMENT_SPLIT.read_text(encoding="utf-8")
    assert text.count("POWER CO #0001") == 1
    card_payment = text.replace("POWER CO #0001", "ALDER NEEM #9991")
    doc = setup.file("2055-06-11.pdf", card_payment)

    assert setup.parse(doc).status == "parsed"
    payment = hint_rows(setup, doc)[-1]
    assert payment[1:] == (True, False, "alder-neem", "ALDER NEEM")


def test_an_etransfer_carries_its_hint_and_name_and_a_purchase_none(
    setup: Setup,
) -> None:
    setup.register()
    doc = setup.file("2055-06-11.pdf", STATEMENT_2055.read_text(encoding="utf-8"))
    assert setup.parse(doc).status == "parsed"

    by_wording = {str(row[0]): row[1:] for row in hint_rows(setup, doc)}
    assert by_wording["ESEND CREDIT (FRIEND)"] == (False, True, None, "friend")
    assert by_wording["ESEND DEBIT (LANDLORD)"] == (False, True, None, "landlord")
    # A payment to a vendor is spending: its payee, no transfer hint.
    assert by_wording["SETTLE TO (POWER CO #0001)"] == (
        False, False, None, "POWER CO",
    )  # fmt: skip
    purchases = [row for text, row in by_wording.items() if "TILL BUY" in text]
    assert purchases and all(row == (False, False, None, None) for row in purchases)


def test_the_statement_registers_as_hint_setting() -> None:
    parser = alder_statement.PARSER
    assert (parser.version, parser.sets_hints) == ("2", True)
