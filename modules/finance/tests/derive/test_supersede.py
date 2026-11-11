"""Stage 1's ``superseded``: of two documents that rank equally on an account for
exactly the same period, the larger sha256 stays primary (SPEC "Derivation" stage 1).

Seam: ``derive.run`` over two copies of one document whose bytes differ, read back
through ``document`` and the txn tables. Every value is synthetic.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import func, select

from rheo_finance.storage.schema import document, txn, txn_source

from .support import (
    CARD_IDENTIFIER,
    FIG_CARD_MOVES,
    FIXTURES,
    Ledger,
    card_fixture,
)

CSV_FIXTURE = (FIXTURES / "alder_chequing_jump.csv").read_text(encoding="utf-8")
STATEMENT = (FIXTURES / "alder_statement_split.txt").read_text(encoding="utf-8")


def statuses(ledger: Ledger) -> dict[str, str]:
    return {
        row.sha256: row.status
        for row in ledger.conn.execute(select(document.c.sha256, document.c.status))
    }


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def counts(ledger: Ledger) -> tuple[int, int]:
    return (
        int(ledger.conn.scalar(select(func.count()).select_from(txn)) or 0),
        int(ledger.conn.scalar(select(func.count()).select_from(txn_source)) or 0),
    )


def test_two_copies_of_an_export_keep_the_larger_sha256(ledger: Ledger) -> None:
    ledger.chequing()
    copies = [CSV_FIXTURE, CSV_FIXTURE + "\n"]
    for number, text in enumerate(copies):
        ledger.file(f"alder/export-{number}.csv", text)

    out = ledger.derive()

    assert out.documents == {"parsed": 1, "superseded": 1}
    larger, smaller = sorted((sha(each) for each in copies), reverse=True)
    assert statuses(ledger) == {larger: "parsed", smaller: "superseded"}
    # Only the primary copy's lines are read: no additional source either.
    assert counts(ledger) == (6, 6)


def test_two_copies_of_a_statement_keep_the_larger_sha256(ledger: Ledger) -> None:
    ledger.chequing()
    copies = [STATEMENT, STATEMENT + "\n\n"]
    for number, text in enumerate(copies):
        ledger.file(f"alder/statement-{number}.pdf", text)

    out = ledger.derive()

    assert out.documents == {"parsed": 1, "superseded": 1}
    larger, smaller = sorted((sha(each) for each in copies), reverse=True)
    assert statuses(ledger) == {larger: "parsed", smaller: "superseded"}
    assert counts(ledger) == (3, 3)


def test_documents_of_different_rank_or_period_are_not_superseded(
    ledger: Ledger,
) -> None:
    ledger.chequing()
    rows = CSV_FIXTURE.splitlines()
    ledger.file("alder/whole.csv", CSV_FIXTURE)
    # The same export less its oldest row: a shorter period.
    ledger.file("alder/shorter.csv", "\n".join(rows[:-1]) + "\n")
    ledger.file("alder/2055-06-12.pdf", STATEMENT)

    out = ledger.derive()

    assert out.documents == {"parsed": 3}
    assert set(statuses(ledger).values()) == {"parsed"}


def test_a_superseded_copy_is_parsed_again_when_its_twin_leaves(
    ledger: Ledger,
) -> None:
    ledger.chequing()
    copies = [CSV_FIXTURE, CSV_FIXTURE + "\n"]
    paths = [f"alder/export-{number}.csv" for number in range(2)]
    for path, text in zip(paths, copies, strict=True):
        ledger.file(path, text)
    ledger.derive()
    larger = max(copies, key=sha)

    # The primary copy's file leaves the archive: it is unparsed on the next derive,
    # and the other copy is read instead.
    (ledger.archive / paths[copies.index(larger)]).unlink()
    out = ledger.derive()

    assert out.documents == {"parsed": 1, "unparsed": 1}
    assert counts(ledger) == (6, 6)


def test_an_export_and_a_download_of_one_period_are_not_superseded(
    ledger: Ledger, card_parsers: None
) -> None:
    # Equal control (both unverified) and the same period, but different formats: a
    # transaction download outranks a CSV, so neither copy is superseded.
    ledger.card()
    text = card_fixture("card_2039_export.csv")
    ledger.file("synthetic-card/2039-10-28-export.csv", text)
    ledger.file("synthetic-card/2039-10-28-download.pdf", text + "\n")

    out = ledger.derive()

    assert out.documents == {"parsed": 2}


def test_exports_of_one_period_with_different_lines_are_not_superseded(
    ledger: Ledger, card_parsers: None
) -> None:
    # Two exports with the same first and last dates, so the same period and rank, that
    # differ in between: neither is a copy of the other, and every line stays.
    ledger.card()
    shared = (
        "2048-08-07 - -25.25 SAMPLE BOOKSHOP 99951",
        "2048-08-23 - -60.60 SAMPLE FUEL 99952",
    )
    for name, middle in (("a", "2048-08-13 - -5.74 SAMPLE CAFE 99953"),
                         ("b", "2048-08-15 - -8.08 SAMPLE KIOSK 99954")):  # fmt: skip
        ledger.file(
            f"synthetic-card/2048-08-23-{name}.csv",
            "\n".join(
                [FIG_CARD_MOVES, f"ACCOUNT {CARD_IDENTIFIER}"]
                + [f"LINE {line}" for line in (shared[0], middle, shared[1])]
            )
            + "\n",
        )

    out = ledger.derive()

    assert out.documents == {"parsed": 2}
    assert (out.lines.txns, out.lines.additional_sources) == (4, 2)


def test_two_bills_of_one_month_with_different_amounts_are_not_superseded(
    ledger: Ledger,
) -> None:
    # A bill's money is on its period (the amount due), not on lines. Two bills of one
    # month that print different amounts due are not copies of each other.
    ledger.accounts(("mobile", "yew line", "bill_vendor", "999000123"))
    bill = (FIXTURES / "yew_bill.txt").read_text(encoding="utf-8")
    unpaid = "   Yew prior left   $38.76\n"
    total = "Yew due sum   Yew settle by Jul 19, 2049   $101.40"
    assert bill.count(unpaid) == 1 and bill.count(total) == 1
    printed = bill.replace(unpaid, "")  # no identity: loads as printed
    ledger.file("yew/2049-06-28.pdf", printed)
    ledger.file(
        "yew/2049-06-28-reissued.pdf",
        printed.replace(total, total.replace("101.40", "105.10")),
    )

    out = ledger.derive()

    assert out.documents == {"parsed": 2}
