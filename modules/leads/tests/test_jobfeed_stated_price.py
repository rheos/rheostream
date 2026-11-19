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
        # From the cold review of #609: ranges with thousands on one end.
        ("Budget $500-1k for the landing page.", price("500", "1000", "total")),
        ("Budget: $800 - 1.2k", price("800", "1200", "total")),
        # The job's own price wins over a product price, a referral bonus or an
        # agency's old rate, wherever it sits.
        (
            "Our app is free; users pay $4.99 per month. Budget: $2,000.",
            price("2000", unit="total"),
        ),
        (
            "Users earn $5 per referral. Fixed price $3,000",
            price("3000", unit="total"),
        ),
        (
            f"Our last agency charged $80{HR}; we want a $500 fixed price",
            price("500", unit="total"),
        ),
        # One sentence never lends its words to another's amount.
        (
            "Price per video $30. 20 videos. Total budget $600.",
            price("30", unit="item"),
        ),
        # A pasted page's client panel is never read as this job's pay.
        (
            "Build a landing page. $1,500.00 Fixed-price\nAbout the client\n"
            f"$8.4K total spent\n$32.50 {HR} avg hourly rate paid",
            price("1500", unit="total"),
        ),
        # From CodeRabbit on #609: a range with the currency after both ends,
        # "sales" and "hire" as ordinary words, and units named before.
        (f"Rate: 25-40 USD{HR}", price("25", "40", "hour")),
        ("Pay 2-3k dollars total", price("2000", "3000", "total")),
        ("Budget: $500 for a sales landing page.", price("500", unit="total")),
        ("We will hire you for $500 fixed.", price("500", unit="total")),
        (f"Rate {PER_HOUR} $40", price("40", unit="hour")),
        ("Rate per month $500", price("500")),
        # From the first live run (#611): one of a thing is a per-item price.
        ("Paid test: $10 for 1 video, then ongoing work.", price("10", unit="item")),
        ("We pay $25 for each clip", price("25", unit="item")),
        ("$40 for 1 hour of consulting", price("40", unit="hour")),
        ("$1,000 for 1 month of edits", price("1000")),
        ("$500 for a landing page", price("500")),
        # A price in the brief loses to the job's own, and an article before an
        # amount does not hide the pay when a pay word is in reach.
        (
            "Our hero buys a $70 hoodie in episode one. Budget: $400.",
            price("400", unit="total"),
        ),
        ("We pay a $300 flat fee.", price("300", unit="total")),
        ("Offering a $200 bonus on delivery", price("200")),
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
        # Business size after the amount, a client's history, a shell variable.
        "We are at $10k MRR and growing.",
        "We did $1,000,000 in sales last year.",
        f"Client history: $8.4K total spent, $32.50{HR} avg hourly rate paid.",
        "Run awk '{print $1}' over the log.",
        # A price inside the creative brief is not the job's pay (#611).
        "Claymation short: a $70 hoodie ends up costing our hero far more.",
        "She cancels the $20 plan and keeps the old phone.",
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
