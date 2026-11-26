"""Upcoming charges (#637): the series rules on fictional lines, then the operation
over the real reader DSN."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, text
from sqlalchemy.pool import NullPool

from rheo_finance import catalogue, operations
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext, FinancePaths
from rheo_finance.contracts import Role
from rheo_finance.reports import upcoming as u
from rheo_finance.settings import parse_settings
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import (
    account,
    document,
    document_line,
    obligation,
    open_item,
    payee,
    payment,
    txn,
    txn_source,
)
from tests.finance_support import DatabaseUnderTest, is_scratch_database

D = dt.date
M = Decimal
TODAY = D(2061, 3, 20)


def out(
    label: str, day: dt.date, amount: str, account: str = "card", currency: str = "CAD"
) -> u.Outflow:
    return u.Outflow(account, account.title(), day, M(amount), currency, label)


def monthly(
    label: str, first: dt.date, count: int, amount: str = "9.99", **kw: str
) -> list[u.Outflow]:
    return [out(label, u.add_months(first, n), amount, **kw) for n in range(count)]


def predict(
    lines: list[u.Outflow],
    through: dict[str, dt.date] | None = None,
    today: dt.date = TODAY,
    days: int = 30,
) -> list[u.Charge]:
    ends = through or {o.account: max(x.posted for x in lines) for o in lines}
    return u.predicted(u.runs(lines), ends, today, days, 3)


def test_add_months_keeps_the_day_or_takes_the_month_end() -> None:
    assert u.add_months(D(2061, 1, 31), 1) == D(2061, 2, 28)
    assert u.add_months(D(2061, 12, 15), 1) == D(2062, 1, 15)
    assert u.add_months(D(2060, 2, 29), 12) == D(2061, 2, 28)


def test_a_label_that_drifts_between_statements_stays_one_series() -> None:
    assert u.label_key("Sample Stream 555-0100 Ca 90210") == "sample stream"
    assert u.label_key("Sample Stream 555-0123 On") == "sample stream"
    keys = u.joined_keys(["sample stream", "sample stream springfield", "other shop"])
    assert keys["sample stream springfield"] == "sample stream"
    assert keys["other shop"] == "other shop"
    lines = [
        out("Sample Stream 555-0100 Ca 90210", D(2060, 12, 4), "7.45"),
        out("Sample Stream Springfield Bc 00000", D(2061, 1, 4), "7.45"),
        out("Sample Stream 555-0123 On", D(2061, 2, 4), "7.45"),
        out("Sample Stream 555-0123 On", D(2061, 3, 4), "7.45"),
    ]
    rows = predict(lines)
    assert [(r.on, r.amount, r.seen) for r in rows] == [(D(2061, 4, 4), M("7.45"), 4)]
    assert rows[0].label == "Sample Stream 555-0123 On"
    assert rows[0].state == "expected" and rows[0].cadence == "monthly"


def test_one_vendor_with_two_plans_is_two_series() -> None:
    lines = monthly("Sample Cloud Ca", D(2060, 11, 14), 5, "3.49") + monthly(
        "Sample Cloud Ca", D(2060, 11, 14), 5, "12.49"
    )
    rows = predict(lines, {"card": D(2061, 3, 18)})
    assert sorted(r.amount for r in rows) == [M("3.49"), M("12.49")]


def test_interest_that_moves_back_and_forth_is_one_series() -> None:
    amounts = ["41.10", "47.80", "41.35", "46.20", "44.90", "41.60"]
    lines = [
        out("Purchase Interest", u.add_months(D(2060, 10, 5), n), a)
        for n, a in enumerate(amounts)
    ]
    rows = predict(lines)
    assert len(rows) == 1
    only = rows[0]
    assert (only.on, only.amount, only.seen) == (D(2061, 4, 5), M("41.60"), 6)
    assert (only.amount_low, only.amount_high) == (M("41.60"), M("46.20"))


def test_shopping_is_not_regular_enough() -> None:
    days = [1, 3, 9, 10, 18, 30, 33, 41, 47, 60]
    lines = [
        out("Corner Grocery", D(2061, 1, 1) + dt.timedelta(d), "40.00") for d in days
    ]
    assert predict(lines) == []


def test_a_charge_that_stopped_inside_the_loaded_data_drops_out() -> None:
    lines = monthly("Sample Tool", D(2060, 6, 9), 6)
    assert predict(lines, {"card": D(2061, 3, 5)}) == []


def test_an_account_whose_recent_statements_are_missing_keeps_its_charges() -> None:
    lines = monthly("Rent", D(2060, 10, 2), 4, "1200.00", account="bank")
    rows = predict(lines, {"bank": D(2061, 1, 30)})
    assert [(r.on, r.state) for r in rows] == [
        (D(2061, 2, 2), "likely_charged"),
        (D(2061, 3, 2), "likely_charged"),
        (D(2061, 4, 2), "expected"),
    ]


def test_an_account_with_no_recent_data_predicts_nothing() -> None:
    lines = monthly("Old Plan", D(2060, 1, 2), 6, account="closed")
    assert predict(lines, {"closed": D(2060, 6, 2)}) == []


def test_a_charge_past_the_data_but_before_today_is_probably_charged() -> None:
    lines = monthly("Sample Host Ltd", D(2060, 11, 12), 4, "27.40")
    rows = predict(lines, {"card": D(2061, 3, 5)})
    assert [(r.on, r.state, r.days_away) for r in rows] == [
        (D(2061, 3, 12), "likely_charged", -8),
        (D(2061, 4, 12), "expected", 23),
    ]


def test_a_label_with_no_letters_takes_nothing_with_it() -> None:
    lines = monthly("Sample Stream Ca", D(2060, 12, 4), 4)
    lines.append(out("000123", D(2061, 2, 9), "3.00"))
    lines.append(out("#4471 - 0099", D(2061, 2, 11), "3.00"))
    assert [r.on for r in predict(lines)] == [D(2061, 4, 4)]


def test_a_one_word_label_does_not_swallow_a_longer_one() -> None:
    lines = monthly("Sample Stream Plus", D(2060, 9, 20), 6)
    lines.append(out("Sample", D(2061, 1, 20), "10.49"))
    assert [(r.on, r.label) for r in predict(lines)] == [
        (D(2061, 3, 20), "Sample Stream Plus")
    ]


def test_a_price_rise_carries_the_series_on() -> None:
    lines = monthly("Sample Stream Ca", D(2060, 2, 6), 12)
    lines += monthly("Sample Stream Ca", D(2061, 2, 6), 2, "11.99")
    rows = predict(lines)
    assert [(r.on, r.amount, r.seen) for r in rows] == [(D(2061, 4, 6), M("11.99"), 14)]
    assert (rows[0].amount_low, rows[0].amount_high) == (M("9.99"), M("11.99"))


def test_a_step_the_loaded_data_already_covers_is_not_shown() -> None:
    lines = monthly("Sample Gym", D(2060, 11, 10), 4)
    rows = predict(lines, {"card": D(2061, 3, 12)})
    assert [(r.on, r.state) for r in rows] == [(D(2061, 4, 10), "expected")]


def test_too_few_charges_or_a_same_day_repeat_is_not_a_series() -> None:
    assert predict(monthly("Sample New", D(2061, 1, 10), 2)) == []
    lines = monthly("Sample Dup", D(2060, 12, 10), 4)
    lines.append(out("Sample Dup", D(2061, 3, 10), "9.99"))
    assert predict(lines) == []


def test_a_weekly_charge_repeats_inside_the_window() -> None:
    lines = [
        out("Sample Box", D(2061, 2, 20) + dt.timedelta(7 * n), "12.00")
        for n in range(4)
    ]
    rows = predict(lines, days=14)
    assert [r.on for r in rows] == [D(2061, 3, 20), D(2061, 3, 27), D(2061, 4, 3)]


# --- the operation over the reader ------------------------------------------------

NOW = dt.datetime(2061, 3, 20, 18, tzinfo=dt.UTC)


def key(label: str) -> uuid.UUID:
    return derived_id(D(2061, 1, 1), ("synthetic_upcoming", label))


@pytest.fixture(scope="module")
def seeded(scratch_database: DatabaseUnderTest) -> Iterator[DatabaseUnderTest]:
    engine = create_engine(scratch_database.owner_url, poolclass=NullPool)
    with engine.begin() as conn:
        conn.execute(
            insert(account).values(
                id=key("acct"),
                slug="sample-card",
                label="Sample Card",
                institution="Sample",
                kind="credit_card",
                currencies=["CAD"],
                cadence="monthly",
                sourced_by="documents",
            )
        )
        conn.execute(
            insert(document).values(
                id=key("doc"),
                path="synthetic/upcoming.txt",
                kind="statement",
                issuer="synthetic",
                format="txt",
                status="parsed",
                imported_at=NOW,
                sha256=hashlib.sha256(b"synthetic upcoming").hexdigest(),
            )
        )
        # (label, first day, amount, printed label, transfer state, exclusion,
        # currency, movement kind); only the stream may become a prediction.
        series = [
            ("stream", D(2060, 11, 4), "-7.45", "Sample Stream Ca", None, None),
            ("move", D(2060, 11, 8), "-50.00", "Sample Move", "paired", None),
            ("spam", D(2060, 11, 9), "-5.00", "Sample Spam", None, "spam"),
            ("bare", D(2060, 11, 10), "-6.00", None, None, None),
            ("coin", D(2060, 11, 11), "-0.001", "Sample Coin Fee", None, None),
            ("utility", D(2060, 11, 12), "-74.10", "Sample Utility Pay", None, None),
        ]
        n = 0
        for name, first, amount, printed, state, exclusion in series:
            for step in range(5):
                n += 1
                label, day = f"{name}-{step}", u.add_months(first, step)
                crypto = name == "coin"
                # A line's id is its primary source's line key, as stage 3 makes it.
                conn.execute(
                    insert(txn).values(
                        id=key(label),
                        account_id=key("acct"),
                        posting_date=day,
                        amount=M(amount),
                        currency="XBT" if crypto else "CAD",
                        origin="document_line",
                        movement_kind="crypto_fee" if crypto else "fiat",
                        display_label=printed,
                        transfer_state=state,
                        exclusion=exclusion,
                    )
                )
                conn.execute(
                    insert(document_line).values(
                        id=key(label),
                        document_id=key("doc"),
                        line_no=n,
                        line_kind="transaction",
                        posting_date=day,
                        account_id=key("acct"),
                        amount=M(amount),
                        currency="CAD",
                        line_key_id=key(label),
                        description_raw=printed or "",
                    )
                )
                conn.execute(
                    insert(txn_source).values(
                        txn_id=key(label),
                        document_line_id=key(label),
                        line_key_id=key(label),
                        role="primary",
                    )
                )
        conn.execute(
            insert(payee).values(
                id=key("payee"),
                slug="sample-utility",
                display_label="Sample Utility",
                kind="vendor",
                patterns=["SAMPLE UTILITY"],
                paid_from=[],
            )
        )
        for label, due in (("soon", D(2061, 3, 30)), ("late", D(2061, 3, 1))):
            conn.execute(
                insert(obligation).values(
                    id=key(label),
                    payee_id=key("payee"),
                    kind="bill",
                    period_label=f"bill-{label}",
                    statement_date=D(2061, 3, 1),
                    issued_on=D(2061, 3, 1),
                    amount_due=M("74.10"),
                    threshold_due=M("74.10"),
                    paid=M("0"),
                    remaining=M("74.10"),
                    threshold_remaining=M("74.10"),
                    due_date=due,
                    status="open",
                    primary_source=f"sample-bills/{label}.pdf",
                    before_floor=False,
                )
            )
        # The utility's payments are unapplied credits: Owed counts its bills, so
        # they are not predicted. A credit names no line; account, day and amount do.
        for step in range(5):
            label, day = f"utility-{step}", u.add_months(D(2060, 11, 12), step)
            conn.execute(
                insert(payment).values(
                    id=key(f"{label}-pay"),
                    payee_id=key("payee"),
                    amount=M("74.10"),
                    currency="CAD",
                    paid_on=day,
                    source="money",
                    state="active",
                )
            )
            conn.execute(
                insert(open_item).values(
                    id=key(f"{label}-item"),
                    tax_year=day.year,
                    kind="unapplied_credit",
                    subject_ref=f"finance.payment_application:{key(label + '-app')}",
                    payment_id=key(f"{label}-pay"),
                    occurred_on=day,
                    account_id=key("acct"),
                    direction="out",
                    amount=M("-74.10"),
                    currency="CAD",
                    state="unapplied",
                    reason_code="no_target",
                    reason_payee_id=key("payee"),
                )
            )
    yield scratch_database
    with engine.begin() as conn:
        assert is_scratch_database(scratch_database.owner_url.database)
        conn.execute(
            text("TRUNCATE finance.payee, finance.document, finance.account CASCADE")
        )
    engine.dispose()


def test_the_operation_lists_bills_and_predictions_by_date_under_the_reader(
    seeded: DatabaseUnderTest, tmp_path: Path
) -> None:
    catalogue.load_all()
    ctx = operations.with_databases(
        FinanceContext(
            settings=parse_settings(""),
            clock=FakeClock(NOW),
            paths=FinancePaths(data=tmp_path),
            role=Role.MEMBER,
            snapshot_provider=lambda _uow, _ref: None,
        ),
        owner_url=seeded.owner_url.set(port=1),
        reader_url=seeded.reader_url,
    )
    result = operations.dispatch("finance.upcoming.list", {}, Role.MEMBER, ctx=ctx)
    assert isinstance(result, u.ListOut)
    assert [(r.kind, r.on, r.label, r.amount) for r in result.rows] == [
        ("due", D(2061, 3, 30), "Sample Utility", M("74.10")),
        ("expected", D(2061, 4, 4), "Sample Stream Ca", M("7.45")),
    ]
    assert result.rows[1].account == "sample-card"
    assert result.overdue_count == 1
    assert [(t.currency, t.amount) for t in result.totals] == [("CAD", M("81.55"))]
    assert [(a.account, a.data_through, a.predicts) for a in result.accounts] == [
        ("sample-card", D(2061, 3, 12), True)
    ]
    with pytest.raises(operations.InputInvalid):
        operations.dispatch("finance.upcoming.list", {"days": 0}, Role.MEMBER, ctx=ctx)
