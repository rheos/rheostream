"""The shipped pairing defaults (settings.py, ADR-0012), set at the Phase 3b gate.

The other derive tests pin the design spec's worked examples (``support.RULE_SETTINGS``:
a 5.00 CAD floor, a 7-day window, a 5 percent spread) so they read as the spec does.
These drop that pin and derive with the defaults themselves: no absolute floor (the
1.5 percent alone), a 5-day transfer window, a 6 percent conversion spread. Seams as
in the rule tests: archive files in, ``derive.run``, the ``pair_edge`` rows and fee
lines out. Every date and amount is invented.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from rheo_finance.settings import parse_settings

from .support import CSV_IDENTIFIER, FIXTURES, Ledger
from .test_conversions import HAZEL, edges, fee_lines, usd_rate
from .test_resolution import register
from .test_transfer_edges import (
    BILL_PAYMENT,
    PAYMENT_RECEIVED,
    file_bank,
    file_card,
    on,
)
from .test_transfer_edges import edges as transfer_edges

D = dt.date


@pytest.fixture
def ledger(ledger: Ledger) -> Ledger:
    """The derive ledger with the shipped pairing defaults: only its paths set."""
    root = ledger.archive.parent
    ledger.ctx = dataclasses.replace(
        ledger.ctx,
        settings=parse_settings(
            f'[finance.paths]\narchive = "{ledger.archive}"\n'
            f'downloads = "{root / "downloads-unset"}"\n'
        ),
    )
    return ledger


@pytest.mark.parametrize(
    ("paid", "received", "edge"),
    [
        ("22.32", "21.99", True),
        ("22.32", "21.98", False),
        ("22.32", "17.32", False),
        ("3.35", "5.08", False),
    ],
    ids=["inside-1.5pct", "just-outside", "inside-a-5-floor", "small-lines"],
)
def test_the_default_tolerance_is_the_percent_alone(
    own: Ledger, paid: str, received: str, edge: bool
) -> None:
    # 1.5 percent of 22.32 is 0.33. Two small lines of different amounts are two
    # movements, not one with a fee, so no CAD floor joins them.
    sent, arrived = D(2047, 6, 7), D(2047, 6, 9)
    file_bank(own, *on(CSV_IDENTIFIER, (sent, BILL_PAYMENT, -Decimal(paid))))
    file_card(own, (arrived, PAYMENT_RECEIVED, Decimal(received)))

    own.derive()

    expected = [(sent, -Decimal(paid), arrived, Decimal(received))]
    assert transfer_edges(own) == (expected if edge else [])


@pytest.mark.parametrize(("days", "edge"), [(5, True), (6, False)])
def test_the_default_transfer_window_is_five_days(
    own: Ledger, days: int, edge: bool
) -> None:
    sent = D(2047, 6, 7)
    arrived = sent + dt.timedelta(days=days)
    file_bank(own, *on(CSV_IDENTIFIER, (sent, BILL_PAYMENT, Decimal("-217.77"))))
    file_card(own, (arrived, PAYMENT_RECEIVED, Decimal("217.77")))

    own.derive()

    expected = [(sent, Decimal("-217.77"), arrived, Decimal("217.77"))]
    assert transfer_edges(own) == (expected if edge else [])


@pytest.mark.parametrize(
    ("rate", "fee"),
    [
        ("1.3131", "-2.45"),  # 42.19 CAD in: a 5.5 percent spread pairs
        ("1.3059", None),  # 41.96 CAD in: over 6 percent, no pair
    ],
    ids=["5.5pct", "over-6pct"],
)
def test_the_default_conversion_spread_tolerance_is_six_percent(
    ledger: Ledger, rate: str, fee: str | None
) -> None:
    # The fixture converts 44.64 CAD to 32.13 USD on one day.
    day = D(2030, 11, 3)
    register(ledger, HAZEL)
    usd_rate(ledger, rate, on=day)
    ledger.file(
        "hazel/activity-2030-11-20.csv",
        (FIXTURES / "hazel_activity.csv").read_text(encoding="utf-8"),
    )

    ledger.derive()

    assert [applied for _, _, applied, _ in edges(ledger)] == (
        [True] if fee is not None else []
    )
    expected = [] if fee is None else [("derived_fee", Decimal(fee), "CAD")]
    assert fee_lines(ledger, day) == expected
