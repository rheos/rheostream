"""The Olive statement (two layout versions), the Fig
transaction history and the Hazel activity statement through ``parse_document``
(FR-2 ``official``, FR-3, FR-8, FR-15(a), FR-15(c); AC-8, AC-58 and AC-85 inputs).

Seam: ``parse_document`` over a synthetic document filed into a temporary archive,
against accounts registered through the manual files. Each control kind ties out
its own way: the OLIVE statement is ``summary`` per account (a one-cent change
rejects it), Hazel is ``summary`` per currency, and the Fig history fixture has no
balance, so it is ``none`` and ``official=false``. An OLIVE e-Send debit, its
cancellation and the debit again give a reversal hint on the credit only. Every
value is invented; the fixtures are layout text, read as text where the real run
calls pdftotext.
"""

from __future__ import annotations

import dataclasses
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
from rheo_finance.parsers.base import NotRecognised
from rheo_finance.parsers.registry import Parsed, parse_document, parsers_for
from rheo_finance.storage.schema import (
    document,
    document_line,
    manual_entry_error,
    statement_period,
)

from .conftest import ManualAccount, register_accounts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
NOW = dt.datetime(2055, 5, 8, 12, 0, tzinfo=dt.UTC)
D = dt.date


OLIVE_NAME = "Olive"
CHEQUING = ManualAccount("olive-chequing", OLIVE_NAME, "chequing", ("999000000101",))
NEST = ManualAccount("olive-nest", OLIVE_NAME, "savings", ("999000000102",))
FIG = ManualAccount("fig", "Fig", "wallet", ("W-0001-ZZ",), currencies="CAD;USD")
HAZEL = ManualAccount(
    "hazel", "Hazel", "payment_processor", ("AB12CD34",), currencies="CAD;USD"
)


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

    def register(self, *accounts: ManualAccount) -> None:
        register_accounts(self.uow, self.data, *accounts)

    def file(self, relative: str, text: str) -> uuid.UUID:
        target = self.archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        target.write_bytes(data)
        file_archive(self.uow, self.archive, self.data, NOW)
        return document_id(hashlib.sha256(data).hexdigest())

    def parse(self, doc: uuid.UUID) -> Parsed:
        return parse_document(self.uow, doc, self.archive, extract_pdf=read_as_text)

    def document(self, doc: uuid.UUID) -> Any:
        return self.conn.execute(select(document).where(document.c.id == doc)).one()

    def periods(self, doc: uuid.UUID) -> list[Any]:
        return list(
            self.conn.execute(
                select(statement_period)
                .where(statement_period.c.document_id == doc)
                .order_by(statement_period.c.account_id, statement_period.c.currency)
            ).all()
        )

    def lines(self, doc: uuid.UUID) -> list[Any]:
        return list(
            self.conn.execute(
                select(document_line)
                .where(document_line.c.document_id == doc)
                .order_by(document_line.c.line_no)
            ).all()
        )

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


def hints(line: Any) -> tuple[bool, bool, bool, bool, str | None]:
    return (
        line.transfer_hint,
        line.reversal_hint,
        line.conversion_hint,
        line.etransfer_hint,
        line.hint_account_slug,
    )


NO_HINT = (False, False, False, False, None)


# --- Olive ---------------------------------------------------

CURRENT = "olive/2054-10-30.pdf"
OLDER_AUGUST = "olive/2039-10-15.pdf"
CURRENT_TEXT = "olive_statement_current.txt"


