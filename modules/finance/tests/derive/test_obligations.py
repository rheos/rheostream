"""Stage 14, the five paths: card statements, parsed bills, manual bills, arrangements
and arrears, manual debts (FR-19, FR-21; Key decision 20; edge case 66; AC-87).

Seam: ``derive.run`` through the derive ``ledger`` over the synthetic statement and bill
layouts, read through the real parsers. Newer documents are the same fixtures with
invented dates and amounts written in. Every value is synthetic.
"""

from __future__ import annotations

import ast
import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Row, select

from rheo_finance.derive.stage14 import NoPayee, obligation_id
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.arrangements import ARRANGEMENTS_FILE
from rheo_finance.manual.obligations import OBLIGATIONS_FILE
from rheo_finance.manual.payees import PAYEES_FILE
from rheo_finance.reports.views import (
    DUPLICATE_BILL_MONTH,
    MANUAL_MISMATCH,
    STATEMENT_CONFLICT,
    TYPED_BILL_UNJOINED,
)
from rheo_finance.storage.schema import document, obligation, payee, report_status

from .support import FIXTURES, Ledger

D = dt.date
CARD = "XXXX-9991"
SUMAC_CARD = "**** **** **** 9992"
NEEM_STATEMENT = "alder-neem/2050-01-24.pdf"
NEWER_NEEM_STATEMENT = "alder-neem/2050-03-12.pdf"
YEW = "yew/bill.pdf"
BOXWOOD = "boxwood/bill.pdf"
SRC = Path(__file__).resolve().parents[2] / "src" / "rheo_finance"
SECOND_YEW_NUMBER = "999000456"
ARREARS_LINE = "   Arrears   $195.44\n"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def accounts(ledger: Ledger, *rows: tuple[str, str, str, str, str]) -> None:
    """Accounts as ``(slug, institution, kind, identifier, payee patterns)``."""
    ledger.manual(
        ACCOUNTS_FILE,
        *(
            f"{slug},{slug.title()},{institution},{kind},CAD,monthly,,,,,,{patterns},,,"
            for slug, institution, kind, _, patterns in rows
        ),
    )
    ledger.manual(
        IDENTIFIERS_FILE,
        *(
            f"{slug},{identifier},"
            f"{'masked_card' if kind == 'credit_card' else 'number'},,"
            for slug, _, kind, identifier, _ in rows
        ),
    )


def neem(ledger: Ledger, patterns: str = "") -> None:
    accounts(ledger, ("neem", "Alder", "credit_card", CARD, patterns))


def yew_account(ledger: Ledger, patterns: str = "") -> None:
    accounts(ledger, ("mobile", "Yew", "bill_vendor", "999000123", patterns))


def boxwood_account(ledger: Ledger) -> None:
    accounts(ledger, ("utility", "Boxwood", "bill_vendor", "9990001234", ""))


def yew_bill(
    bill: str = "June 28, 2049",
    due_short: str = "Jul 19, 2049",
    due_long: str = "July 19, 2049",
    total: str | None = None,
) -> str:
    """The Yew bill fixture with an invented bill date, due date and total. A
    written total drops the printed unpaid balance, so the bill states no identity."""
    text = (
        fixture("yew_bill.txt")
        .replace("June 28, 2049", bill)
        .replace("Jul 19, 2049", due_short)
        .replace("July 19, 2049", due_long)
    )
    if total is None:
        return text
    return text.replace("   Yew prior left   $38.76\n", "").replace("101.40", total)


def boxwood_bill(bill: str, due: str) -> str:
    return (
        fixture("boxwood_bill.txt")
        .replace("January 26, 2029", bill)
        .replace("February 16, 2029", due)
    )


def rows(ledger: Ledger) -> list[Row[tuple[object, ...]]]:
    """The obligations, oldest issue first."""
    return list(
        ledger.conn.execute(
            select(obligation).order_by(
                obligation.c.issued_on, obligation.c.period_label
            )
        ).all()
    )


def payee_id(ledger: Ledger, slug: str) -> uuid.UUID:
    found: uuid.UUID = ledger.conn.execute(
        select(payee.c.id).where(payee.c.slug == slug)
    ).scalar_one()
    return found


def owed_total(ledger: Ledger) -> Decimal:
    return sum(
        (Decimal(row.remaining) for row in rows(ledger) if row.status != "superseded"),
        Decimal(0),
    )


def facts(ledger: Ledger, name: str) -> dict[str | None, int]:
    found = ledger.conn.execute(
        select(report_status.c.subject, report_status.c.count).where(
            report_status.c.fact == name
        )
    ).all()
    return {subject: count for subject, count in found}


def duplicate_months(ledger: Ledger) -> dict[str | None, int]:
    return facts(ledger, DUPLICATE_BILL_MONTH)


def by_label(ledger: Ledger) -> dict[str, Row[tuple[object, ...]]]:
    return {str(row.period_label): row for row in rows(ledger)}


def sample_internet(ledger: Ledger) -> None:
    ledger.manual(PAYEES_FILE, "sample-internet,Sample Internet,vendor,,,")


def yew_with_arrears(text: str) -> str:
    return text.replace(
        "   Yew prior left   $38.76\n",
        "   Yew prior left   $38.76\n   Yew lagging   $33.89\n",
    )


def boxwood_with_arrears(text: str) -> str:
    return text.replace(
        "   Balance forward   $160.82\n",
        f"   Balance forward   $160.82\n{ARREARS_LINE}",
    )


# --- path (1): card statements --------------------------------------------------------


def test_a_card_statement_gives_the_obligation_as_printed(ledger: Ledger) -> None:
    neem(ledger)
    ledger.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert (row.kind, row.period_label) == ("card_statement", "card-2050-01-24")
    assert row.statement_date == row.issued_on == D(2050, 1, 24)
    assert row.due_date == D(2050, 1, 29)
    assert row.amount_due == Decimal("3088.66")
    assert row.minimum_due == row.threshold_due == Decimal("98.19")
    assert row.paid == 0
    assert row.remaining == Decimal("3088.66")
    assert row.threshold_remaining == Decimal("98.19")
    assert row.status == "open"
    assert row.primary_source == NEEM_STATEMENT
    assert row.supersedes_id is None
    assert row.manual_mismatch is None and row.payments_mismatch is None


