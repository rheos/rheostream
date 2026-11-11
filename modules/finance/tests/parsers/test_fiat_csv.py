"""The Hazel activity CSV, the Fig statement CSV and the Juniper balance history CSV
through ``parse_document`` (FR-12(a), FR-2, FR-3, FR-15(c); AC-80 parse, control and
unparsed clauses; edge case 22).

Seam: ``parse_document`` over a synthetic export filed into a temporary archive,
against accounts registered through the manual files. The Hazel and Fig layouts
carry a balance, so each is ``running_balance`` per currency: continuous balances tie
a period out and a break is a gap. The Juniper layout has none, so it is ``none``,
and each payout gives its gross, its fees and the payout debit, which carries the
Fig hint. A header that does not match is ``unparsed`` with no line. The conversion
legs carry ``conversion_hint``, and a fee is its own line. Every value, column and
layout is invented.
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
from sqlalchemy import Connection, select

from rheo_finance.context import UnitOfWork
from rheo_finance.documents.registry import document_id, file_archive
from rheo_finance.manual.stage2 import account_id
from rheo_finance.parsers.registry import Parsed, parse_document, parsers_for
from rheo_finance.storage.schema import document, document_line, statement_period

from ..csv_layouts import csv_text, fixture_rows
from .conftest import ManualAccount, register_accounts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
NOW = dt.datetime(2051, 5, 11, 12, 0, tzinfo=dt.UTC)
D = dt.date
HAZEL = ManualAccount(
    "hazel", "Hazel", "payment_processor", ("AB12CD34",), currencies="CAD;USD"
)
FIG = ManualAccount("fig", "Fig", "wallet", ("W-0001-ZZ",), currencies="CAD;USD")
JUNIPER = ManualAccount(
    "juniper", "Juniper", "payment_processor", ("SAMPLE999JUNIPER",), currencies="USD"
)
HAZEL_CSV = "hazel/activity-2051-04-30.csv"
FIG_CSV = "fig/statement-2045-08-28.csv"
JUNIPER_CSV = "juniper/balance-2045-08-27.csv"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


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
        return parse_document(self.uow, doc, self.archive)

    def document(self, doc: uuid.UUID) -> Any:
        return self.conn.execute(select(document).where(document.c.id == doc)).one()

    def periods(self, doc: uuid.UUID) -> list[Any]:
        return list(
            self.conn.execute(
                select(statement_period)
                .where(statement_period.c.document_id == doc)
                .order_by(statement_period.c.currency)
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
CONVERSION = (False, False, True, False, None)


def period_view(period: Any) -> tuple[object, ...]:
    return (
        period.account_id, period.currency, period.period_start, period.period_end,
        period.control_kind, period.tied_out,
    )  # fmt: skip


# --- Hazel activity CSV --------------------------------------------------------------


def test_hazel_csv_is_running_balance_per_currency(setup: Setup) -> None:
    setup.register(HAZEL)
    doc = setup.file(HAZEL_CSV, fixture("hazel_activity.csv"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=2, lines=7, segments=2)

    hazel = account_id("hazel")
    assert [period_view(each) for each in setup.periods(doc)] == [
        (hazel, "CAD", D(2030, 10, 28), D(2030, 11, 3), "running_balance", True),
        (hazel, "USD", D(2030, 11, 3), D(2030, 11, 20), "running_balance", True),
    ]
    row = setup.document(doc)
    assert (row.kind, row.parser_id, row.official) == (
        "export", "hazel.activity_csv", True,
    )  # fmt: skip


def test_hazel_csv_lines_match_the_statement_shape(setup: Setup) -> None:
    setup.register(HAZEL)
    doc = setup.file(HAZEL_CSV, fixture("hazel_activity.csv"))
    setup.parse(doc)

    got = [
        (line.posting_date, line.amount, line.currency, line.fee_type,
         line.description_raw, line.running_balance, hints(line))
        for line in setup.lines(doc)
    ]  # fmt: skip
    assert got == [
        (D(2030, 10, 28), Decimal("-73.21"), "CAD", None,
         "HZ SPEND Sample Shop", Decimal("11.16"), NO_HINT),
        (D(2030, 11, 3), Decimal("33.48"), "CAD", None,
         "HZ LOAD", Decimal("44.64"),
         (True, False, False, False, None)),
        (D(2030, 11, 3), Decimal("-44.64"), "CAD", None,
         "HZ RATE", Decimal("0.00"), CONVERSION),
        (D(2030, 11, 3), Decimal("32.13"), "USD", None,
         "HZ RATE", Decimal("37.71"), CONVERSION),
        # The authorization on hold before it gives no line.
        (D(2030, 11, 4), Decimal("-37.71"), "USD", None,
         "HZ PLAN Sample Streaming Co",
         Decimal("0.00"), NO_HINT),
        # A fee is its own line, after the amount; only the row's last line carries
        # the balance the row prints.
        (D(2030, 11, 20), Decimal("22.32"), "USD", None,
         "HZ INFLOW Sample Client Inc.", None, NO_HINT),
        (D(2030, 11, 20), Decimal("-0.43"), "USD", "fee",
         "HZ INFLOW Sample Client Inc. (fee)", Decimal("21.89"), NO_HINT),
    ]  # fmt: skip
    assert [line.counterparty for line in setup.lines(doc)] == [
        "Sample Shop", None, None, None, "Sample Streaming Co", "Sample Client Inc.",
        None,
    ]  # fmt: skip


def test_a_hazel_balance_that_does_not_continue_is_a_gap(setup: Setup) -> None:
    setup.register(HAZEL)
    rows = fixture_rows("hazel_activity.csv")
    rows[0]["Hz Balance"] = "12.01"  # the October purchase's balance
    doc = setup.file(HAZEL_CSV, csv_text("hazel", rows))

    parsed = setup.parse(doc)

    assert (parsed.status, parsed.lines, parsed.gaps) == ("parsed", 7, 1)
    cad = setup.periods(doc)[0]
    assert (cad.control_kind, cad.tied_out) == ("running_balance", False)


def test_a_currency_of_one_row_with_a_fee_does_not_tie_out(setup: Setup) -> None:
    """A row's own lines never verify each other: one row is no segment."""
    setup.register(HAZEL)
    rows = [
        row for row in fixture_rows("hazel_activity.csv") if row["Hz Fee"] != "0.00"
    ]
    doc = setup.file(HAZEL_CSV, csv_text("hazel", rows))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=2)
    (usd,) = setup.periods(doc)
    assert (usd.currency, usd.control_kind, usd.tied_out) == (
        "USD", "running_balance", False,
    )  # fmt: skip


