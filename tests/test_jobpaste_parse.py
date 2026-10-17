"""The job paste parsers: synthetic tiles in the three search-page markups and a
synthetic full posting. Pure functions, no database."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from rheo_leads.jobpaste.parse import (
    POSTING_LIMIT,
    PastedJob,
    parse_posting,
    parse_search_page,
    posting_link,
    proposal_band,
)

HOSTS = ("www.example.com", "example.com")
TODAY = date(2026, 3, 10)
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)

LEGACY = """
<section class="air3-card-section air3-card-hover p-4">
  <h3><a data-ev-label="link" href="/jobs/Example-data-pipeline_~02111?referrer=search">
    Example data pipeline</a></h3>
  <small><strong data-test="job-type">Hourly: $25-$40</strong> - Intermediate</small>
  <span data-test="posted-on">3 hours ago</span>
  <span data-test="payment-verification-status">Payment verified</span>
  <span data-test="client-spendings">
    <strong data-test="formatted-amount">$10K+</strong></span>
  <span data-test="js-feedback">
    <span class="sr-only">Rating is 4.8 out of 5.</span></span>
  <span data-test="proposals-tier">10 to 15</span>
  <span data-test="job-description-text">A short snippet of the posting.</span>
</section>
"""

ARTICLE = """
<article data-test="JobTile">
  <a data-test="job-tile-title-link UpLink"
     href="/jobs/Example-dashboard_~02222/">Example dashboard</a>
  <div data-test="job-pubilshed-date"><span>Posted 2 days ago</span></div>
  <span data-test="job-type-label">Fixed price</span>
  <span data-test="is-fixed-price">Est. budget: $1,500.00</span>
  <div data-test="payment-verified">Unverified Payment unverified</div>
  <span data-test="total-spent">$2K+ spent</span>
  <span data-test="total-feedback">
    <span class="sr-only">Rating is 0 out of 5.</span></span>
  <span data-test="proposals-tier">Proposals: Less than 5</span>
</article>
"""

FEED = """
<div data-test="job-tile">
  <div class="header">
    <h3 data-test="job-title">Example API integration</h3>
    <span>Posted yesterday</span>
    <span>Hourly: $30</span>
  </div>
  <span data-ev-opening_uid="333"></span>
  <p>A feed card snippet.</p>
  <div class="footer"><span>Payment verified</span>
    <span data-test="rating-minimal">4.5</span></div>
  <span data-test="proposals-tier">50+</span>