def test_an_sumac_statement_gives_the_same_shape(ledger: Ledger) -> None:
    accounts(ledger, ("sumac-card", "SUMAC", "credit_card", SUMAC_CARD, ""))
    ledger.file("sumac/2042-10-22.pdf", fixture("sumac_statement.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert (row.kind, row.period_label) == ("card_statement", "card-2042-10-22")
    assert (row.amount_due, row.minimum_due, row.threshold_due) == (
        Decimal("1064.18"),
        Decimal("70.46"),
        Decimal("70.46"),
    )
    assert row.due_date == D(2042, 11, 13)
    assert row.payee_id == payee_id(ledger, "sumac-card")


def test_a_statement_with_no_minimum_has_the_full_balance_as_its_threshold(
    ledger: Ledger,
) -> None:
    neem(ledger)
    ledger.file(NEWER_NEEM_STATEMENT, fixture("alder_neem_statement_no_minimum.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.minimum_due is None
    assert row.threshold_due == row.threshold_remaining == Decimal("48.86")


@pytest.mark.parametrize(
    ("printed", "paid_in"), [("$0.00", "$830.62"), ("-$48.86", "$879.48")]
)
def test_a_statement_at_zero_or_in_credit_owes_nothing(
    ledger: Ledger, printed: str, paid_in: str
) -> None:
    neem(ledger)
    ledger.file(
        NEWER_NEEM_STATEMENT,
        fixture("alder_neem_statement_no_minimum.txt")
        .replace("Neem credits in   -   $781.76", f"Neem credits in   -   {paid_in}")
        .replace("- $781.76", f"- {paid_in}")
        .replace("Neem cycle balance   $48.86", f"Neem cycle balance   {printed}")
        .replace("$48.86   March 30", f"{printed}   March 30"),
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert row.amount_due == Decimal(printed.replace("$", ""))
    assert (row.remaining, row.threshold_remaining) == (0, 0)
    assert row.status == "paid"


def test_a_newer_statement_supersedes_the_older_and_never_doubles_the_carried_balance(
    ledger: Ledger,
) -> None:
    neem(ledger)
    # The newer one first: the order of filing is not what makes it newer.
    ledger.file(NEWER_NEEM_STATEMENT, fixture("alder_neem_statement_no_minimum.txt"))
    ledger.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))
    ledger.derive()

    older, newer = rows(ledger)

    assert (older.period_label, newer.period_label) == (
        "card-2050-01-24",
        "card-2050-03-12",
    )
    assert (older.status, newer.status) == ("superseded", "open")
    assert newer.supersedes_id == older.id
    assert older.supersedes_id is None
    assert owed_total(ledger) == newer.amount_due == Decimal("48.86")


def test_a_chain_of_three_statements_owes_only_the_newest(ledger: Ledger) -> None:
    neem(ledger)
    ledger.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))
    ledger.file(
        "alder-neem/2050-02-13.pdf",
        fixture("alder_neem_statement_no_minimum.txt")
        .replace("February 14 to March 12", "January 25 to February 13")
        .replace("March 21", "February 20")
        .replace("FEB 21   FEB 22", "JAN 28   JAN 29")
        .replace("FEB 25   FEB 25", "JAN 31   JAN 31"),
    )
    ledger.file(NEWER_NEEM_STATEMENT, fixture("alder_neem_statement_no_minimum.txt"))
    ledger.derive()

    first, second, third = rows(ledger)

    assert [row.status for row in (first, second, third)] == [
        "superseded",
        "superseded",
        "open",
    ]
    assert (second.supersedes_id, third.supersedes_id) == (first.id, second.id)


def zero_balance_statement() -> str:
    return (
        fixture("alder_neem_statement_no_minimum.txt")
        .replace("Neem credits in   -   $781.76", "Neem credits in   -   $830.62")
        .replace("- $781.76", "- $830.62")
        .replace("Neem cycle balance   $48.86", "Neem cycle balance   $0.00")
        .replace("$48.86   March 30", "$0.00   March 30")
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [("alder-neem/a.pdf", "alder-neem/b.pdf"),
     ("alder-neem/b.pdf", "alder-neem/a.pdf")],
    ids=["one-way", "the-other-way"],
)  # fmt: skip
def test_two_statements_of_one_closing_date_with_other_figures_are_one_row_and_a_fact(
    ledger: Ledger, first: str, second: str
) -> None:
    """The one ``precedence`` ranks first holds the row, here the larger sha256: which
    path sorts last does not decide it."""
    neem(ledger)
    ledger.file(first, fixture("alder_neem_statement_no_minimum.txt"))
    ledger.file(second, zero_balance_statement())

    out = ledger.derive()

    assert out.documents == {"parsed": 2}
    (row,) = rows(ledger)
    assert row.period_label == "card-2050-03-12"
    winner = ledger.conn.execute(
        select(document.c.path).order_by(document.c.sha256.desc()).limit(1)
    ).scalar_one()
    assert row.primary_source == winner
    assert row.statement_conflict is True
    assert facts(ledger, STATEMENT_CONFLICT) == {"Neem": 1}


def test_two_statements_of_one_closing_date_with_the_same_figures_record_no_fact(
    ledger: Ledger,
) -> None:
    """Their lines differ, so neither is a copy of the other; what is owed does not."""
    neem(ledger)
    statement = fixture("alder_neem_statement_no_minimum.txt")
    ledger.file("alder-neem/a.pdf", statement)
    ledger.file(
        "alder-neem/b.pdf", statement.replace("SAMPLE PHARMACY", "SAMPLE CHEMIST")
    )

    out = ledger.derive()

    assert out.documents == {"parsed": 2}
    (row,) = rows(ledger)
    assert row.statement_conflict is False
    assert facts(ledger, STATEMENT_CONFLICT) == {}


def test_two_cards_are_two_payees_and_neither_supersedes_the_other(
    ledger: Ledger,
) -> None:
    accounts(
        ledger,
        ("neem", "Alder", "credit_card", CARD, ""),
        ("sumac-card", "SUMAC", "credit_card", SUMAC_CARD, ""),
    )
    ledger.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))
    ledger.file("sumac/2042-10-22.pdf", fixture("sumac_statement.txt"))
    ledger.derive()

    assert [row.status for row in rows(ledger)] == ["open", "open"]


# --- an unrecognised subscription charge ----------------------------------------------