def test_a_hazel_csv_with_another_header_is_unparsed(setup: Setup) -> None:
    setup.register(HAZEL)
    rows = fixture_rows("hazel_activity.csv")
    doc = setup.file(HAZEL_CSV, csv_text("hazel", rows, renamed={"Hz Kind": "Hz Sort"}))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")
    assert setup.lines(doc) == []
    assert setup.periods(doc) == []


def test_a_hazel_row_whose_fee_does_not_read_is_unparsed(setup: Setup) -> None:
    setup.register(HAZEL)
    rows = fixture_rows("hazel_activity.csv")
    rows[-1]["Hz Fee"] = "-0.8O"
    doc = setup.file(HAZEL_CSV, csv_text("hazel", rows))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_the_hazel_csv_takes_the_one_hazel_account(setup: Setup) -> None:
    """The export prints no account identifier: none or two Hazel accounts reject."""
    setup.register(FIG)
    doc = setup.file(HAZEL_CSV, fixture("hazel_activity.csv"))
    assert setup.parse(doc) == Parsed("rejected", "unknown_account")

    second = dataclasses.replace(HAZEL, slug="hazel-two", identifiers=("OTHER555X1",))
    setup.register(HAZEL, second)
    assert setup.parse(doc) == Parsed("rejected", "ambiguous_account")


def test_only_a_numbered_account_of_the_vendor_kind_is_a_candidate(
    setup: Setup,
) -> None:
    """Another kind of account at the vendor, or one known by a card alone, does not
    make the file ambiguous."""
    crypto = ManualAccount("pp-crypto", "Hazel", "crypto_wallet", ("PPC555X3",))
    card_only = ManualAccount(
        "pp-card", "Hazel", "payment_processor", ("9999 99** **** 5552",),
        "masked_card",
    )  # fmt: skip
    setup.register(HAZEL, crypto, card_only)
    doc = setup.file(HAZEL_CSV, fixture("hazel_activity.csv"))

    assert setup.parse(doc).status == "parsed"
    assert {each.account_id for each in setup.periods(doc)} == {account_id("hazel")}


