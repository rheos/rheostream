"""The owed list's read-time figures for a reported paid row (#639): what it owes,
what it carries and whether it was unconfirmed. No database; every figure invented."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from rheo_finance.reports.owed import Facts, figures

TODAY = dt.date(2055, 4, 20)
BASIS = "finance.decision:01900000-0000-7000-8000-000000000001"


def card(**changes: object) -> Facts:
    base: dict[str, object] = {
        "obligation_ref": "finance.obligation:01900000-0000-7000-8000-000000000002",
        "payee_slug": "test-card",
        "payee_label": "Test Card",
        "kind": "card_statement",
        "period_label": "card-2055-04-05",
        "statement_date": dt.date(2055, 4, 5),
        "issued_on": dt.date(2055, 4, 5),
        "amount_due": Decimal("500.00"),
        "minimum_due": Decimal("25.00"),
        "threshold_due": Decimal("25.00"),
        "paid": Decimal("0.00"),
        "remaining": Decimal("500.00"),
        "threshold_remaining": Decimal("25.00"),
        "due_date": dt.date(2055, 4, 30),
        "status": "open",
        "primary_source": "statements/test-card/2055-04-05.pdf",
        "manual_source": None,
        "before_floor": False,
        "payments_unmatchable": False,
        "manual_mismatch": None,
        "payments_mismatch": None,
        "statement_conflict": False,
        "invoice_ref": None,
        "arrangement_ref": None,
        "supersedes_ref": None,
        "presumed_paid_basis": None,
    }
    return Facts.model_validate(base | changes)


def test_a_reported_card_counts_as_paid_until_its_statements_say_otherwise() -> None:
    """Presumed paid reads the whole statement as paid, as it always has; the real
    payments take over once they apply."""
    row = figures(card(status="presumed_paid", presumed_paid_basis=BASIS), TODAY, 14)
    zero = Decimal("0.00")
    assert (row.due_in_window, row.overdue, row.remaining, row.carried) == (zero,) * 4
    assert row.paid == Decimal("500.00")
    assert row.reported_unconfirmed is False
    assert row.reminder_eligible is False


@pytest.mark.parametrize(
    ("status", "threshold", "unconfirmed"),
    [
        ("open", "25.00", True),
        ("partial", "10.00", True),
        # The minimum was met: the report was confirmed, whatever the balance.
        ("partial", "0.00", False),
        ("presumed_paid", "25.00", False),
    ],
)
def test_unconfirmed_is_a_named_decision_with_something_still_owed(
    status: str, threshold: str, unconfirmed: bool
) -> None:
    row = figures(
        card(
            status=status,
            presumed_paid_basis=BASIS,
            threshold_remaining=Decimal(threshold),
        ),
        TODAY,
        14,
    )
    assert row.reported_unconfirmed is unconfirmed


def test_no_decision_is_never_unconfirmed() -> None:
    assert figures(card(), TODAY, 14).reported_unconfirmed is False