def test_a_subscription_charge_on_a_statement_creates_no_obligation(
    ledger: Ledger,
) -> None:
    neem(ledger)
    # A vendor the owner registered, and its charge on the card.
    ledger.manual(PAYEES_FILE, "cloud,Cloud Vendor,vendor,SAMPLE PHARMACY,,")
    ledger.file(NEWER_NEEM_STATEMENT, fixture("alder_neem_statement_no_minimum.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.kind == "card_statement"
    assert row.payee_id == payee_id(ledger, "neem")


def test_a_card_export_alone_creates_no_obligation(ledger: Ledger) -> None:
    neem(ledger)
    ledger.manual(PAYEES_FILE, "cafe,A Cafe,vendor,SAMPLE CAFE,,")
    ledger.file("alder-neem/2048-02-22.csv", fixture("alder_neem_export.csv"))
    out = ledger.derive()

    assert out.documents == {"parsed": 1}
    assert ledger.txns() != []
    assert rows(ledger) == []


# --- path (2): parsed bills -----------------------------------------------------------


def test_a_yew_bill_is_labelled_by_its_due_month_and_issued_on_its_bill_date(
    ledger: Ledger,
) -> None:
    yew_account(ledger)
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert (row.kind, row.period_label) == ("bill", "bill-2049-07")
    assert row.issued_on == D(2049, 6, 28)
    assert row.statement_date is None
    assert row.due_date == D(2049, 7, 19)
    assert row.amount_due == row.threshold_due == Decimal("101.40")
    assert row.minimum_due is None
    assert (row.paid, row.remaining, row.threshold_remaining) == (
        0,
        Decimal("101.40"),
        Decimal("101.40"),
    )
    assert (row.status, row.primary_source) == ("open", YEW)
    assert row.payee_id == payee_id(ledger, "yew")
    assert duplicate_months(ledger) == {}


def test_a_boxwood_bill_due_the_month_after_its_date_takes_the_due_month(
    ledger: Ledger,
) -> None:
    boxwood_account(ledger)
    ledger.file(BOXWOOD, fixture("boxwood_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.period_label == "bill-2029-02"
    # The bill date, not the billing period's end, which comes days before it.
    assert row.issued_on == D(2029, 1, 26)
    assert row.due_date == D(2029, 2, 16)
    assert row.amount_due == Decimal("494.66")
    assert row.payee_id == payee_id(ledger, "boxwood")


def test_a_second_bill_of_a_due_month_takes_the_suffix_in_bill_date_order(
    ledger: Ledger,
) -> None:
    yew_account(ledger)
    # The later bill is filed first, and its path sorts first.
    ledger.file(
        "yew/a-later.pdf",
        yew_bill("July 05, 2049", "Jul 23, 2049", "July 23, 2049"),
    )
    ledger.file("yew/b-earlier.pdf", yew_bill())
    ledger.file(
        "yew/c-next.pdf",
        yew_bill("July 26, 2049", "Aug 16, 2049", "August 16, 2049"),
    )
    ledger.derive()

    first, second, third = rows(ledger)

    assert (first.period_label, first.primary_source) == (
        "bill-2049-07",
        "yew/b-earlier.pdf",
    )
    assert (second.period_label, second.primary_source) == (
        "bill-2049-07-2",
        "yew/a-later.pdf",
    )
    assert third.period_label == "bill-2049-08"
    assert duplicate_months(ledger) == {"yew": 1}


def test_a_newer_bill_supersedes_the_older_and_only_it_is_owed(ledger: Ledger) -> None:
    boxwood_account(ledger)
    ledger.file("boxwood/a.pdf", boxwood_bill("February 25, 2029", "March 18, 2029"))
    ledger.file("boxwood/b.pdf", boxwood_bill("January 26, 2029", "February 16, 2029"))
    ledger.derive()

    older, newer = rows(ledger)

    assert (older.period_label, newer.period_label) == (
        "bill-2029-02",
        "bill-2029-03",
    )
    assert (older.status, newer.status) == ("superseded", "open")
    assert newer.supersedes_id == older.id
    assert owed_total(ledger) == Decimal("494.66")


def test_two_bills_with_one_bill_date_and_due_month_still_owe_one(
    ledger: Ledger,
) -> None:
    """A reissued bill prints another total on the same dates: not a copy, so it is
    numbered, and the two never add up."""
    yew_account(ledger)
    ledger.file("yew/a.pdf", yew_bill(total="101.40"))
    ledger.file("yew/b.pdf", yew_bill(total="105.10"))
    ledger.derive()

    first, second = rows(ledger)

    assert (first.period_label, second.period_label) == (
        "bill-2049-07",
        "bill-2049-07-2",
    )
    assert first.status == "superseded"
    assert owed_total(ledger) == Decimal("105.10")
    assert duplicate_months(ledger) == {"yew": 1}


def test_a_copy_of_a_bill_makes_no_second_obligation(ledger: Ledger) -> None:
    yew_account(ledger)
    ledger.file("yew/a.pdf", fixture("yew_bill.txt"))
    ledger.file("yew/b.pdf", fixture("yew_bill.txt") + "\n")
    out = ledger.derive()

    assert out.documents == {"parsed": 1, "superseded": 1}
    (row,) = rows(ledger)
    assert row.period_label == "bill-2049-07"
    assert duplicate_months(ledger) == {}


def test_a_bill_that_prints_no_total_makes_no_obligation(ledger: Ledger) -> None:
    yew_account(ledger)
    ledger.file(
        YEW,
        "\n".join(
            line
            for line in fixture("yew_bill.txt").splitlines()
            if "Yew due sum" not in line
        ),
    )
    out = ledger.derive()

    assert out.documents == {"parsed": 1}
    assert rows(ledger) == []


# --- the payee and the key ------------------------------------------------------------


def test_a_statement_on_an_account_not_registered_as_a_card_fails_the_derive(
    ledger: Ledger,
) -> None:
    """Dropping it would hide what the statement says is owed."""
    accounts(ledger, ("neem", "Alder", "chequing", "999000009991", ""))
    ledger.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))

    with pytest.raises(NoPayee, match="account neem"):
        ledger.derive()


def test_a_payee_row_gives_a_statement_on_a_misregistered_card_its_payee(
    ledger: Ledger,
) -> None:
    accounts(ledger, ("neem", "Alder", "chequing", "999000009991", ""))
    ledger.manual(PAYEES_FILE, "neem,My card,vendor,,,")
    ledger.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.kind == "card_statement"
    assert row.payee_id == payee_id(ledger, "neem")


def test_a_card_with_patterns_is_owed_to_its_account_backed_payee(
    ledger: Ledger,
) -> None:
    neem(ledger, "SAMPLE NEEM PAYMENT")
    ledger.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.payee_id == payee_id(ledger, "neem")


def test_a_bill_account_with_patterns_is_owed_to_that_payee_not_the_issuer(
    ledger: Ledger,
) -> None:
    yew_account(ledger, "SAMPLE MOBILE CO")
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.payee_id == payee_id(ledger, "mobile")


def test_a_payee_row_that_takes_the_issuers_slug_holds_its_bills(
    ledger: Ledger,
) -> None:
    yew_account(ledger)
    ledger.manual(PAYEES_FILE, "yew,My phone,vendor,,,")
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.payee_id == payee_id(ledger, "yew")
    label = ledger.conn.scalar(
        select(payee.c.display_label).where(payee.c.id == row.payee_id)
    )
    assert label == "My phone"


def test_a_payee_row_that_takes_a_cards_slug_holds_its_statements(
    ledger: Ledger,
) -> None:
    neem(ledger)
    ledger.manual(PAYEES_FILE, "neem,My card,vendor,,,")
    ledger.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.payee_id == payee_id(ledger, "neem")


def test_the_id_comes_from_the_natural_key_alone(ledger: Ledger) -> None:
    """Superseding the row changes its figures' standing and not its id, so a decision
    that names it still finds it."""
    boxwood_account(ledger)
    ledger.file("boxwood/a.pdf", boxwood_bill("January 26, 2029", "February 16, 2029"))
    ledger.derive()
    (alone,) = rows(ledger)
    boxwood = payee_id(ledger, "boxwood")
    assert alone.id == obligation_id(boxwood, "bill", "bill-2029-02")

    ledger.file("boxwood/b.pdf", boxwood_bill("February 25, 2029", "March 18, 2029"))
    ledger.derive()

    older, _ = rows(ledger)
    assert (older.id, older.status) == (alone.id, "superseded")


def test_a_second_derive_gives_the_same_rows(ledger: Ledger) -> None:
    neem(ledger)
    ledger.file(NEEM_STATEMENT, fixture("alder_neem_statement.txt"))
    ledger.derive()
    before = rows(ledger)

    ledger.derive()

    assert rows(ledger) == before


# --- supersession is per account ------------------------------------------------------


def two_yew_accounts(ledger: Ledger) -> None:
    accounts(
        ledger,
        ("mobile", "Yew", "bill_vendor", "999000123", ""),
        ("second-mobile", "Yew", "bill_vendor", SECOND_YEW_NUMBER, ""),
    )


def second_account(text: str) -> str:
    return text.replace("999000123", SECOND_YEW_NUMBER)


def test_two_accounts_of_one_issuer_do_not_supersede_each_other(ledger: Ledger) -> None:
    two_yew_accounts(ledger)
    ledger.file("yew/a.pdf", yew_bill())
    ledger.file(
        "yew/b.pdf",
        second_account(yew_bill("July 26, 2049", "Aug 16, 2049", "August 16, 2049")),
    )
    ledger.derive()

    first, second = rows(ledger)

    assert (first.period_label, second.period_label) == (
        "bill-2049-07",
        "bill-2049-08",
    )
    assert (first.status, second.status) == ("open", "open")
    assert second.supersedes_id is None
    assert first.payee_id == second.payee_id == payee_id(ledger, "yew")


def test_each_account_of_one_issuer_keeps_its_own_chain(ledger: Ledger) -> None:
    two_yew_accounts(ledger)
    ledger.file("yew/a.pdf", yew_bill())
    ledger.file("yew/b.pdf", second_account(yew_bill()))
    ledger.file(
        "yew/c.pdf",
        yew_bill("July 26, 2049", "Aug 16, 2049", "August 16, 2049"),
    )
    ledger.derive()

    found = by_label(ledger)

    # One due month across two accounts is one payee's second bill of that month.
    assert sorted(found) == ["bill-2049-07", "bill-2049-07-2", "bill-2049-08"]
    assert found["bill-2049-08"].supersedes_id == found["bill-2049-07"].id
    assert found["bill-2049-07"].status == "superseded"
    assert found["bill-2049-07-2"].status == "open"
    assert found["bill-2049-08"].status == "open"
    assert duplicate_months(ledger) == {"yew": 1}


# --- path (3): manual bills -----------------------------------------------------------


def test_a_manual_bill_is_one_obligation_with_its_row_as_its_source(
    ledger: Ledger,
) -> None:
    sample_internet(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,si999,58.55,,2059-02-26,,2059-02-01",
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert (row.kind, row.period_label) == ("bill", "bill-2059-02")
    assert row.issued_on == D(2059, 2, 1)
    assert row.due_date == D(2059, 2, 26)
    assert row.amount_due == row.threshold_due == row.remaining == Decimal("58.55")
    assert row.status == "open"
    assert row.primary_source == row.manual_source == "manual:obligations.csv:2"
    assert row.manual_mismatch is None


def test_a_bills_written_minimum_is_kept_and_is_not_its_threshold(
    ledger: Ledger,
) -> None:
    sample_internet(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,,58.55,11.03,2059-02-26,,2059-02-01",
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert row.minimum_due == Decimal("11.03")
    assert row.threshold_due == row.threshold_remaining == Decimal("58.55")


def test_the_raw_invoice_email_a_row_names_is_its_source_and_makes_nothing(
    ledger: Ledger,
) -> None:
    sample_internet(ledger)
    ledger.file("_mail/invoice.eml", "From: billing@example.test\n\nSample invoice\n")
    ledger.file("_mail/other.eml", "From: billing@example.test\n\nAnother invoice\n")
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,,58.55,,2059-02-26,_mail/invoice.eml,2059-02-01",
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert row.primary_source == "_mail/invoice.eml"
    assert row.manual_source == "manual:obligations.csv:2"


def test_a_row_naming_a_path_with_no_document_keeps_the_row_as_its_source(
    ledger: Ledger,
) -> None:
    sample_internet(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,,58.55,,2059-02-26,_mail/never-filed.eml,2059-02-01",
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert row.primary_source == "manual:obligations.csv:2"


def test_invoice_style_manual_bills_do_not_chain_so_two_of_one_payee_are_both_owed(
    ledger: Ledger,
) -> None:
    """A payee with no bill source: an invoice does not carry the one before it. This is
    how two live obligations of one payee arise for the payment matrix (AC-25)."""
    sample_internet(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,,58.55,,2059-02-26,,2059-02-01",
        "sample-internet,bill,,,75.45,,2059-03-26,,2059-03-01",
    )
    ledger.derive()

    first, second = rows(ledger)

    assert (first.status, second.status) == ("open", "open")
    assert second.supersedes_id is None
    assert owed_total(ledger) == Decimal("134.00")


def test_a_newer_parsed_bill_supersedes_an_older_manual_bill_of_a_bill_source(
    ledger: Ledger,
) -> None:
    """A bill typed from a photo, then the next bill as a PDF: its total carries the
    typed one, so it is owed once."""
    yew_account(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,,,58.55,,2049-06-19,,2049-05-29",
    )
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.derive()

    manual, parsed = rows(ledger)

    assert (manual.status, parsed.status) == ("superseded", "open")
    assert parsed.supersedes_id == manual.id
    assert owed_total(ledger) == Decimal("101.40")


def test_a_newer_manual_bill_supersedes_an_older_parsed_bill_of_a_bill_source(
    ledger: Ledger,
) -> None:
    yew_account(ledger)
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,,,981.77,,2049-08-16,,2049-07-26",
    )
    ledger.derive()

    parsed, manual = rows(ledger)

    assert (parsed.status, manual.status) == ("superseded", "open")
    assert manual.supersedes_id == parsed.id
    assert owed_total(ledger) == Decimal("981.77")


def test_a_manual_bill_takes_its_place_in_the_chain_by_issue_date(
    ledger: Ledger,
) -> None:
    yew_account(ledger)
    ledger.file("yew/a.pdf", yew_bill())
    ledger.file(
        "yew/c.pdf",
        yew_bill("August 26, 2049", "Sep 16, 2049", "September 16, 2049"),
    )
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,,,981.77,,2049-08-16,,2049-07-26",
    )
    ledger.derive()

    first, typed, last = rows(ledger)

    assert (first.status, typed.status, last.status) == (
        "superseded",
        "superseded",
        "open",
    )
    assert (typed.supersedes_id, last.supersedes_id) == (first.id, typed.id)


def test_two_bills_typed_in_for_an_issuer_with_no_parsed_bill_owe_the_newer(
    ledger: Ledger,
) -> None:
    """An issuer's photographed bills: 400 then 600, and the 600 holds the 400."""
    ledger.manual(
        OBLIGATIONS_FILE,
        "boxwood,bill,,,781.76,,2029-02-16,,2029-01-26",
        "boxwood,bill,,,1172.64,,2059-03-11,,2059-02-21",
    )
    ledger.derive()

    older, newer = rows(ledger)

    assert (older.status, newer.status) == ("superseded", "open")
    assert newer.supersedes_id == older.id
    assert owed_total(ledger) == Decimal("1172.64")


def test_two_bills_typed_in_for_a_bill_vendor_account_owe_the_newer(
    ledger: Ledger,
) -> None:
    yew_account(ledger, "SAMPLE MOBILE CO")
    ledger.manual(
        OBLIGATIONS_FILE,
        "mobile,bill,,,781.76,,2029-02-16,,2029-01-26",
        "mobile,bill,,,1172.64,,2059-03-11,,2059-02-21",
    )
    ledger.derive()

    assert owed_total(ledger) == Decimal("1172.64")


def test_a_manual_bill_of_an_issuer_with_two_billed_accounts_joins_neither(
    ledger: Ledger,
) -> None:
    """Nothing says which account it is on, so it supersedes neither."""
    two_yew_accounts(ledger)
    ledger.file("yew/a.pdf", yew_bill())
    ledger.file("yew/b.pdf", second_account(yew_bill()))
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,,,981.77,,2049-08-16,,2049-07-26",
    )
    ledger.derive()

    assert [row.status for row in rows(ledger)] == ["open", "open", "open"]
    assert facts(ledger, TYPED_BILL_UNJOINED) == {"yew": 1}
    assert [row.unjoined for row in rows(ledger)].count(True) == 1


def test_a_typed_bill_that_joins_a_chain_records_no_fact(ledger: Ledger) -> None:
    yew_account(ledger)
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,,,981.77,,2049-08-16,,2049-07-26",
    )
    ledger.derive()

    assert facts(ledger, TYPED_BILL_UNJOINED) == {}


def test_typed_bills_of_an_issuer_with_no_parsed_bill_record_no_fact(
    ledger: Ledger,
) -> None:
    """They chain with each other, and no parsed bill stands beside them."""
    ledger.manual(
        OBLIGATIONS_FILE,
        "boxwood,bill,,,781.76,,2029-02-16,,2029-01-26",
    )
    ledger.derive()

    assert facts(ledger, TYPED_BILL_UNJOINED) == {}


def test_a_bill_sources_manual_debts_never_chain(ledger: Ledger) -> None:
    """A debt to a bill vendor is not a bill: the newer one holds nothing of the older,
    and chaining them would make the older debt vanish from what is owed."""
    yew_account(ledger)
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,manual_debt,first,,977.20,,,,2059-01-01",
        "yew,manual_debt,second,,14.24,,,,2059-03-01",
    )
    ledger.derive()

    found = by_label(ledger)

    assert (found["debt-first"].status, found["debt-second"].status) == ("open", "open")
    assert found["debt-second"].supersedes_id is None
    assert found["bill-2049-07"].status == "open"
    assert owed_total(ledger) == Decimal("1092.84")