def test_the_current_layout_ties_out_per_account(setup: Setup) -> None:
    setup.register(CHEQUING, NEST)
    doc = setup.file(CURRENT, fixture(CURRENT_TEXT))

    assert setup.parse(doc) == Parsed("parsed", None, periods=2, lines=7)
    row = setup.document(doc)
    assert (row.parser_id, row.parser_version, row.official) == (
        "olive.statement", "1", True,
    )  # fmt: skip
    by_account = {period.account_id: period for period in setup.periods(doc)}
    chequing = by_account[account_id("olive-chequing")]
    assert (chequing.control_kind, chequing.tied_out, chequing.currency) == (
        "summary", True, "CAD",
    )  # fmt: skip
    assert (chequing.period_start, chequing.period_end) == (
        D(2054, 10, 1), D(2054, 10, 30),
    )  # fmt: skip
    assert (chequing.opening, chequing.closing) == (
        Decimal("12744.09"), Decimal("6397.90"),
    )  # fmt: skip
    assert (chequing.total_debits, chequing.total_credits) == (
        Decimal("13197.91"), Decimal("6851.72"),
    )  # fmt: skip
    nest = by_account[account_id("olive-nest")]
    assert (nest.opening, nest.closing, nest.tied_out) == (
        Decimal("44.85"), Decimal("44.85"), True,
    )  # fmt: skip


def test_a_cancelled_debit_gives_a_reversal_hint_on_the_credit_only(
    setup: Setup,
) -> None:
    """An e-Send debit, the bank's reversal back in (no payee printed), and the
    same debit again: one reversal hint, on the credit."""
    setup.register(CHEQUING, NEST)
    doc = setup.file(CURRENT, fixture(CURRENT_TEXT))
    setup.parse(doc)

    lines = setup.lines(doc)
    shape = [
        (line.posting_date, line.amount, line.reversal_hint, line.counterparty)
        for line in lines[:3]
    ]
    assert shape == [
        (D(2054, 10, 4), Decimal("-6588.49"), False, "sample payee"),
        (D(2054, 10, 9), Decimal("6588.49"), True, None),
        (D(2054, 10, 10), Decimal("-6588.49"), False, "sample payee"),
    ]
    assert [line.reversal_hint for line in lines].count(True) == 1
    assert all(line.etransfer_hint for line in lines[:4])
    # A payer's name wraps onto the next line; the counterparty holds all of it.
    assert (lines[3].amount, lines[3].counterparty) == (
        Decimal("259.10"), "sample payer",
    )  # fmt: skip
    assert lines[3].description_raw.endswith("came from: SAMPLE PAYER")
    # The fees and the interest carry no transfer, reversal or e-Transfer hint.
    assert [hints(line) for line in lines[4:]] == [NO_HINT] * 3
    assert [line.fee_type for line in lines[4:]] == ["service_charge", "fee", None]