</div>
"""


def test_each_markup_yields_one_job_keyed_by_its_posting_id() -> None:
    parsed = parse_search_page(LEGACY + ARTICLE + FEED, HOSTS, today=TODAY)
    assert parsed.skipped == 0 and parsed.warnings == ()
    legacy, article, feed = parsed.jobs
    assert legacy == PastedJob(
        posting_id="~02111",
        posting_url="https://www.example.com/jobs/~02111",
        title="Example data pipeline",
        job_type="hourly",
        budget_min=Decimal(25),
        budget_max=Decimal(40),
        proposal_count=15,
        proposal_count_exact=False,
        posted_on=TODAY,
        client_payment_verified=True,
        client_spend=Decimal(10000),
        client_rating=Decimal("4.8"),
    )
    assert article.posting_id == "~02222"
    assert article.posting_url == "https://www.example.com/jobs/~02222"
    assert (article.job_type, article.budget_min, article.budget_max) == (
        "fixed",
        None,
        Decimal("1500.00"),
    )
    assert article.posted_on == date(2026, 3, 8)
    assert article.client_payment_verified is False
    assert article.client_spend == Decimal(2000)
    assert article.client_rating == Decimal(0)
    assert (article.proposal_count, article.proposal_count_exact) == (4, False)
    # The feed card has no link: the opening uid gives the same ~02 posting id.
    assert feed.posting_id == "~02333"
    assert feed.title == "Example API integration"
    assert feed.posted_on == date(2026, 3, 9)
    assert (feed.job_type, feed.budget_max) == ("hourly", Decimal(30))
    assert feed.client_payment_verified is True
    assert feed.client_rating == Decimal("4.5")
    assert (feed.proposal_count, feed.proposal_count_exact) == (51, False)


def test_a_tile_snippet_is_never_a_description() -> None:
    parsed = parse_search_page(LEGACY + FEED, HOSTS, today=TODAY)
    assert all(job.description is None for job in parsed.jobs)
    assert all("description" not in job.to_body(NOW) for job in parsed.jobs)


def test_unknown_stays_unknown_in_the_body() -> None:
    bare = (
        '<article data-test="JobTile">'
        '<a data-test="job-tile-title-link" href="/jobs/~02444">Bare tile</a>'
        "</article>"
    )
    (job,) = parse_search_page(bare, HOSTS, today=TODAY).jobs
    assert job.client_payment_verified is None
    assert job.to_body(NOW) == {
        "title": "Bare tile",
        "posting_url": "https://www.example.com/jobs/~02444",
        "posting_id": "~02444",
        "observed_at": NOW.isoformat(),
    }


def test_the_body_uses_the_feed_value_forms() -> None:
    (job,) = parse_search_page(LEGACY, HOSTS, today=TODAY).jobs
    body = job.to_body(NOW)
    assert body["budget_min"] == "25" and body["budget_max"] == "40"
    assert body["client_spend"] == "10000"
    assert body["client_payment_verified"] == "true"
    assert body["proposal_count"] == 15
    assert body["proposal_count_exact"] == "false"
    assert body["proposal_observed_at"] == NOW.isoformat()
    assert body["posted_on"] == TODAY.isoformat()
    assert "applied" not in body


def test_a_tile_off_the_job_hosts_or_without_a_title_is_skipped() -> None:
    elsewhere = LEGACY.replace('href="/jobs/', 'href="https://jobs.example.net/jobs/')
    untitled = (
        '<article data-test="JobTile">'
        '<a data-test="job-tile-title-link" href="/jobs/~02555"></a></article>'
    )
    parsed = parse_search_page(elsewhere + untitled + ARTICLE, HOSTS, today=TODAY)
    assert [job.posting_id for job in parsed.jobs] == ["~02222"]
    assert parsed.skipped == 2
    assert len(parsed.warnings) == 2


def test_a_posting_twice_in_one_page_counts_once() -> None:
    parsed = parse_search_page(ARTICLE + ARTICLE, HOSTS, today=TODAY)
    assert [job.posting_id for job in parsed.jobs] == ["~02222"]


def test_an_unknown_tier_is_a_warning_not_a_count() -> None:
    odd = ARTICLE.replace("Proposals: Less than 5", "Proposals: Some")
    (job,) = (parsed := parse_search_page(odd, HOSTS, today=TODAY)).jobs
    assert job.proposal_count is None and job.proposal_count_exact is None
    assert parsed.warnings == ("Unknown proposals tier 'Some'",)


@pytest.mark.parametrize("html", ["", "   ", "<p>no tiles here</p>", "<<<>>>"])
def test_a_page_without_tiles_yields_nothing_and_never_raises(html: str) -> None:
    parsed = parse_search_page(html, HOSTS, today=TODAY)
    assert parsed.jobs == () and parsed.skipped == 0


def test_without_job_hosts_nothing_is_recognised() -> None:
    assert parse_search_page(LEGACY, (), today=TODAY).jobs == ()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.example.com/jobs/~02123", "~02123"),
        ("https://example.com/jobs/Some-title_~02123/", "~02123"),
        ("www.example.com/jobs/~02123?source=rss", "~02123"),
        ("/jobs/~02123", "~02123"),
        ("http://www.example.com/jobs/~02123", "~02123"),
    ],
)
def test_posting_links_canonicalise_to_the_first_host(url: str, expected: str) -> None:
    assert posting_link(url, HOSTS) == (
        f"https://www.example.com/jobs/{expected}",
        expected,
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.example.net/jobs/~02123",
        "https://www.example.com/jobs/~01123",
        "https://www.example.com/jobs/~02",
        "https://www.example.com/jobs/~02123.4",
        "https://www.example.com/freelancers/~02123",
        "ftp://www.example.com/jobs/~02123",
        "",
    ],
)
def test_other_links_are_not_postings(url: str) -> None:
    assert posting_link(url, HOSTS) is None


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("Activity on this job\nProposals: Less than 5", 4),
        ("Proposals:20 to 50", 50),
        ("proposals 50+", 51),
        ("No activity section", None),
    ],
)
def test_the_posting_band_is_its_upper_bound(text: str, count: int | None) -> None:
    assert proposal_band(text) == count


def test_a_full_posting_is_title_description_and_band() -> None:
    text = "\n  Example data pipeline\n\nBuild it.\n\nProposals: 20 to 50\n"
    parsed = parse_posting("https://www.example.com/jobs/x_~02111", text, HOSTS)
    (job,) = parsed.jobs
    assert job.title == "Example data pipeline"
    assert job.description == text.strip()
    assert (job.proposal_count, job.proposal_count_exact) == (50, False)
    assert job.posting_url == "https://www.example.com/jobs/~02111"


@pytest.mark.parametrize(
    ("url", "text", "warning"),
    [
        ("https://jobs.example.net/jobs/~02111", "Title", "The link is not"),
        ("https://www.example.com/jobs/~02111", "   \n ", "The posting text is empty"),
        (
            "https://www.example.com/jobs/~02111",
            "T" * (POSTING_LIMIT + 1),
            "The posting text exceeds",
        ),
    ],
)
def test_a_posting_that_cannot_be_keyed_or_read_is_skipped(
    url: str, text: str, warning: str
) -> None:
    parsed = parse_posting(url, text, HOSTS)
    assert parsed.jobs == () and parsed.skipped == 1
    assert parsed.warnings[0].startswith(warning)