def test_a_manual_bill_chains_on_the_account_of_the_parsed_bills_not_the_stood_for_one(
    ledger: Ledger,
) -> None:
    """The issuer's payee stands for one account and its parsed bills are on the other:
    the filed bills say where the running balance is."""
    accounts(
        ledger,
        ("yew", "Yew", "bill_vendor", "999000123", "SAMPLE MOBILE CO"),
        ("second-mobile", "Yew", "bill_vendor", SECOND_YEW_NUMBER, ""),
    )
    ledger.file(YEW, second_account(fixture("yew_bill.txt")))
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,,,981.77,,2049-08-16,,2049-07-26",
    )
    ledger.derive()

    parsed, typed = rows(ledger)

    assert (parsed.status, typed.status) == ("superseded", "open")
    assert typed.supersedes_id == parsed.id
    assert owed_total(ledger) == Decimal("981.77")


def test_a_later_due_date_is_the_newer_of_two_bills_issued_on_one_date(
    ledger: Ledger,
) -> None:
    """The later due date is written first: the order of the rows does not decide it."""
    ledger.manual(
        OBLIGATIONS_FILE,
        "boxwood,bill,,,1172.64,,2059-03-11,,2029-01-26",
        "boxwood,bill,,,781.76,,2029-02-16,,2029-01-26",
    )
    ledger.derive()

    found = by_label(ledger)

    assert (found["bill-2029-02"].status, found["bill-2059-03"].status) == (
        "superseded",
        "open",
    )
    assert found["bill-2059-03"].supersedes_id == found["bill-2029-02"].id
    assert owed_total(ledger) == Decimal("1172.64")