def test_an_account_whose_number_ended_before_the_file_is_no_candidate(
    setup: Setup,
) -> None:
    replaced = ManualAccount(
        "pp-old", "Hazel", "payment_processor", ("OLDPP555X4",),
        currencies="CAD;USD", valid_to="2030-01-31",
    )  # fmt: skip
    setup.register(replaced)
    doc = setup.file(HAZEL_CSV, fixture("hazel_activity.csv"))
    assert setup.parse(doc) == Parsed("rejected", "unknown_account")

    setup.register(replaced, HAZEL)
    assert setup.parse(doc).status == "parsed"
    assert {each.account_id for each in setup.periods(doc)} == {account_id("hazel")}


def test_the_held_row_is_stored_nowhere(setup: Setup) -> None:
    setup.register(HAZEL)
    doc = setup.file(HAZEL_CSV, fixture("hazel_activity.csv"))
    setup.parse(doc)

    assert "hz held" not in setup.stored_text(doc)


# --- Fig statement CSV ---------------------------------------------------------------


def test_fig_csv_is_running_balance_per_currency_and_official(setup: Setup) -> None:
    setup.register(FIG)
    doc = setup.file(FIG_CSV, fixture("fig_statement.csv"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=2, lines=11, segments=2)

    fig = account_id("fig")
    assert [period_view(each) for each in setup.periods(doc)] == [
        (fig, "CAD", D(2045, 8, 8), D(2045, 8, 20), "running_balance", True),
        (fig, "USD", D(2045, 8, 20), D(2045, 8, 27), "running_balance", True),
    ]
    row = setup.document(doc)
    assert (row.kind, row.parser_id, row.official) == (
        "export", "fig.statement_csv", True,
    )  # fmt: skip


def test_fig_csv_lines_match_the_history_shape(setup: Setup) -> None:
    """The amounts, signs and fee lines of ``fig_history.txt``'s transactions."""
    setup.register(FIG)
    doc = setup.file(FIG_CSV, fixture("fig_statement.csv"))
    setup.parse(doc)

    lines = setup.lines(doc)
    got = [
        (line.posting_date, line.amount, line.currency, line.fee_type,
         line.running_balance, hints(line))
        for line in lines
    ]  # fmt: skip
    # Only a row's last line carries the balance the row prints.
    assert got == [
        (D(2045, 8, 8), Decimal("-1.00"), "CAD", None, None, NO_HINT),
        (D(2045, 8, 8), Decimal("-4.30"), "CAD", "fee", Decimal("1597.06"), NO_HINT),
        (D(2045, 8, 10), Decimal("-503.61"), "CAD", None, None, NO_HINT),
        (D(2045, 8, 10), Decimal("-0.40"), "CAD", "fee", Decimal("1093.05"), NO_HINT),
        (D(2045, 8, 12), Decimal("-83.99"), "CAD", None, None, NO_HINT),
        (D(2045, 8, 12), Decimal("-0.10"), "CAD", "conversion_fee",
         Decimal("1008.96"), NO_HINT),
        (D(2045, 8, 15), Decimal("-15.15"), "CAD", None, Decimal("993.81"), NO_HINT),
        # The conversion's in-leg; its out-leg carries the only fee line.
        (D(2045, 8, 20), Decimal("687.54"), "CAD", None, Decimal("1681.35"),
         CONVERSION),
        (D(2045, 8, 20), Decimal("-504.01"), "USD", None, None, CONVERSION),
        (D(2045, 8, 20), Decimal("-0.53"), "USD", "conversion_fee",
         Decimal("503.48"), NO_HINT),
        (D(2045, 8, 27), Decimal("257.05"), "USD", None, Decimal("760.53"),
         (True, False, False, False, "juniper")),
    ]  # fmt: skip
    card = lines[4]
    assert (card.original_amount, card.original_currency) == (Decimal("-4.00"), "USD")
    assert [line.counterparty for line in lines] == [
        "Sample Payee Two", None, "Sample Payee", None, None, None, None, None, None,
        None, "JUNIPER PAYMENTS",
    ]  # fmt: skip


def test_a_fig_csv_with_another_header_is_unparsed(setup: Setup) -> None:
    setup.register(FIG)
    rows = fixture_rows("fig_statement.csv")
    doc = setup.file(FIG_CSV, csv_text("fig", rows, renamed={"Fig Left": "Fig Rest"}))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")
    assert setup.lines(doc) == []


def test_a_fig_balance_that_does_not_continue_is_a_gap(setup: Setup) -> None:
    setup.register(FIG)
    rows = fixture_rows("fig_statement.csv")
    (card,) = [row for row in rows if row["Fig Amount"] == "-84.09"]
    card["Fig Amount"] = "-84.19"
    doc = setup.file(FIG_CSV, csv_text("fig", rows))

    parsed = setup.parse(doc)

    assert (parsed.status, parsed.lines, parsed.gaps) == ("parsed", 11, 1)
    assert [each.tied_out for each in setup.periods(doc)] == [False, True]


def test_a_negative_fig_fee_is_unparsed(setup: Setup) -> None:
    setup.register(FIG)
    rows = fixture_rows("fig_statement.csv")
    (sent,) = [row for row in rows if row["Fig Fee"] == "0.40"]
    sent["Fig Fee"] = "-0.40"
    doc = setup.file(FIG_CSV, csv_text("fig", rows))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_fig_csv_row_kinds_are_read_and_stored_nowhere(setup: Setup) -> None:
    setup.register(FIG)
    doc = setup.file(FIG_CSV, fixture("fig_statement.csv"))
    setup.parse(doc)

    stored = setup.stored_text(doc)
    for kind in {row["Fig Kind"] for row in fixture_rows("fig_statement.csv")}:
        assert kind.casefold() not in stored, kind


# --- Juniper balance history CSV -----------------------------------------------------


def juniper_lines(setup: Setup, doc: uuid.UUID) -> list[tuple[object, ...]]:
    return [
        (line.posting_date, line.amount, line.currency, line.fee_type,
         line.description_raw, hints(line))
        for line in setup.lines(doc)
    ]  # fmt: skip


PAYOUT_HINT = (True, False, False, False, "fig")


def test_juniper_csv_gives_gross_fee_and_payout_per_payout(setup: Setup) -> None:
    setup.register(JUNIPER)
    doc = setup.file(JUNIPER_CSV, fixture("juniper_balance.csv"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=6)

    # No balance column: one unverified period over every row, the charge not yet
    # paid out included.
    assert [period_view(each) for each in setup.periods(doc)] == [
        (account_id("juniper"), "USD", D(2045, 8, 3), D(2045, 8, 27), "none", False),
    ]
    row = setup.document(doc)
    assert (row.kind, row.parser_id, row.official) == (
        "export", "juniper.balance_csv", True,
    )  # fmt: skip
    # Each payout, oldest first, on its created date: the gross of the rows it pays
    # out (a refund nets against the charges), one fee line, then the payout debit
    # carrying the Fig hint. The charge with no payout gives no line. The first
    # payout's own row carries a fee too: it joins the fee line, so the payout is the
    # gross less every fee (45.72 less 1.95 and 2.99).
    assert juniper_lines(setup, doc) == [
        (D(2045, 8, 12), Decimal("45.72"), "USD", None, "Gross charges", NO_HINT),
        (D(2045, 8, 12), Decimal("-4.94"), "USD", "fee", "Juniper fees", NO_HINT),
        (D(2045, 8, 12), Decimal("-40.78"), "USD", None, "Payout to bank",
         PAYOUT_HINT),
        (D(2045, 8, 26), Decimal("264.54"), "USD", None, "Gross charges", NO_HINT),
        (D(2045, 8, 26), Decimal("-7.49"), "USD", "fee", "Juniper fees", NO_HINT),
        (D(2045, 8, 26), Decimal("-257.05"), "USD", None, "Payout to bank",
         PAYOUT_HINT),
    ]  # fmt: skip


def test_each_juniper_payout_reconciles_gross_less_fees(setup: Setup) -> None:
    setup.register(JUNIPER)
    doc = setup.file(JUNIPER_CSV, fixture("juniper_balance.csv"))
    setup.parse(doc)

    lines = setup.lines(doc)
    for gross, fee, payout in (lines[0:3], lines[3:6]):
        assert (gross.amount > 0, fee.fee_type, payout.transfer_hint) == (
            True, "fee", True,
        )  # fmt: skip
        assert gross.amount + fee.amount == -payout.amount
    assert {line.running_balance for line in lines} == {None}


def test_a_payout_partly_in_the_file_gives_its_payout_line_alone(
    setup: Setup,
) -> None:
    """An export cut by date: the later payout's rows do not add up to it, so only the
    payout debit (still hinted) is a line; a fuller export adds the gross and fee."""
    setup.register(JUNIPER)
    rows = fixture_rows("juniper_balance.csv")
    kept = [row for row in rows if row["Jn Day"] != "2045-08-14"]
    doc = setup.file(JUNIPER_CSV, csv_text("juniper", kept))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=4)
    assert juniper_lines(setup, doc)[3:] == [
        (D(2045, 8, 26), Decimal("-257.05"), "USD", None, "Payout to bank",
         PAYOUT_HINT),
    ]  # fmt: skip


def test_a_payout_with_no_fee_gives_no_fee_line(setup: Setup) -> None:
    """A payout whose rows and own row carry no fee gives its gross and the payout
    debit, and no zero fee line."""
    setup.register(JUNIPER)
    rows = [
        {"Jn Day": "2045-08-03", "Jn Kind": "TAKE", "Jn Unit": "USD",
         "Jn Amount": "58.03", "Jn Fee": "0.00", "Jn Group": "grp503"},
        {"Jn Day": "2045-08-12", "Jn Kind": "BANKED", "Jn Unit": "USD",
         "Jn Amount": "-58.03", "Jn Fee": "0.00", "Jn Group": "grp503"},
    ]  # fmt: skip
    doc = setup.file(JUNIPER_CSV, csv_text("juniper", rows))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=2)
    assert juniper_lines(setup, doc) == [
        (D(2045, 8, 12), Decimal("58.03"), "USD", None, "Gross charges", NO_HINT),
        (D(2045, 8, 12), Decimal("-58.03"), "USD", None, "Payout to bank",
         PAYOUT_HINT),
    ]  # fmt: skip


