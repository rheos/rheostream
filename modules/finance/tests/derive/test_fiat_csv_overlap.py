"""The fiat CSV exports dedupe against the PDFs of the same account and against a
second copy of themselves (FR-4, FR-12(a); AC-80 dedupe clause).

Seam: ``derive.run`` over the synthetic Hazel, Fig and Juniper CSVs (the first two
filed beside the PDF fixtures of the same vendor), read back through ``txn`` and
``txn_source``. The CSV parsers give the PDF parsers' lines (same sign, same amount and
currency per leg, the fee as its own line), so stage 3's one-to-one matcher (account,
currency, signed amount, a date inside the tolerance) pairs each shared transaction
into one txn with two sources. Every value is synthetic.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import Table, func, select

from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.stage2 import account_id
from rheo_finance.storage.schema import (
    coverage,
    document,
    document_line,
    txn,
    txn_source,
)

from ..csv_layouts import csv_text, fixture_rows
from .support import FIXTURES, D, Ledger

HAZEL_PDF = "hazel/2030-11-report.pdf"
HAZEL_CSV = "hazel/activity-2046-04-30.csv"
FIG_PDF = "fig/2045-08-history.pdf"
FIG_CSV = "fig/statement-2045-08-28.csv"
JUNIPER_CSV = "juniper/balance-2045-08-27.csv"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def register(
    ledger: Ledger, slug: str, institution: str, kind: str, number: str
) -> None:
    """One two-currency account; its number is what the PDF prints."""
    ledger.manual(
        ACCOUNTS_FILE, f"{slug},{slug.title()},{institution},{kind},CAD;USD,monthly"
        ",,,,,,,,,",
    )  # fmt: skip
    ledger.manual(IDENTIFIERS_FILE, f"{slug},{number},number,,")


def sources_by_txn(ledger: Ledger) -> Counter[uuid.UUID]:
    return Counter(ledger.conn.scalars(select(txn_source.c.txn_id)))


def documents_of(ledger: Ledger, txn_id: uuid.UUID) -> set[uuid.UUID]:
    return set(
        ledger.conn.scalars(
            select(document_line.c.document_id)
            .join(txn_source, txn_source.c.document_line_id == document_line.c.id)
            .where(txn_source.c.txn_id == txn_id)
        )
    )


def count(ledger: Ledger, table: Table) -> int:
    return int(ledger.conn.scalar(select(func.count()).select_from(table)) or 0)


def test_a_hazel_csv_overlapping_the_statement_adds_only_its_own_line(
    ledger: Ledger,
) -> None:
    register(ledger, "hazel", "Hazel", "payment_processor", "AB12CD34")
    pdf = ledger.file(HAZEL_PDF, fixture("hazel_activity.txt"))
    csv = ledger.file(HAZEL_CSV, fixture("hazel_activity.csv"))

    out = ledger.derive()

    assert out.documents == Counter({"parsed": 2})
    # The statement's 6 lines are txns; the CSV's 6 November lines (the fee line among
    # them) are their second sources, and only its October purchase, before the
    # statement, is a txn of its own.
    assert (out.lines.from_documents, out.lines.additional_sources) == (7, 6)
    assert out.lines.discrepancies == 0
    per_txn = sources_by_txn(ledger)
    assert sorted(per_txn.values()) == [1, 2, 2, 2, 2, 2, 2]
    for txn_id, sources in per_txn.items():
        assert documents_of(ledger, txn_id) == ({pdf, csv} if sources == 2 else {csv})
    rows = ledger.txns()
    assert [row["basis"] for row in rows].count("statement") == 6
    march = [row for row in rows if row["posting_date"] == D(2030, 10, 28)]
    assert [row["basis"] for row in march] == ["verified_export"]


def test_a_fig_csv_and_the_history_pdf_are_one_line_two_sources(
    ledger: Ledger,
) -> None:
    register(ledger, "fig", "Fig", "wallet", "W-0001-ZZ")
    pdf = ledger.file(FIG_PDF, fixture("fig_history.txt"))
    csv = ledger.file(FIG_CSV, fixture("fig_statement.csv"))

    out = ledger.derive()

    # The CSV prints a balance, so it outranks the unverified history: its 11 lines
    # are txns and every history line, the fee lines and both conversion legs among
    # them, is their second source.
    assert (out.lines.from_documents, out.lines.additional_sources) == (11, 11)
    assert out.lines.discrepancies == 0
    per_txn = sources_by_txn(ledger)
    assert set(per_txn.values()) == {2}
    assert all(documents_of(ledger, each) == {pdf, csv} for each in per_txn)
    assert {row["basis"] for row in ledger.txns()} == {"verified_export"}


def test_a_second_copy_of_a_csv_adds_nothing(ledger: Ledger) -> None:
    register(ledger, "hazel", "Hazel", "payment_processor", "AB12CD34")
    text = fixture("hazel_activity.csv")
    ledger.file(HAZEL_CSV, text)
    ledger.derive()
    before = (count(ledger, document), count(ledger, txn), count(ledger, txn_source))

    ledger.file("hazel/activity-2046-04-30-again.csv", text)
    out = ledger.derive()

    assert before == (1, 7, 7)
    assert (count(ledger, document), count(ledger, txn), count(ledger, txn_source)) == (
        before
    )
    assert out.documents == Counter({"parsed": 1})


def test_a_resaved_copy_with_other_line_endings_adds_nothing(ledger: Ledger) -> None:
    """Different bytes, the same rows: the copy is superseded and adds no txn and no
    source."""
    register(ledger, "fig", "Fig", "wallet", "W-0001-ZZ")
    text = fixture("fig_statement.csv")
    ledger.file(FIG_CSV, text)
    ledger.derive()
    before = (count(ledger, txn), count(ledger, txn_source))

    crlf = text.replace("\n", "\r\n")
    assert crlf != text
    ledger.file("fig/statement-2045-08-28-resaved.csv", crlf)
    out = ledger.derive()

    assert before == (11, 11)
    assert (count(ledger, txn), count(ledger, txn_source)) == before
    assert out.documents == Counter({"parsed": 1, "superseded": 1})


@pytest.mark.parametrize(
    ("copy", "outcomes"),
    [
        (lambda text: text, Counter({"parsed": 1})),
        (
            lambda text: text.replace("\n", "\r\n"),
            Counter({"parsed": 1, "superseded": 1}),
        ),
    ],
    ids=["same_bytes", "crlf_resave"],
)
def test_a_second_juniper_copy_adds_nothing(
    ledger: Ledger, copy: Callable[[str], str], outcomes: Counter[str]
) -> None:
    """The Juniper export's control is ``none``: a copy of it, byte for byte or saved
    again with other line endings, adds no txn and no source."""
    register(ledger, "juniper", "Juniper", "payment_processor", "SAMPLE999JUNIPER")
    text = fixture("juniper_balance.csv")
    ledger.file(JUNIPER_CSV, text)
    ledger.derive()
    before = (count(ledger, txn), count(ledger, txn_source))

    ledger.file("juniper/balance-2045-08-27-again.csv", copy(text))
    out = ledger.derive()

    assert before == (6, 6)
    assert (count(ledger, txn), count(ledger, txn_source)) == before
    assert out.documents == outcomes


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ranked(partial: str, fuller: str, partial_first: bool) -> tuple[str, str]:
    """The two exports, saved with line endings that put the partial one first in
    precedence (the larger sha256 ranks first among equal CSVs) or the fuller one."""
    for a in (partial, partial.replace("\n", "\r\n")):
        for b in (fuller, fuller.replace("\n", "\r\n")):
            if (_sha(a) > _sha(b)) == partial_first:
                return a, b
    raise AssertionError("no line endings give that precedence")


@pytest.mark.parametrize("partial_ranks_first", [True, False])
@pytest.mark.parametrize("partial_filed_first", [True, False])
def test_a_partial_juniper_export_and_a_fuller_one_count_each_payout_once(
    ledger: Ledger, partial_filed_first: bool, partial_ranks_first: bool
) -> None:
    """An export cut inside a payout gives that payout's line alone; a fuller export
    filed before or after it gives the payout with its gross and fee. Whichever
    ranks first and whichever is filed first, the lone payout line pairs one to one
    with the fuller export's payout line, the shared payout's three lines are one txn
    each with two sources, and nothing is counted twice."""
    register(ledger, "juniper", "Juniper", "payment_processor", "SAMPLE999JUNIPER")
    full = fixture("juniper_balance.csv")
    cut = [
        row
        for row in fixture_rows("juniper_balance.csv")
        if row["Jn Day"] != "2045-08-14"
    ]
    partial_text, full_text = _ranked(
        csv_text("juniper", cut), full, partial_ranks_first
    )
    partial_path, full_path = "juniper/partial.csv", "juniper/balance-2045-08-27.csv"
    filings = [(partial_path, partial_text), (full_path, full_text)]
    if not partial_filed_first:
        filings.reverse()
    docs: dict[str, uuid.UUID] = {}
    for relative, text in filings:
        docs[relative] = ledger.file(relative, text)
        out = ledger.derive()
    partial, fuller = docs[partial_path], docs[full_path]

    assert out.documents == Counter({"parsed": 2})
    # The fuller export's 6 lines are the txns; the partial one's 4 (the first
    # payout's three and the second payout's line alone) are their second sources.
    assert (out.lines.from_documents, out.lines.additional_sources) == (6, 4)
    assert out.lines.discrepancies == 0
    assert count(ledger, txn) == 6
    per_txn = sources_by_txn(ledger)
    assert sorted(per_txn.values()) == [1, 1, 2, 2, 2, 2]
    by_amount = {
        cast(Decimal, row["amount"]): cast(uuid.UUID, row["id"])
        for row in ledger.txns()
    }
    assert len(by_amount) == 6
    assert documents_of(ledger, by_amount[Decimal("-257.05")]) == {partial, fuller}
    # The second payout's gross and fee come from the fuller export alone.
    for amount in (Decimal("264.54"), Decimal("-7.49")):
        assert documents_of(ledger, by_amount[amount]) == {fuller}
    # Each payout's gross less its fees is its payout: the account nets to zero.
    assert sum(by_amount, Decimal(0)) == 0


def test_coverage_replays_a_row_with_a_fee_as_one_step(ledger: Ledger) -> None:
    """Only a row's last line stores a balance; coverage adds the row's other lines
    to it, so the fee rows continue the run and no month reads as a break."""
    register(ledger, "fig", "Fig", "wallet", "W-0001-ZZ")
    ledger.file(FIG_CSV, fixture("fig_statement.csv"))

    ledger.derive()

    months = dict(
        ledger.conn.execute(
            select(coverage.c.month, coverage.c.reason_code).where(
                coverage.c.account_id == account_id("fig")
            )
        ).all()
    )
    assert D(2045, 8, 1) in months
    assert "running_balance_break" not in months.values()


def test_coverage_breaks_where_a_fee_row_is_missing_and_only_there(
    ledger: Ledger,
) -> None:
    """The break side of the replay. The oldest fee row moves to July and a later
    fee row is removed, so the run continues across the month boundary (a row's
    principal carried onto its fee line) and breaks inside August only. Dropping the
    carried amount, or never resetting it, would break July too."""
    register(ledger, "fig", "Fig", "wallet", "W-0001-ZZ")
    rows = fixture_rows("fig_statement.csv")
    (oldest,) = [row for row in rows if row["Fig Day"] == "2045/08/08"]
    oldest["Fig Day"] = "2045/07/08"
    kept = [row for row in rows if row["Fig Fee"] != "0.10"]
    assert len(kept) == len(rows) - 1
    ledger.file(FIG_CSV, csv_text("fig", kept))

    ledger.derive()

    months = {
        row.month: (row.state, row.reason_code)
        for row in ledger.conn.execute(
            select(coverage.c.month, coverage.c.state, coverage.c.reason_code).where(
                coverage.c.account_id == account_id("fig")
            )
        )
    }
    assert months == {
        D(2045, 7, 1): ("partial", None),
        D(2045, 8, 1): ("partial", "running_balance_break"),
    }


def test_an_overlapping_second_export_shares_its_lines(ledger: Ledger) -> None:
    """A later Fig export that drops the oldest rows and adds a new one: the shared
    rows are one txn with two sources, and only the new row is a txn of its own."""
    register(ledger, "fig", "Fig", "wallet", "W-0001-ZZ")
    newest = {
        "Fig Day": "2045/08/28", "Fig Kind": "FG-A", "Fig Unit": "USD",
        "Fig Text": "OUT Sample Payee Three", "Fig Amount": "-22.32",
        "Fig Fee": "0.00", "Fig Left": "738.21", "Fig Party": "Sample Payee Three",
    }  # fmt: skip
    first = ledger.file(FIG_CSV, fixture("fig_statement.csv"))
    # Rows oldest first: the later export starts after the two oldest rows.
    later_text = csv_text("fig", [*fixture_rows("fig_statement.csv")[2:], newest])
    later = ledger.file("fig/statement-2045-08-28-later.csv", later_text)

    out = ledger.derive()

    assert out.documents == Counter({"parsed": 2})
    # 11 lines in the first file, 10 in the later one: 9 shared, the later file's
    # new row its own txn, the first file's oldest transfer and its fee on their own.
    assert (out.lines.from_documents, out.lines.additional_sources) == (12, 9)
    assert out.lines.discrepancies == 0
    shared = [each for each, n in sources_by_txn(ledger).items() if n == 2]
    assert len(shared) == 9
    assert all(documents_of(ledger, each) == {first, later} for each in shared)