# --- a bill row that names the issuer of an account with its own payee ----------------


def test_a_bill_row_naming_the_issuer_is_read_as_the_standing_payee(
    ledger: Ledger,
) -> None:
    """One bill, parsed under the account's own payee and typed under the issuer's, is
    owed once."""
    yew_account(ledger, "SAMPLE MOBILE CO")
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,,,101.40,,2049-07-19,,2049-06-28",
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert row.payee_id == payee_id(ledger, "mobile")
    assert row.primary_source == YEW
    assert row.manual_source == "manual:obligations.csv:2"
    assert owed_total(ledger) == Decimal("101.40")
    assert facts(ledger, TYPED_BILL_UNJOINED) == {}


def test_a_later_bill_row_naming_the_issuer_chains_under_the_standing_payee(
    ledger: Ledger,
) -> None:
    yew_account(ledger, "SAMPLE MOBILE CO")
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,,,981.77,,2049-08-16,,2049-07-26",
    )
    ledger.derive()

    parsed, typed = rows(ledger)

    assert typed.payee_id == parsed.payee_id == payee_id(ledger, "mobile")
    assert (parsed.status, typed.status) == ("superseded", "open")
    assert owed_total(ledger) == Decimal("981.77")


def test_a_debt_row_naming_the_issuer_stays_on_the_issuers_payee(
    ledger: Ledger,
) -> None:
    """A debt is not one of the account's bills."""
    yew_account(ledger, "SAMPLE MOBILE CO")
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.manual(OBLIGATIONS_FILE, "yew,manual_debt,loan,,977.20,,,,")
    ledger.derive()

    assert by_label(ledger)["debt-loan"].payee_id == payee_id(ledger, "yew")


def test_a_bill_row_naming_an_issuer_with_no_parsed_bill_stays_where_it_was_written(
    ledger: Ledger,
) -> None:
    yew_account(ledger, "SAMPLE MOBILE CO")
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,,,101.40,,2049-07-19,,2049-06-28",
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert row.payee_id == payee_id(ledger, "yew")
    assert facts(ledger, TYPED_BILL_UNJOINED) == {}


def test_a_bill_row_naming_an_issuer_billed_under_two_payees_stays_and_is_counted(
    ledger: Ledger,
) -> None:
    """Either account's bill could be the one it copies: it is not guessed, and status
    says so."""
    accounts(
        ledger,
        ("mobile", "Yew", "bill_vendor", "999000123", "SAMPLE MOBILE CO"),
        (
            "second-mobile",
            "Yew",
            "bill_vendor",
            SECOND_YEW_NUMBER,
            "SAMPLE SECOND CO",
        ),
    )
    ledger.file("yew/a.pdf", yew_bill())
    ledger.file("yew/b.pdf", second_account(yew_bill()))
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,,,101.40,,2049-07-19,,2049-06-28",
    )
    ledger.derive()

    typed = [row for row in rows(ledger) if row.payee_id == payee_id(ledger, "yew")]

    assert len(typed) == 1
    assert len(rows(ledger)) == 3
    assert facts(ledger, TYPED_BILL_UNJOINED) == {"yew": 1}