def test_a_juniper_csv_with_another_header_is_unparsed(setup: Setup) -> None:
    setup.register(JUNIPER)
    rows = fixture_rows("juniper_balance.csv")
    doc = setup.file(
        JUNIPER_CSV, csv_text("juniper", rows, renamed={"Jn Group": "Jn Lot"})
    )

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")
    assert setup.lines(doc) == []
    assert setup.periods(doc) == []


@pytest.mark.parametrize(
    ("day", "column", "value"),
    [
        ("2045-08-17", "Jn Amount", "12O.00"),
        ("2045-08-17", "Jn Fee", "-3.90"),
        ("2045-08-26", "Jn Amount", "257.05"),
        # A type the parser does not read joins no gross: the file is unparsed.
        ("2045-08-17", "Jn Kind", "ADJUST"),
        # A row in another currency than the payout that pays it out.
        ("2045-08-20", "Jn Unit", "CAD"),
        # Two payouts with one group.
        ("2045-08-12", "Jn Group", "grp502"),
    ],
    ids=[
        "amount_does_not_read",
        "negative_fee",
        "payout_not_a_debit",
        "unknown_row_type",
        "row_currency_not_the_payouts",
        "duplicate_payout_group",
    ],  # fmt: skip
)
def test_a_juniper_row_outside_the_layout_is_unparsed(
    setup: Setup, day: str, column: str, value: str
) -> None:
    setup.register(JUNIPER)
    rows = fixture_rows("juniper_balance.csv")
    (row,) = [each for each in rows if each["Jn Day"] == day]
    row[column] = value
    doc = setup.file(JUNIPER_CSV, csv_text("juniper", rows))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_the_juniper_csv_takes_the_one_juniper_account(setup: Setup) -> None:
    """The export prints no account identifier: none or two Juniper accounts reject."""
    setup.register(HAZEL)
    doc = setup.file(JUNIPER_CSV, fixture("juniper_balance.csv"))
    assert setup.parse(doc) == Parsed("rejected", "unknown_account")

    second = dataclasses.replace(
        JUNIPER, slug="juniper-two", identifiers=("OTHER555X2",)
    )
    setup.register(JUNIPER, second)
    assert setup.parse(doc) == Parsed("rejected", "ambiguous_account")


def test_juniper_csv_ids_descriptions_and_customers_are_stored_nowhere(
    setup: Setup,
) -> None:
    setup.register(JUNIPER)
    doc = setup.file(JUNIPER_CSV, fixture("juniper_balance.csv"))
    setup.parse(doc)

    stored = setup.stored_text(doc)
    assert "grp50" not in stored
    assert {line.counterparty for line in setup.lines(doc)} == {None}


# --- registration ---------------------------------------------------------------------


def test_the_csv_parsers_register_as_hint_setting_exports() -> None:
    registered = {
        parser.parser_id: (parser.version, parser.sets_hints)
        for issuer in ("hazel", "fig", "juniper")
        for parser in parsers_for(issuer, "csv")
    }
    assert registered == {
        "hazel.activity_csv": ("1", True),
        "fig.statement_csv": ("1", True),
        "juniper.balance_csv": ("1", True),
    }
