"""The headerless Alder chequing CSV through ``parse_document`` (FR-3
``running_balance``, FR-8, edge case 7).

The fixture lists six rows newest first, with rows missing between its 7 Oct and
14 Oct lines (the balance jumps there). Every value is fake, and the layout is the
parser's invented one; the account identifier is registered as the account's
``number`` identifier.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Connection, select

from rheo_finance.context import UnitOfWork
from rheo_finance.documents.registry import document_id, file_archive
from rheo_finance.manual.stage2 import account_id
from rheo_finance.parsers import alder_chequing_csv
from rheo_finance.parsers.base import BalanceGap, VerifiedSegment
from rheo_finance.parsers.registry import (
    DOCUMENT_REJECTED,
    Parsed,
    account_lookup,
    parse_document,
)
from rheo_finance.storage.schema import document, document_line, event, statement_period

from ..csv_layouts import csv_text
from .conftest import ManualAccount, register_accounts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
JUMP = FIXTURES / "alder_chequing_jump.csv"
NOW = dt.datetime(2039, 2, 21, 12, 0, tzinfo=dt.UTC)
IDENTIFIER = "999.07.77"
D = dt.date


class Setup:
    def __init__(self, conn: Connection, tmp_path: Path) -> None:
        self.conn = conn
        self.uow = UnitOfWork(conn)
        self.data = tmp_path / "data"
        self.archive = tmp_path / "archive"
        self.archive.mkdir()

    def register(self, *identifiers: str) -> None:
        """One chequing account holding ``identifiers`` (the fixture's by default)."""
        register_accounts(
            self.uow,
            self.data,
            ManualAccount(
                "chequing", "Alder", "chequing", identifiers or (IDENTIFIER,)
            ),
        )

    def file(self, text: str, name: str = "transactions-2039-02.csv") -> uuid.UUID:
        target = self.archive / "alder" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        target.write_bytes(data)
        file_archive(self.uow, self.archive, self.data, NOW)
        return document_id(hashlib.sha256(data).hexdigest())

    def parse(self, doc: uuid.UUID) -> Parsed:
        return parse_document(self.uow, doc, self.archive)

    def lines(self, doc: uuid.UUID) -> list[tuple[object, ...]]:
        rows = self.conn.execute(
            select(
                document_line.c.posting_date,
                document_line.c.amount,
                document_line.c.running_balance,
            )
            .where(document_line.c.document_id == doc)
            .order_by(document_line.c.line_no)
        ).all()
        return [tuple(row) for row in rows]

    def period(self, doc: uuid.UUID) -> dict[str, object]:
        row = self.conn.execute(
            select(statement_period).where(statement_period.c.document_id == doc)
        ).one()
        return dict(row._mapping)


@pytest.fixture
def setup(owner_conn: Connection, tmp_path: Path) -> Setup:
    return Setup(owner_conn, tmp_path)


def row(
    day: str, withdrawal: str, deposit: str, balance: str, identifier: str = IDENTIFIER
) -> str:
    """One export row of the parser's public layout (``csv_layouts``)."""
    return csv_text(
        "alder_chequing",
        [{"description": "SAMPLE ITEM", "balance": balance, "date": day,
          "deposit": deposit, "withdrawal": withdrawal, "account": identifier}],
    )  # fmt: skip


def jump_text() -> str:
    return JUMP.read_text(encoding="utf-8")


def after_the_jump() -> str:
    """The fixture's three newest rows: one continuous run."""
    return "".join(jump_text().splitlines(keepends=True)[:3])


def test_a_continuous_export_is_one_verified_segment(setup: Setup) -> None:
    setup.register()
    doc = setup.file(after_the_jump())

    assert setup.parse(doc) == Parsed(
        "parsed", None, periods=1, lines=3, segments=1, gaps=0
    )

    # Oldest first, though the file lists newest first.
    assert setup.lines(doc) == [
        (D(2038, 10, 14), Decimal("-81.67"), Decimal("472.05")),
        (D(2038, 10, 14), Decimal("93.00"), Decimal("565.05")),
        (D(2038, 10, 18), Decimal("-3.69"), Decimal("561.36")),
    ]
    period = setup.period(doc)
    assert period["account_id"] == account_id("chequing")
    assert (period["control_kind"], period["tied_out"], period["currency"]) == (
        "running_balance", True, "CAD",
    )  # fmt: skip
    assert (period["period_start"], period["period_end"]) == (
        D(2038, 10, 14), D(2038, 10, 18),
    )  # fmt: skip
    row = setup.conn.execute(
        select(document.c.status, document.c.parser_id).where(document.c.id == doc)
    ).one()
    assert tuple(row) == ("parsed", "alder.chequing_csv")


def test_a_balance_jump_loads_both_lines_and_returns_a_gap(setup: Setup) -> None:
    setup.register()
    doc = setup.file(jump_text())

    assert setup.parse(doc) == Parsed(
        "parsed", None, periods=1, lines=6, segments=2, gaps=1
    )

    lines = setup.lines(doc)
    assert len(lines) == 6
    assert lines[2] == (D(2038, 10, 7), Decimal("-89.57"), Decimal("654.43"))
    assert lines[3] == (D(2038, 10, 14), Decimal("-81.67"), Decimal("472.05"))
    period = setup.period(doc)
    assert (period["control_kind"], period["tied_out"]) == ("running_balance", False)
    assert (period["period_start"], period["period_end"]) == (
        D(2038, 10, 4), D(2038, 10, 18),
    )  # fmt: skip

    result = alder_chequing_csv.parse(jump_text(), account_lookup(setup.uow))
    (registered,) = {period.account for period in result.periods}
    assert result.gaps == (BalanceGap(registered, D(2038, 10, 7), D(2038, 10, 14)),)
    assert result.segments == (
        VerifiedSegment(registered, D(2038, 10, 4), D(2038, 10, 7)),
        VerifiedSegment(registered, D(2038, 10, 14), D(2038, 10, 18)),
    )


def test_the_account_identifier_is_stored_nowhere(setup: Setup) -> None:
    setup.register()
    doc = setup.file(jump_text())
    setup.parse(doc)

    digits = IDENTIFIER.replace(".", "")
    for table in (document_line, statement_period, document, event):
        for row in setup.conn.execute(select(table)):
            for value in row:
                text = str(value)
                assert IDENTIFIER not in text and digits not in text, table.name


def test_an_unregistered_identifier_is_rejected(setup: Setup) -> None:
    setup.register("999.08.88")
    doc = setup.file(jump_text())

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")

    assert setup.lines(doc) == []
    reasons = [
        data["reason"]
        for data in setup.conn.scalars(
            select(event.c.data).where(event.c.type == DOCUMENT_REJECTED)
        )
    ]
    assert reasons == ["unknown_account"]


def test_a_csv_of_another_layout_is_unparsed(setup: Setup) -> None:
    setup.register()
    header = csv_text("hazel", [])  # a header row the headerless export never has
    doc = setup.file(header + after_the_jump())

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")
    assert setup.lines(doc) == []


def test_a_lone_line_between_two_breaks_verifies_nothing(setup: Setup) -> None:
    setup.register()
    text = (
        row("2039/02/01", "", "124.00", "124.00")
        + row("2039/02/02", "12.40", "", "111.60")  # continues the first
        + row("2039/02/05", "6.20", "", "62.00")  # a break before and after
        + row("2039/02/07", "6.20", "", "24.80")
    )
    doc = setup.file(text)

    assert setup.parse(doc) == Parsed(
        "parsed", None, periods=1, lines=4, segments=1, gaps=2
    )
    result = alder_chequing_csv.parse(text, account_lookup(setup.uow))
    (registered,) = {period.account for period in result.periods}
    assert result.segments == (
        VerifiedSegment(registered, D(2039, 2, 1), D(2039, 2, 2)),
    )
    assert result.gaps == (
        BalanceGap(registered, D(2039, 2, 2), D(2039, 2, 5)),
        BalanceGap(registered, D(2039, 2, 5), D(2039, 2, 7)),
    )
    assert setup.period(doc)["tied_out"] is False


def test_a_one_row_file_loads_its_line_and_verifies_nothing(setup: Setup) -> None:
    setup.register()
    doc = setup.file(row("2038/10/05", "10.95", "", "97.02"))

    assert setup.parse(doc) == Parsed(
        "parsed", None, periods=1, lines=1, segments=0, gaps=0
    )
    assert setup.lines(doc) == [(D(2038, 10, 5), Decimal("-10.95"), Decimal("97.02"))]
    period = setup.period(doc)
    assert (period["period_start"], period["period_end"]) == (
        D(2038, 10, 5), D(2038, 10, 5),
    )  # fmt: skip
    assert period["tied_out"] is False


def test_two_identifiers_of_one_account_in_one_file_are_ambiguous(
    setup: Setup,
) -> None:
    other = "999.06.66"
    setup.register(IDENTIFIER, other)
    doc = setup.file(
        row("2038/10/04", "", "124.00", "124.00")
        + row("2038/10/05", "12.40", "", "111.60", identifier=other)
    )

    assert setup.parse(doc) == Parsed("rejected", "ambiguous_account")
    assert setup.lines(doc) == []


def test_a_row_with_a_withdrawal_and_a_deposit_becomes_two_lines(
    setup: Setup,
) -> None:
    """The withdrawal first, carrying the balance between them; the run stays
    continuous."""
    setup.register()
    doc = setup.file(
        row("2038/10/04", "", "124.00", "124.00")
        + row("2038/10/05", "19.33", "56.53", "161.20")
    )

    assert setup.parse(doc) == Parsed(
        "parsed", None, periods=1, lines=3, segments=1, gaps=0
    )
    assert setup.lines(doc) == [
        (D(2038, 10, 4), Decimal("124.00"), Decimal("124.00")),
        (D(2038, 10, 5), Decimal("-19.33"), Decimal("104.67")),
        (D(2038, 10, 5), Decimal("56.53"), Decimal("161.20")),
    ]


def test_an_oldest_first_file_gives_the_same_lines(setup: Setup) -> None:
    setup.register()
    newest_first = setup.file(jump_text())
    oldest_first = setup.file(
        "".join(reversed(jump_text().splitlines(keepends=True))), "oldest-first.csv"
    )

    expected = Parsed("parsed", None, periods=1, lines=6, segments=2, gaps=1)
    assert setup.parse(newest_first) == expected
    assert setup.parse(oldest_first) == expected
    assert setup.lines(oldest_first) == setup.lines(newest_first)


def test_the_registered_identifier_matches_whatever_its_separators(
    setup: Setup,
) -> None:
    setup.register(IDENTIFIER.replace(".", " "))
    doc = setup.file(jump_text())

    assert setup.parse(doc).status == "parsed"
    assert setup.period(doc)["account_id"] == account_id("chequing")


# --- hints (FR-15, Phase 3a deliverable 1) --------------------------------------------

HINTS = FIXTURES / "alder_chequing_hints.csv"
HINT_COLUMNS = (
    document_line.c.transfer_hint,
    document_line.c.reversal_hint,
    document_line.c.conversion_hint,
    document_line.c.etransfer_hint,
    document_line.c.hint_account_slug,
    document_line.c.counterparty,
)


def test_each_line_carries_the_hints_its_wording_gives(setup: Setup) -> None:
    """The e-Transfer carries its hint and its printed name, the chequing leg of a
    card payment a transfer hint naming the card issuer, a purchase none."""
    setup.register()
    doc = setup.file(HINTS.read_text(encoding="utf-8"))

    assert setup.parse(doc) == Parsed(
        "parsed", None, periods=1, lines=3, segments=1, gaps=0
    )
    rows = setup.conn.execute(
        select(*HINT_COLUMNS)
        .where(document_line.c.document_id == doc)
        .order_by(document_line.c.line_no)
    ).all()
    assert [tuple(each) for each in rows] == [
        (False, False, False, True, None, "SAMPLE FRIEND"),
        (True, False, False, False, "alder-neem", "ALDER NEEM"),
        (False, False, False, False, None, None),
    ]


def test_the_chequing_export_registers_as_hint_setting() -> None:
    parser = alder_chequing_csv.PARSER
    assert (parser.version, parser.sets_hints) == ("2", True)