# --- a manual row and a parsed document -----------------------------------------------


def test_a_manual_row_and_a_parsed_bill_of_one_key_are_one_with_the_documents_figures(
    ledger: Ledger,
) -> None:
    yew_account(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,,,94.51,,2049-07-20,,2049-06-29",
    )
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.amount_due == row.remaining == Decimal("101.40")
    assert row.issued_on == D(2049, 6, 28)
    assert row.due_date == D(2049, 7, 19)
    assert row.primary_source == YEW
    assert row.manual_source == "manual:obligations.csv:2"
    assert row.manual_mismatch == Decimal("-6.89")
    assert facts(ledger, MANUAL_MISMATCH) == {"yew": 1}


def test_a_manual_row_that_agrees_with_its_document_records_no_mismatch(
    ledger: Ledger,
) -> None:
    yew_account(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,,,101.40,,2049-07-19,,2049-06-28",
    )
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.manual_source == "manual:obligations.csv:2"
    assert row.manual_mismatch is None
    assert facts(ledger, MANUAL_MISMATCH) == {}


def test_a_manual_row_merges_with_the_second_bill_of_a_month_by_its_label(
    ledger: Ledger,
) -> None:
    yew_account(ledger)
    ledger.file("yew/a.pdf", yew_bill())
    ledger.file(
        "yew/b.pdf",
        yew_bill("July 05, 2049", "Jul 23, 2049", "July 23, 2049"),
    )
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,bill-2049-07-2,,101.40,,2049-07-23,,2049-07-05",
    )
    ledger.derive()

    found = by_label(ledger)

    assert sorted(found) == ["bill-2049-07", "bill-2049-07-2"]
    assert found["bill-2049-07"].manual_source is None
    assert found["bill-2049-07-2"].manual_source == "manual:obligations.csv:2"


def test_a_merged_row_still_chains_as_its_document_does(ledger: Ledger) -> None:
    yew_account(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "yew,bill,,,101.40,,2049-07-19,,2049-06-28",
    )
    ledger.file("yew/a.pdf", yew_bill())
    ledger.file(
        "yew/b.pdf",
        yew_bill("July 26, 2049", "Aug 16, 2049", "August 16, 2049"),
    )
    ledger.derive()

    merged, newer = rows(ledger)

    assert (merged.status, newer.status) == ("superseded", "open")
    assert newer.supersedes_id == merged.id
    assert owed_total(ledger) == Decimal("101.40")


# --- path (4): arrangements and arrears -----------------------------------------------


def arrangement(ledger: Ledger, payee_slug: str, replaces: str = "") -> None:
    ledger.manual(
        ARRANGEMENTS_FILE,
        f"plan,{payee_slug},2059-01-16,1,38.22,2059-04-01,{replaces}",
        f"plan,{payee_slug},2059-01-16,2,49.52,2059-05-01,{replaces}",
    )


def test_each_instalment_is_an_obligation_issued_when_the_arrangement_was_agreed(
    ledger: Ledger,
) -> None:
    sample_internet(ledger)
    arrangement(ledger, "sample-internet")
    ledger.derive()

    first, second = rows(ledger)

    assert (first.kind, first.period_label) == (
        "arrangement_instalment",
        "inst-plan-1",
    )
    assert second.period_label == "inst-plan-2"
    assert first.issued_on == second.issued_on == D(2059, 1, 16)
    assert first.due_date == D(2059, 4, 1)
    assert (
        first.amount_due == first.threshold_due == first.remaining == Decimal("38.22")
    )
    assert (first.status, first.arrangement_ref) == ("open", "plan")
    assert first.primary_source == "manual:arrangements.csv:2"
    assert first.manual_source is None
    assert owed_total(ledger) == Decimal("87.74")


def test_an_active_arrangement_supersedes_what_it_replaces_and_owes_current_charges(
    ledger: Ledger,
) -> None:
    boxwood_account(ledger)
    arrangement(ledger, "boxwood", "bill-2029-02")
    ledger.file("boxwood/a.pdf", boxwood_bill("January 26, 2029", "February 16, 2029"))
    ledger.file("boxwood/b.pdf", boxwood_bill("February 25, 2029", "March 18, 2029"))
    ledger.derive()

    found = by_label(ledger)

    replaced, current = found["bill-2029-02"], found["bill-2029-03"]
    assert (replaced.status, replaced.arrangement_ref) == ("superseded", "plan")
    assert current.status == "open"
    assert current.arrangement_ref is None
    # The total printed holds what the instalments now cover.
    assert current.amount_due == current.remaining == Decimal("333.84")
    assert owed_total(ledger) == Decimal("421.58")


def test_a_replaced_newest_bill_leaves_only_the_instalments_owed(
    ledger: Ledger,
) -> None:
    boxwood_account(ledger)
    arrangement(ledger, "boxwood", "bill-2029-02")
    ledger.file(BOXWOOD, fixture("boxwood_bill.txt"))
    ledger.derive()

    assert by_label(ledger)["bill-2029-02"].status == "superseded"
    assert owed_total(ledger) == Decimal("87.74")


def test_an_arrangement_replaces_a_manual_bill_too(ledger: Ledger) -> None:
    sample_internet(ledger)
    arrangement(ledger, "sample-internet", "bill-2059-02")
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,,58.55,,2059-02-26,,2059-02-01",
        "sample-internet,bill,,,75.45,,2059-03-26,,2059-03-01",
    )
    ledger.derive()

    found = by_label(ledger)

    assert found["bill-2059-02"].status == "superseded"
    assert found["bill-2059-03"].status == "open"
    assert found["bill-2059-03"].amount_due == Decimal("75.45")


def test_a_manual_row_is_compared_with_the_total_printed_not_the_current_charges(
    ledger: Ledger,
) -> None:
    boxwood_account(ledger)
    arrangement(ledger, "boxwood")
    ledger.manual(
        OBLIGATIONS_FILE,
        "boxwood,bill,,,494.66,,2029-02-16,,2029-01-26",
    )
    ledger.file(BOXWOOD, fixture("boxwood_bill.txt"))
    ledger.derive()

    bill = by_label(ledger)["bill-2029-02"]

    assert bill.amount_due == Decimal("333.84")
    assert bill.manual_mismatch is None


def test_an_arrangement_of_another_payee_changes_nothing(ledger: Ledger) -> None:
    boxwood_account(ledger)
    sample_internet(ledger)
    arrangement(ledger, "sample-internet", "bill-2029-02")
    ledger.file(BOXWOOD, boxwood_with_arrears(fixture("boxwood_bill.txt")))
    ledger.derive()

    found = by_label(ledger)

    assert found["bill-2029-02"].status == "open"
    assert found["bill-2029-02"].amount_due == Decimal("299.22")
    assert found["arrears-2029-01"].amount_due == Decimal("195.44")


