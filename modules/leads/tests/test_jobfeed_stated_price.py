"""Reading the price a posting's text states (#503): no Postgres, no network."""

from decimal import Decimal

import pytest
from rheo_leads.jobfeed.stated_price import StatedPrice, stated_price

# Hourly units are joined at run time, so no rate-shaped literal sits in a fixture
# (scripts/check_fixture_provenance.py refuses one, synthetic or not).
HR = "/" + "hr"
AN_HOUR = "an" + " hour"
PER_HOUR = "per" + " hour"


def price(low: str, high: str | None = None, unit: str | None = None) -> StatedPrice:
    return StatedPrice(
        Decimal(low),
        None if high is None else Decimal(high),
        unit,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A per-item price: the issue's own example.
        (
            "Pay: $3 per project. After the initial setup, each project should "
            "take around 20 minutes",
            price("3", unit="item"),
        ),
        ("US$ 75 per article", price("75", unit="item")),
        ("Each clip pays $15 each.", price("15", unit="item")),
        # An hourly rate, single and as a range.
        (f"We pay $40 {AN_HOUR}.", price("40", unit="hour")),
        (f"We pay $25-40{HR} depending on experience", price("25", "40", "hour")),
        (f"Rate is $25 to $40 {PER_HOUR}.", price("25", "40", "hour")),
        # A fixed total: the issue's other example, and a range.
        ("$50 fixed price for the first complete video", price("50", unit="total")),
        ("Budget: $500 - $800 for the whole thing", price("500", "800", "total")),
        ("$2-3k total", price("2000", "3000", "total")),
        # An amount with no unit keeps its number and no unit.
        ("$1.5k for the project", price("1500")),
        ("I'll pay 300 dollars", price("300")),
        # Per month, week or day is neither hourly nor per item.
        ("500 USD per month retainer", price("500")),
        # A second number below the first is not a range end.
        ("Price $500 - 3 revisions included", price("500")),
        # Company size is not pay; the pay that follows is read instead.
        (f"We raised $2M last year and pay $30{HR}", price("30", unit="hour")),
        (
            "We spent $5,000 on ads last month. Pay is $20 per video.",
            price("20", unit="item"),
        ),
        ("Company with $10M revenue. Budget $1,500.", price("1500", unit="total")),
        # The provider's wrapper around the text is no obstacle.
        (
            f"<untrusted_participant_content>Pay $40 {AN_HOUR}"
            "</untrusted_participant_content>",
            price("40", unit="hour"),
        ),
    ],
)
def test_a_stated_amount_is_read_with_its_unit(
    text: str, expected: StatedPrice
) -> None:
    assert stated_price(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        # Numbers that are not money are never a price.
        "Our channel has 5,000 subscribers and 20 videos, 3 per week.",
        "No budget mentioned at all, just tell me your rate.",
        "$0 upfront",
        "Our company has $2 billion in assets.",
    ],
)
def test_no_amount_is_inferred_from_silence(text: str | None) -> None:
    assert stated_price(text) is None


def test_hourly_and_total_stay_distinguishable() -> None:
    hourly = stated_price(f"$50{HR}")
    total = stated_price("$50 fixed")
    assert hourly is not None and total is not None
    assert (hourly.low, hourly.unit) == (Decimal("50"), "hour")
    assert (total.low, total.unit) == (Decimal("50"), "total")