def test_the_older_layout_is_its_own_version(setup: Setup) -> None:
    setup.register(CHEQUING, NEST)
    doc = setup.file(OLDER_AUGUST, fixture("olive_statement_older.txt"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=2, lines=5)
    row = setup.document(doc)
    assert (row.parser_id, row.parser_version) == (
        "olive.statement_older", "2",
    )  # fmt: skip
    lines = setup.lines(doc)
    assert [line.amount for line in lines] == [
        Decimal(each) for each in ("598.00", "-2.99", "-897.00", "-11.96", "0.59")
    ]
    assert lines[0].description_raw == "Olive Cash In SAMPLE BRANCH ANYTOWN"
    assert (lines[2].etransfer_hint, lines[2].counterparty) == (True, "sample payee")
    chequing = next(
        period for period in setup.periods(doc)
        if period.account_id == account_id("olive-chequing")
    )  # fmt: skip
    assert (chequing.period_start, chequing.period_end, chequing.tied_out) == (
        D(2039, 9, 15), D(2039, 10, 15), True,
    )  # fmt: skip


def test_each_version_reads_only_its_own_layout() -> None:
    by_version = {p.version: p for p in parsers_for("olive", "pdf")}
    current, older = by_version["1"].filing_date, by_version["2"].filing_date
    assert current is not None and older is not None
    assert current(fixture(CURRENT_TEXT)) == D(2054, 10, 30)
    assert older(fixture("olive_statement_older.txt")) == D(2039, 10, 15)
    with pytest.raises(NotRecognised):
        current(fixture("olive_statement_older.txt"))
    with pytest.raises(NotRecognised):
        older(fixture(CURRENT_TEXT))


def test_one_cent_off_the_totals_rejects_the_statement(setup: Setup) -> None:
    setup.register(CHEQUING, NEST)
    text = fixture(CURRENT_TEXT)
    assert text.count("$6,851.72") == 1
    doc = setup.file(CURRENT, text.replace("$6,851.72", "$6,851.73"))

    assert setup.parse(doc) == Parsed("rejected", "summary_mismatch")
    assert setup.lines(doc) == []


def test_a_balance_the_lines_do_not_reach_rejects_the_statement(setup: Setup) -> None:
    setup.register(CHEQUING, NEST)
    text = fixture(CURRENT_TEXT)
    broken = "6,588.49   12,744.09"
    assert text.count(broken) == 1
    doc = setup.file(CURRENT, text.replace(broken, "6,588.49   12,744.10"))

    assert setup.parse(doc) == Parsed("rejected", "checks_failed")


def test_an_unregistered_account_number_rejects_the_statement(setup: Setup) -> None:
    setup.register(CHEQUING)  # not the second savings account
    doc = setup.file(CURRENT, fixture(CURRENT_TEXT))

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")


def test_two_section_numbers_ending_alike_load_and_resolve_each_section(
    setup: Setup,
) -> None:
    """Two sections whose full numbers end in the same four digits. Both are full
    numbers, so the loader keeps both and each section resolves to its own account by
    its whole number."""
    nest_number = "999000010101"
    assert nest_number[-4:] == CHEQUING.identifiers[0][-4:]
    text = fixture(CURRENT_TEXT)
    assert text.count(NEST.identifiers[0]) == 1
    text = text.replace(NEST.identifiers[0], nest_number)
    setup.register(CHEQUING, dataclasses.replace(NEST, identifiers=(nest_number,)))
    assert setup.conn.scalar(select(func.count()).select_from(manual_entry_error)) == 0
    doc = setup.file(CURRENT, text)

    assert setup.parse(doc) == Parsed("parsed", None, periods=2, lines=7)
    by_account = {period.account_id: period for period in setup.periods(doc)}
    assert set(by_account) == {account_id("olive-chequing"), account_id("olive-nest")}
    assert (by_account[account_id("olive-nest")].opening,) == (Decimal("44.85"),)
    assert (by_account[account_id("olive-chequing")].opening,) == (Decimal("12744.09"),)


@pytest.mark.parametrize(
    "footer",
    ["   www.example.test", "   Page   1 of 2", "   Sample Branch 100 Sample St."],
)
def test_footer_text_after_a_transaction_never_joins_its_description(
    setup: Setup, footer: str
) -> None:
    """Footer or header text right after a transaction line, with no continued
    marker or blank line between, is a layout the parser has not seen: the
    statement is unparsed, and no description takes the text."""
    setup.register(CHEQUING, NEST)
    text = fixture(CURRENT_TEXT)
    last = "28 Oct 54   Olive Interest Paid   4.13   6,397.90\n"
    assert text.count(last) == 1
    doc = setup.file(CURRENT, text.replace(last, last + footer + "\n"))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")
    assert setup.lines(doc) == []


@pytest.mark.parametrize("footer", ["   Page 1 of 2", "   www.example.test"])
def test_a_wrap_that_is_not_name_shaped_is_unparsed_even_before_the_marker(
    setup: Setup, footer: str
) -> None:
    """A page number or a web address between a wrapped name and the continued
    marker is in the right place for a wrap but not its shape: the statement is
    unparsed rather than the text joining the description."""
    setup.register(CHEQUING, NEST)
    text = fixture(CURRENT_TEXT)
    wrap = "   PAYER\n   ~ page turns ~\n"
    assert text.count(wrap) == 1
    doc = setup.file(
        CURRENT, text.replace(wrap, f"   PAYER\n{footer}\n   ~ page turns ~\n")
    )

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")
    assert setup.lines(doc) == []


def test_a_wrapped_name_still_joins_before_the_next_line(setup: Setup) -> None:
    setup.register(CHEQUING, NEST)
    doc = setup.file(OLDER_AUGUST, fixture("olive_statement_older.txt"))
    setup.parse(doc)

    assert setup.lines(doc)[0].description_raw == "Olive Cash In SAMPLE BRANCH ANYTOWN"


@pytest.mark.parametrize(
    "heading", ["USD OLIVE EVERYDAY", "OLIVE LENDING", "OLIVE FIXED SAVINGS"]
)
def test_an_unknown_section_heading_is_not_read_as_the_section_before(
    setup: Setup, heading: str
) -> None:
    """The second savings section's heading replaced by another: its account must not
    take the chequing kind, and a USD section is never read as CAD."""
    setup.register(CHEQUING, NEST)
    text = fixture(CURRENT_TEXT)
    assert text.count("\nOLIVE NEST\n") == 1
    doc = setup.file(CURRENT, text.replace("\nOLIVE NEST\n", f"\n{heading}\n"))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_an_unknown_heading_under_a_known_one_is_not_recognised(setup: Setup) -> None:
    """An unknown capitals heading between a known heading and its account line: the
    account keeps a heading of its own above it, so only the unknown heading itself
    can make the statement unparsed."""
    setup.register(CHEQUING, NEST)
    text = fixture(CURRENT_TEXT)
    assert text.count("\nOLIVE NEST\n") == 1
    doc = setup.file(
        CURRENT,
        text.replace("\nOLIVE NEST\n", "\nOLIVE NEST\nOLIVE FIXED SAVINGS\n"),
    )

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_an_account_line_without_its_own_heading_is_not_recognised(
    setup: Setup,
) -> None:
    setup.register(CHEQUING, NEST)
    text = fixture(CURRENT_TEXT)
    assert text.count("\nOLIVE NEST\n") == 1
    doc = setup.file(CURRENT, text.replace("\nOLIVE NEST\n", "\n"))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_the_account_numbers_and_the_members_name_are_stored_nowhere(
    setup: Setup,
) -> None:
    setup.register(CHEQUING, NEST)
    doc = setup.file(CURRENT, fixture(CURRENT_TEXT))
    setup.parse(doc)

    stored = setup.stored_text(doc)
    for value in ("999000000101", "999000000102", "99900001", "sample a holder"):
        assert value not in stored


# --- Fig -----------------------------------------------------------------------------

FIG_HISTORY = "fig/2045-08-history.pdf"


def test_the_fig_history_is_unofficial_and_unverified_per_currency(
    setup: Setup,
) -> None:
    setup.register(FIG)
    doc = setup.file(FIG_HISTORY, fixture("fig_history.txt"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=2, lines=11)
    row = setup.document(doc)
    assert (row.parser_id, row.official) == ("fig.history", False)
    periods = setup.periods(doc)
    assert [(p.currency, p.control_kind, p.tied_out) for p in periods] == [
        ("CAD", "none", False), ("USD", "none", False),
    ]  # fmt: skip
    assert {(p.period_start, p.period_end) for p in periods} == {
        (D(2045, 8, 1), D(2045, 8, 28))
    }


def test_the_fig_lines_read_side_fee_and_currency_from_the_layout(
    setup: Setup,
) -> None:
    setup.register(FIG)
    doc = setup.file(FIG_HISTORY, fixture("fig_history.txt"))
    setup.parse(doc)

    got = [
        (line.posting_date, line.amount, line.currency, line.fee_type,
         line.description_raw)
        for line in setup.lines(doc)
    ]  # fmt: skip
    assert got == [
        # A fee larger than what arrived keeps its role: read by its cell.
        (D(2045, 8, 8), Decimal("-1.00"), "CAD", None, "Pushed » Sample Payee Two"),
        (D(2045, 8, 8), Decimal("-4.30"), "CAD", "fee",
         "Pushed » Sample Payee Two (fee)"),
        # Sent with a fee: what arrived, and the fee on its own line.
        (D(2045, 8, 10), Decimal("-503.61"), "CAD", None, "Pushed » Sample Payee"),
        (D(2045, 8, 10), Decimal("-0.40"), "CAD", "fee", "Pushed » Sample Payee (fee)"),
        # A card paid in USD from the CAD balance, its conversion fee apart.
        (D(2045, 8, 12), Decimal("-83.99"), "CAD", None, "Sample Web Shop"),
        (D(2045, 8, 12), Decimal("-0.10"), "CAD", "conversion_fee",
         "Sample Web Shop (fee)"),
        (D(2045, 8, 15), Decimal("-15.15"), "CAD", None, "Sample Grocer"),
        # A conversion: out-leg, in-leg, and the printed fee as the only fee line.
        (D(2045, 8, 20), Decimal("-504.01"), "USD", None, "Swapped » CAD bought"),
        (D(2045, 8, 20), Decimal("687.54"), "CAD", None, "Swapped » CAD bought"),
        (D(2045, 8, 20), Decimal("-0.53"), "USD", "conversion_fee",
         "Swapped » CAD bought (fee)"),
        (D(2045, 8, 27), Decimal("257.05"), "USD", None, "JUNIPER PAYMENTS"),
    ]  # fmt: skip
    card = setup.lines(doc)[4]
    assert (card.original_amount, card.original_currency) == (Decimal("-4.00"), "USD")


def test_a_fig_fee_and_amount_on_separate_lines_must_hold_a_zero(
    setup: Setup,
) -> None:
    """With no line holding two amounts, only a zero fee can be told apart."""
    setup.register(FIG)
    text = fixture("fig_history.txt")
    shared = "Pushed » Sample Payee Two   4.30 CAD   1.00 CAD\n"
    assert text.count(shared) == 1
    split = "Pushed » Sample Payee Two   4.30 CAD\n   1.00 CAD\n"
    doc = setup.file(FIG_HISTORY, text.replace(shared, split))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_the_fig_hints_mark_the_conversion_legs_and_the_juniper_payout(
    setup: Setup,
) -> None:
    setup.register(FIG)
    doc = setup.file(FIG_HISTORY, fixture("fig_history.txt"))
    setup.parse(doc)

    lines = setup.lines(doc)
    assert [hints(line) for line in lines] == [
        NO_HINT, NO_HINT, NO_HINT, NO_HINT, NO_HINT, NO_HINT, NO_HINT,
        (False, False, True, False, None),
        (False, False, True, False, None),
        NO_HINT,  # the fee line is never taken for a conversion leg
        (True, False, False, False, "juniper"),
    ]  # fmt: skip
    counterparties = [line.counterparty for line in lines]
    assert counterparties == [
        "Sample Payee Two", None, "Sample Payee", None, None, None, None, None,
        None, None, "JUNIPER PAYMENTS",
    ]  # fmt: skip


def test_the_fig_wallet_number_is_stored_nowhere(setup: Setup) -> None:
    setup.register(FIG)
    doc = setup.file(FIG_HISTORY, fixture("fig_history.txt"))
    setup.parse(doc)

    stored = setup.stored_text(doc)
    assert (
        "w-0001-zz" not in stored
        and "w0001zz" not in stored
        and "sample holder" not in stored
    )


def test_a_fig_letter_that_is_not_a_history_is_unparsed(setup: Setup) -> None:
    setup.register(FIG)
    letter = "Fig sample letter\n\nA note about your wallet\n\nWallet key\n"
    doc = setup.file("fig/account-details.pdf", letter)

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


# --- Hazel ---------------------------------------------------------------------------

HAZEL_STATEMENT = "hazel/2030-11-report.pdf"


def test_hazel_gives_one_tied_out_summary_period_per_currency(setup: Setup) -> None:
    setup.register(HAZEL)
    doc = setup.file(HAZEL_STATEMENT, fixture("hazel_activity.txt"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=2, lines=6)
    periods = setup.periods(doc)
    assert [
        (p.currency, p.control_kind, p.tied_out, p.opening, p.closing) for p in periods
    ] == [
        ("CAD", "summary", True, Decimal("11.16"), Decimal("0.00")),
        ("USD", "summary", True, Decimal("5.58"), Decimal("21.89")),
    ]
    assert {(p.period_start, p.period_end) for p in periods} == {
        (D(2030, 11, 2), D(2030, 11, 29))
    }


def test_hazel_lines_carry_the_bank_pull_and_conversion_hints(setup: Setup) -> None:
    setup.register(HAZEL)
    doc = setup.file(HAZEL_STATEMENT, fixture("hazel_activity.txt"))
    setup.parse(doc)

    got = [
        (line.posting_date, line.currency, line.amount, hints(line),
         line.counterparty, line.fee_type)
        for line in setup.lines(doc)
    ]  # fmt: skip
    conversion = (False, False, True, False, None)
    assert got == [
        (D(2030, 11, 3), "CAD", Decimal("33.48"),
         (True, False, False, False, None), None, None),
        (D(2030, 11, 3), "CAD", Decimal("-44.64"), conversion, None, None),
        (D(2030, 11, 3), "USD", Decimal("32.13"), conversion, None, None),
        (D(2030, 11, 4), "USD", Decimal("-37.71"), NO_HINT, "Sample Streaming Co",
         None),
        (D(2030, 11, 20), "USD", Decimal("22.32"), NO_HINT, "Sample Client Inc.",
         None),
        (D(2030, 11, 20), "USD", Decimal("-0.43"), NO_HINT, None, "fee"),
    ]  # fmt: skip
    pull = setup.lines(doc)[3]
    assert pull.description_raw == ("HZ PLAN Sample Streaming Co")


def test_hazel_one_cent_off_the_closing_rejects_the_statement(setup: Setup) -> None:
    setup.register(HAZEL)
    text = fixture("hazel_activity.txt")
    closing = " USD balances   5.58   21.89"
    assert text.count(closing) == 1
    doc = setup.file(
        HAZEL_STATEMENT, text.replace(closing, " USD balances   5.58   21.90")
    )

    assert setup.parse(doc) == Parsed("rejected", "summary_mismatch")


def test_an_account_id_is_matched_on_its_letters_too(setup: Setup) -> None:
    """An ID with the same digits and other letters is another account."""
    setup.register(dataclasses.replace(HAZEL, identifiers=("XY12ZW34",)))
    doc = setup.file(HAZEL_STATEMENT, fixture("hazel_activity.txt"))
    assert setup.parse(doc) == Parsed("rejected", "unknown_account")

    setup.register(dataclasses.replace(HAZEL, identifiers=("ab-12cd-34",)))
    assert setup.parse(doc).status == "parsed"


def test_hazel_ids_and_emails_are_stored_nowhere(setup: Setup) -> None:
    setup.register(HAZEL)
    doc = setup.file(HAZEL_STATEMENT, fixture("hazel_activity.txt"))
    setup.parse(doc)

    stored = setup.stored_text(doc)
    for value in ("ab12cd34", "@", "40001"):
        assert value not in stored


# --- registration ---------------------------------------------------------------------


def test_the_new_parsers_register_as_hint_setting() -> None:
    registered = {
        parser.parser_id: (parser.version, parser.sets_hints)
        for issuer in ("olive", "fig", "hazel")
        for parser in parsers_for(issuer, "pdf")
    }
    assert registered == {
        "olive.statement": ("1", True),
        "olive.statement_older": ("2", True),
        "fig.history": ("1", True),
        "hazel.activity": ("1", True),
    }