def test_a_bill_with_an_arrears_line_splits_and_still_adds_up_to_its_total(
    ledger: Ledger,
) -> None:
    boxwood_account(ledger)
    ledger.file(BOXWOOD, boxwood_with_arrears(fixture("boxwood_bill.txt")))
    ledger.derive()

    found = by_label(ledger)

    arrears, bill = found["arrears-2029-01"], found["bill-2029-02"]
    assert arrears.kind == "arrears"
    # The as-of date is the bill's, and it is due when the bill is.
    assert arrears.issued_on == D(2029, 1, 26)
    assert arrears.due_date == bill.due_date == D(2029, 2, 16)
    assert arrears.amount_due == arrears.threshold_due == Decimal("195.44")
    assert (arrears.status, arrears.primary_source) == ("open", BOXWOOD)
    assert bill.amount_due == Decimal("299.22")
    assert owed_total(ledger) == Decimal("494.66")


def test_a_yew_bill_with_an_arrears_line_splits_too(ledger: Ledger) -> None:
    yew_account(ledger)
    ledger.file(YEW, yew_with_arrears(fixture("yew_bill.txt")))
    ledger.derive()

    found = by_label(ledger)

    assert found["arrears-2049-06"].amount_due == Decimal("33.89")
    assert found["bill-2049-07"].amount_due == Decimal("67.51")
    assert owed_total(ledger) == Decimal("101.40")


def test_a_carried_balance_with_no_arrears_line_stays_in_the_bills_own_total(
    ledger: Ledger,
) -> None:
    yew_account(ledger)
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.kind == "bill"
    assert row.amount_due == Decimal("101.40")


def test_an_active_arrangement_suppresses_arrears_so_never_both_are_owed(
    ledger: Ledger,
) -> None:
    boxwood_account(ledger)
    arrangement(ledger, "boxwood")
    ledger.file(BOXWOOD, boxwood_with_arrears(fixture("boxwood_bill.txt")))
    ledger.derive()

    assert sorted(by_label(ledger)) == ["bill-2029-02", "inst-plan-1", "inst-plan-2"]
    assert by_label(ledger)["bill-2029-02"].amount_due == Decimal("333.84")
    assert owed_total(ledger) == Decimal("421.58")


def test_a_bill_printing_no_current_charges_is_owed_its_total_less_its_arrears(
    ledger: Ledger,
) -> None:
    """With an arrangement active, what it prints is all there is to go on."""
    boxwood_account(ledger)
    arrangement(ledger, "boxwood")
    no_charges = "".join(
        line
        for line in fixture("boxwood_bill.txt").splitlines(keepends=True)
        if not line.strip().startswith("Current charges  ")
    )
    assert no_charges != fixture("boxwood_bill.txt")
    ledger.file(BOXWOOD, no_charges)
    ledger.file("boxwood/arrears.pdf", boxwood_with_arrears(no_charges).replace(
        "January 26, 2029", "February 25, 2029").replace(
        "February 16, 2029", "March 18, 2029"))  # fmt: skip
    ledger.derive()

    found = by_label(ledger)

    assert found["bill-2029-02"].status == "superseded"
    assert found["bill-2029-03"].amount_due == Decimal("299.22")


def test_only_the_newest_bill_states_arrears(ledger: Ledger) -> None:
    """A later bill that prints none has folded them in or had them paid: the older
    figure is not owed on top of it."""
    boxwood_account(ledger)
    ledger.file(
        "boxwood/a.pdf",
        boxwood_with_arrears(boxwood_bill("January 26, 2029", "February 16, 2029")),
    )
    ledger.file("boxwood/b.pdf", boxwood_bill("February 25, 2029", "March 18, 2029"))
    ledger.derive()

    assert sorted(by_label(ledger)) == ["bill-2029-02", "bill-2029-03"]
    assert owed_total(ledger) == Decimal("494.66")


def test_arrears_of_two_accounts_of_one_issuer_are_two_rows(ledger: Ledger) -> None:
    two_yew_accounts(ledger)
    ledger.file("yew/a.pdf", yew_with_arrears(yew_bill()))
    ledger.file("yew/b.pdf", second_account(yew_with_arrears(yew_bill())))
    ledger.derive()

    found = by_label(ledger)

    assert {"arrears-2049-06", "arrears-2049-06-2"} <= set(found)
    assert owed_total(ledger) == Decimal("202.80")


def test_an_arrangement_owes_a_yew_bill_its_printed_current_charges(
    ledger: Ledger,
) -> None:
    yew_account(ledger)
    arrangement(ledger, "yew")
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.derive()

    bill = by_label(ledger)["bill-2049-07"]

    assert bill.amount_due == bill.remaining == Decimal("62.64")
    assert owed_total(ledger) == Decimal("150.38")


def test_a_typed_in_bill_beside_an_active_arrangement_is_owed_as_typed(
    ledger: Ledger,
) -> None:
    """It supersedes the parsed bill before it, which the arrangement replaced, and it
    prints no current charges to read."""
    boxwood_account(ledger)
    arrangement(ledger, "boxwood", "bill-2029-02")
    ledger.file(BOXWOOD, fixture("boxwood_bill.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "boxwood,bill,,,84.82,,2059-03-11,,2059-02-21",
    )
    ledger.derive()

    found = by_label(ledger)

    assert (found["bill-2029-02"].status, found["bill-2059-03"].status) == (
        "superseded",
        "open",
    )
    assert found["bill-2059-03"].amount_due == Decimal("84.82")
    assert found["bill-2059-03"].supersedes_id == found["bill-2029-02"].id
    assert owed_total(ledger) == Decimal("172.56")


def test_a_replaced_typed_in_bill_leaves_only_the_instalments_owed(
    ledger: Ledger,
) -> None:
    boxwood_account(ledger)
    arrangement(ledger, "boxwood", "bill-2029-02")
    ledger.manual(
        OBLIGATIONS_FILE,
        "boxwood,bill,,,781.76,,2029-02-16,,2029-01-26",
    )
    ledger.derive()

    assert by_label(ledger)["bill-2029-02"].status == "superseded"
    assert owed_total(ledger) == Decimal("87.74")


def test_a_replaced_bill_keeps_its_printed_total(ledger: Ledger) -> None:
    """The arrangement rule changes what a live bill is owed, and this one is not."""
    boxwood_account(ledger)
    arrangement(ledger, "boxwood", "bill-2029-02")
    ledger.file(BOXWOOD, fixture("boxwood_bill.txt"))
    ledger.derive()

    assert by_label(ledger)["bill-2029-02"].amount_due == Decimal("494.66")


def boxwood_credited(
    payments: tuple[str, str], forward: str, total: str, arrears: str = ""
) -> str:
    """The Boxwood fixture with larger payments, so its total falls below its current
    charges of $333.84. The identity still holds."""
    return (
        fixture("boxwood_bill.txt")
        .replace("-$114.66", payments[0])
        .replace("-$38.22", payments[1])
        .replace("Balance forward   $160.82", f"Balance forward   {forward}{arrears}")
        .replace("$494.66", total)
    )


def test_current_charges_above_a_credited_total_owe_only_the_total(
    ledger: Ledger,
) -> None:
    boxwood_account(ledger)
    arrangement(ledger, "boxwood")
    ledger.file(
        BOXWOOD, boxwood_credited(("-$488.60", "-$114.36"), "-$289.26", "$44.58")
    )
    ledger.derive()

    bill = by_label(ledger)["bill-2029-02"]

    assert bill.amount_due == bill.remaining == Decimal("44.58")


def test_a_bill_in_credit_under_an_arrangement_stays_as_printed_and_owes_nothing(
    ledger: Ledger,
) -> None:
    boxwood_account(ledger)
    arrangement(ledger, "boxwood")
    ledger.file(
        BOXWOOD, boxwood_credited(("-$781.76", "-$581.98"), "-$1050.04", "-$716.20")
    )
    ledger.derive()

    bill = by_label(ledger)["bill-2029-02"]

    assert bill.amount_due == Decimal("-716.20")
    assert (bill.remaining, bill.status) == (0, "paid")


def test_credited_current_charges_never_make_a_positive_total_a_negative_bill(
    ledger: Ledger,
) -> None:
    boxwood_account(ledger)
    arrangement(ledger, "boxwood")
    ledger.file(
        BOXWOOD,
        fixture("boxwood_bill.txt")
        .replace("Current charges   $333.84", "Current charges   -$3.56")
        .replace("$494.66", "$157.26"),
    )
    ledger.derive()

    bill = by_label(ledger)["bill-2029-02"]

    assert bill.amount_due == 0
    assert bill.status == "paid"


def test_arrears_above_the_total_owe_only_the_total_and_no_negative_bill(
    ledger: Ledger,
) -> None:
    boxwood_account(ledger)
    ledger.file(
        BOXWOOD,
        boxwood_with_arrears(fixture("boxwood_bill.txt")).replace(
            "Arrears   $195.44", "Arrears   $977.20"
        ),
    )
    ledger.derive()

    found = by_label(ledger)

    assert found["arrears-2029-01"].amount_due == Decimal("494.66")
    assert found["bill-2029-02"].amount_due == 0
    assert owed_total(ledger) == Decimal("494.66")


def test_arrears_on_a_bill_in_credit_make_no_row(ledger: Ledger) -> None:
    boxwood_account(ledger)
    ledger.file(
        BOXWOOD,
        boxwood_credited(
            ("-$781.76", "-$581.98"),
            "-$1050.04",
            "-$716.20",
            "\n   Arrears   $195.44",
        ),
    )
    ledger.derive()

    (bill,) = rows(ledger)

    assert bill.kind == "bill"
    assert bill.amount_due == Decimal("-716.20")


def test_a_negative_arrears_line_is_no_arrears(ledger: Ledger) -> None:
    boxwood_account(ledger)
    ledger.file(
        BOXWOOD,
        boxwood_with_arrears(fixture("boxwood_bill.txt")).replace(
            "Arrears   $195.44", "Arrears   -$3.56"
        ),
    )
    ledger.derive()

    (bill,) = rows(ledger)

    assert bill.kind == "bill"
    assert bill.amount_due == Decimal("494.66")


# --- path (5): manual debts -----------------------------------------------------------


def test_a_manual_debt_with_no_due_date_exists_with_null_dates(ledger: Ledger) -> None:
    ledger.manual(PAYEES_FILE, "sample-payee,Sample Payee,person,,,")
    ledger.manual(OBLIGATIONS_FILE, "sample-payee,manual_debt,loan,,977.20,,,,")
    ledger.derive()

    (row,) = rows(ledger)

    assert (row.kind, row.period_label) == ("manual_debt", "debt-loan")
    assert row.due_date is None
    assert row.issued_on is None
    assert row.amount_due == row.threshold_due == row.remaining == Decimal("977.20")
    assert row.status == "open"
    assert row.primary_source == "manual:obligations.csv:2"


def test_a_manual_debt_keeps_its_dates_and_does_not_chain(ledger: Ledger) -> None:
    ledger.manual(PAYEES_FILE, "sample-payee,Sample Payee,person,,,")
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-payee,manual_debt,debt-first,,977.20,,2059-06-01,,2059-01-01",
        "sample-payee,manual_debt,second,,59.73,,,,2059-02-01",
    )
    ledger.derive()

    first, second = rows(ledger)

    assert (first.period_label, second.period_label) == ("debt-first", "debt-second")
    assert (first.issued_on, first.due_date) == (D(2059, 1, 1), D(2059, 6, 1))
    assert (first.status, second.status) == ("open", "open")
    assert owed_total(ledger) == Decimal("1036.93")


# --- the five paths and no sixth ------------------------------------------------------


def test_one_derive_over_every_source_makes_exactly_the_five_paths(
    ledger: Ledger,
) -> None:
    accounts(
        ledger,
        ("neem", "Alder", "credit_card", CARD, ""),
        ("mobile", "Yew", "bill_vendor", "999000123", ""),
        ("utility", "Boxwood", "bill_vendor", "9990001234", ""),
    )
    ledger.manual(
        PAYEES_FILE,
        "sample-internet,Sample Internet,vendor,,,",
        "sample-payee,Sample Payee,person,,,",
        "cloud,Cloud Vendor,vendor,SAMPLE PHARMACY,,",
    )
    ledger.file(NEWER_NEEM_STATEMENT, fixture("alder_neem_statement_no_minimum.txt"))
    ledger.file(YEW, fixture("yew_bill.txt"))
    ledger.file(BOXWOOD, boxwood_with_arrears(fixture("boxwood_bill.txt")))
    ledger.file("_mail/invoice.eml", "From: billing@example.test\n\nSample invoice\n")
    ledger.file("alder-neem/2048-02-22.csv", fixture("alder_neem_export.csv"))
    ledger.file("invoices-received/sample/invoice.pdf", fixture("invoice_received.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,,58.55,,2050-02-25,_mail/invoice.eml,2049-06-01",
        "sample-payee,manual_debt,loan,,552.00,,,,",
    )
    arrangement(ledger, "yew", "bill-2049-07")
    ledger.derive()

    kinds = sorted((str(row.kind), str(row.period_label)) for row in rows(ledger))

    assert kinds == [
        ("arrangement_instalment", "inst-plan-1"),
        ("arrangement_instalment", "inst-plan-2"),
        ("arrears", "arrears-2029-01"),
        ("bill", "bill-2029-02"),
        ("bill", "bill-2049-07"),
        ("bill", "bill-2050-02"),
        ("card_statement", "card-2050-03-12"),
        ("manual_debt", "debt-loan"),
    ]


def test_only_stage_14_inserts_an_obligation() -> None:
    """Any call of ``insert(obligation)`` in the package, whatever the import."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "name", getattr(node.func, "id", "")) == "insert"
                and any(
                    isinstance(arg, ast.Name) and arg.id == "obligation"
                    for arg in node.args
                )
            ):
                found.append(path.relative_to(SRC).as_posix())

    assert found == ["derive/stage14.py"]
