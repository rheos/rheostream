"""Job feed normalization over synthetic feed rows: no Postgres, no network."""

import importlib.util
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import rheo_leads
from rheo_core.operations.refusals import OperationRefused
from rheo_leads.intake.mapping import transform_value
from rheo_leads.intake.process import JOBSEARCH_TARGET_PREFIX
from rheo_leads.jobfeed.install import RULES
from rheo_leads.jobfeed.normalize import (
    BANDS,
    BODY_KEYS,
    COMPARABLE_FIELDS,
    POSTING_ID_PATTERN,
    NormalizedPosting,
    apply_detail,
    normalize_invitation,
    normalize_row,
    parse_number,
)
from rheo_leads.pipeline_evidence import validate_value

HOSTS = ("www.example.com", "example.com")
OBSERVED = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
URL = "https://www.example.com/jobs/Synthetic-dashboard-build_~0210001"
FLAG_FIELDS = ("proposal_count_exact", "client_payment_verified", "applied")


def feed_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "10001",
        "title": "Synthetic dashboard build",
        "url": URL + "?source=feed",
        "job_type": "fixed",
        "budget": "$500 - $1,500",
        "proposals_tier": "20 to 50",
        "description": "a feed row's description is never used",
        "description_snippet": "a short synthetic snippet",
        "client": {"verification_status": "VERIFIED"},
    }
    row.update(changes)
    return row


def normalize(row: object, hosts: Any = HOSTS) -> NormalizedPosting | None:
    return normalize_row(
        row, hosts, observed_at=OBSERVED, purpose="discover", search_ordinal=1
    )


def posting(**changes: object) -> NormalizedPosting:
    result = normalize(feed_row(**changes))
    assert result is not None
    return result


def detail(**record: object) -> dict[str, object]:
    return {
        "status": "ok",
        "data": {"marketplaceJobPosting": {"content": {"description": "Full text"}}},
        "client_record": record,
    }


def _migration(name: str) -> ModuleType:
    path = Path(rheo_leads.__file__).parent / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def preset_kinds() -> dict[str, dict[str, Any]]:
    """The Job search preset's field kinds, read from its frozen migrations: the
    seed, and the boolean ``invited`` that 0018 adds."""
    kinds: dict[str, dict[str, Any]] = {
        name: {"type": kind, "enum_values": list(values)}
        for name, kind, values in _migration("0007_jobsearch_preset.py").FIELDS
    }
    added = _migration("0018_invited_jobs.py").FIELD
    kinds[added.removeprefix(JOBSEARCH_TARGET_PREFIX)] = {
        "type": "boolean",
        "enum_values": [],
    }
    return kinds


# --- URL and posting id --------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "canonical", "posting_id"),
    [
        (URL, URL, "~0210001"),
        (URL + "?source=feed#top", URL, "~0210001"),
        (URL + "/", URL, "~0210001"),
        (
            "https://www.example.com/jobs/~0210002",
            "https://www.example.com/jobs/~0210002",
            "~0210002",
        ),
        (
            "https://example.com/jobs/Other_~0210003?x=1",
            "https://www.example.com/jobs/Other_~0210003",
            "~0210003",
        ),
    ],
)
def test_valid_urls_are_canonical_on_the_first_host(
    url: str, canonical: str, posting_id: str
) -> None:
    result = posting(url=url)
    assert (result.posting_url, result.posting_id) == (canonical, posting_id)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.example.com/jobs/~0210001",
        "https://jobs.example.org/jobs/~0210001",
        "https://www.example.com.example.org/jobs/~0210001",
        "https://www.example.com:8443/jobs/~0210001",
        "https://someone@www.example.com/jobs/~0210001",
        "https://www.example.com/jobs/Synthetic_10001",
        "https://www.example.com/jobs/~0110001",
        "https://www.example.com/jobs/~02abc",
        "https://www.example.com/jobs/~02",
        "https://www.example.com/jobs/~0210001/extra",
        "https://www.example.com/jobs/a/b_~0210001",
        "https://www.example.com/job/~0210001",
        "https://www.example.com/jobs/~02١٢٣",
        "",
        None,
        12,
    ],
)
def test_invalid_urls_skip_the_row(url: object) -> None:
    assert normalize(feed_row(url=url)) is None


def test_no_configured_host_accepts_nothing() -> None:
    assert normalize(feed_row(), hosts=()) is None


def test_a_bare_host_string_is_refused_as_misconfiguration() -> None:
    with pytest.raises(TypeError):
        normalize(feed_row(), hosts="www.example.com")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.example.com/jobs/slug_~02123.45",
        "https://www.example.com/jobs/~02123.4/",
        "https://www.example.com/jobs/~02123.",
    ],
)
def test_a_posting_id_holding_the_event_separator_is_rejected(url: str) -> None:
    assert normalize(feed_row(url=url)) is None


@pytest.mark.parametrize(
    ("value", "matches"),
    [
        ("~0210001", True),
        ("~020", True),
        ("~02123.45", False),
        ("~02", False),
        ("~0210001x", False),
        ("x~0210001", False),
        ("~0110001", False),
        ("~02١٢٣", False),
    ],
)
def test_posting_id_pattern_is_tilde_02_and_ascii_digits(
    value: str, matches: bool
) -> None:
    assert (POSTING_ID_PATTERN.fullmatch(value) is not None) is matches


@pytest.mark.parametrize("title", [None, "", "   ", 7])
def test_title_is_required(title: object) -> None:
    assert normalize(feed_row(title=title)) is None


def test_one_invalid_row_never_aborts_the_batch() -> None:
    rows: list[object] = [
        feed_row(),
        "not a row",
        feed_row(url="http://www.example.com/jobs/~0210002"),
        feed_row(url="https://www.example.com/jobs/~0210004"),
    ]
    results = [normalize(row) for row in rows]
    assert [r.posting_id for r in results if r is not None] == ["~0210001", "~0210004"]
    assert sum(r is None for r in results) == 2


# --- proposal counts -----------------------------------------------------------


@pytest.mark.parametrize("count", [0, 7, 50, 62])
def test_integer_counts_are_exact(count: int) -> None:
    result = posting(proposal_count=count)
    assert (result.proposal_count, result.proposal_count_exact) == (count, True)
    assert result.proposal_observed_at == OBSERVED


@pytest.mark.parametrize(
    ("tier", "bound"),
    [
        ("Fewer than 5", 4),
        ("5 to 10", 10),
        ("10 to 15", 15),
        ("15 to 20", 20),
        ("20 to 50", 50),
        ("50+", 51),
    ],
)
def test_bands_store_an_upper_bound_not_exact(tier: str, bound: int) -> None:
    assert BANDS[tier] == bound
    result = posting(proposals_tier=tier)
    assert (result.proposal_count, result.proposal_count_exact) == (bound, False)
    assert result.count_band_label == tier
    assert result.proposal_observed_at == OBSERVED
    assert result.to_body()["proposal_count_exact"] == "false"


def test_no_count_and_no_known_band_stays_unknown() -> None:
    for tier in (None, "Unknown", 50):
        result = posting(proposals_tier=tier)
        assert result.proposal_count is None
        assert result.proposal_count_exact is None
        assert result.proposal_observed_at is None
        assert result.count_band_label is None
        body = result.to_body()
        assert not {
            "proposal_count",
            "proposal_count_exact",
            "proposal_observed_at",
        } & set(body)


@pytest.mark.parametrize("count", [True, False, -1, 3.0, "5"])
def test_boolean_negative_and_non_integer_counts_invalidate_the_row(
    count: object,
) -> None:
    assert normalize(feed_row(proposal_count=count)) is None


# --- budget, job type, posted date --------------------------------------------


@pytest.mark.parametrize(
    ("budget", "low", "high"),
    [
        ("$500", "500", "500"),
        ("$500 - $1,500", "500", "1500"),
        ("$1,250.50", "1250.5", "1250.5"),
        ("Up to $250", None, "250"),
        ("up to $250", None, "250"),
        ("$2,000+", "2000", None),
        ("Budget not stated", None, None),
        ("", None, None),
        (None, None, None),
        (500, None, None),
    ],
)
def test_budget_amounts(budget: object, low: str | None, high: str | None) -> None:
    body = posting(budget=budget).to_body()
    assert (body.get("budget_min"), body.get("budget_max")) == (low, high)


@pytest.mark.parametrize(
    ("job_type", "expected"),
    [
        ("fixed", "fixed"),
        ("hourly", "hourly"),
        ("other", None),
        ("", None),
        (None, None),
    ],
)
def test_job_type_is_fixed_or_hourly_else_omitted(
    job_type: object, expected: str | None
) -> None:
    assert posting(job_type=job_type).to_body().get("job_type") == expected


@pytest.mark.parametrize(
    ("published", "created", "expected"),
    [
        ("2026-10-08T09:30:00Z", "2026-10-01T09:30:00Z", date(2026, 10, 8)),
        (None, "2026-10-01T09:30:00Z", date(2026, 10, 1)),
        ("", "2026-10-01", date(2026, 10, 1)),
        ("2026-10-08T22:30:00-05:00", None, date(2026, 10, 9)),
        (None, None, None),
        ("last Tuesday", "2026-10-01", None),
    ],
)
def test_posted_on_from_published_else_created(
    published: object, created: object, expected: date | None
) -> None:
    result = posting(published_date=published, created_date=created)
    assert result.posted_on == expected
    assert result.to_body().get("posted_on") == (
        None if expected is None else expected.isoformat()
    )


# --- client facts ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [("VERIFIED", True), ("NOT_VERIFIED", False), ("PENDING", None), (None, None)],
)
def test_payment_verification(status: object, expected: bool | None) -> None:
    assert (
        posting(client={"verification_status": status}).client_payment_verified
        is expected
    )


def test_missing_client_is_all_unknown() -> None:
    clients: tuple[object, ...] = (None, {}, "not a client")
    for client in clients:
        result = posting(client=client)
        assert result.client_payment_verified is None
        assert result.client_spend is None
        assert result.client_rating is None
        assert result.client_reviews is None


def test_client_facts_parse_numerically() -> None:
    result = posting(
        client={
            "verification_status": "VERIFIED",
            "total_spent": "$1,234.50",
            "rating": "4.9",
            "total_reviews": 3,
        }
    )
    assert result.client_spend == Decimal("1234.50")
    assert result.client_rating == Decimal("4.9")
    assert result.client_reviews == 3
    body = result.to_body()
    assert (body["client_spend"], body["client_rating"], body["client_reviews"]) == (
        "1234.5",
        "4.9",
        3,
    )


def test_absent_reviews_never_imply_a_new_client() -> None:
    result = posting(client={"verification_status": "VERIFIED", "total_spent": "$0.00"})
    assert result.client_reviews is None
    body = result.to_body()
    assert "client_reviews" not in body
    assert body["client_spend"] == "0"


def test_unknown_values_are_left_out_never_zero() -> None:
    result = normalize(
        {
            "title": "Synthetic minimal posting",
            "url": "https://www.example.com/jobs/~020",
        }
    )
    assert result is not None
    assert result.to_body() == {
        "title": "Synthetic minimal posting",
        "posting_url": "https://www.example.com/jobs/~020",
        "posting_id": "~020",
        "applied": "false",
        "observed_at": OBSERVED.isoformat(),
    }


@pytest.mark.parametrize(("applied", "expected"), [(True, "true"), ("yes", "false")])
def test_applied_is_true_only_when_the_row_says_so(
    applied: object, expected: str
) -> None:
    assert posting(applied=applied).to_body()["applied"] == expected


def test_a_feed_row_never_supplies_a_description() -> None:
    result = posting(description="feed text", description_snippet="snippet")
    assert result.description is None
    assert "description" not in result.to_body()
    assert "description" not in result.comparable_fields()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("$1,234.50", Decimal("1234.50")),
        ("4.84", Decimal("4.84")),
        (7, Decimal(7)),
        (4.84, Decimal("4.84")),
        (None, None),
        (True, None),
        (False, None),
        ("not a number", None),
        ("NaN", None),
        ("Infinity", None),
    ],
)
def test_parse_number(value: object, expected: Decimal | None) -> None:
    assert parse_number(value) == expected


# --- the delivery body ------------------------------------------------------------


def full_posting(flag: bool) -> NormalizedPosting:
    """Every field carried; ``invited`` is delivered only when true, so it is."""
    base = posting(
        proposal_count=None if flag else 12,
        proposals_tier="10 to 15",
        published_date="2026-10-08",
        applied=flag,
        client={
            "verification_status": "VERIFIED" if flag else "NOT_VERIFIED",
            "total_spent": "$900",
            "rating": 4.5,
            "total_reviews": 8,
        },
    )
    described = apply_detail(base, detail(hire_rate_percent=40), HOSTS)
    return replace(described, invited=True)


@pytest.mark.parametrize("flag", [True, False])
def test_flags_are_the_strings_true_and_false(flag: bool) -> None:
    body = full_posting(flag).to_body()
    for name in FLAG_FIELDS:
        assert type(body[name]) is str
    assert body["applied"] == ("true" if flag else "false")
    assert body["client_payment_verified"] == ("true" if flag else "false")
    # A band is not exact; an integer count is.
    assert body["proposal_count_exact"] == ("false" if flag else "true")
    assert not [key for key, value in body.items() if isinstance(value, bool)]


@pytest.mark.parametrize("flag", [True, False])
def test_every_body_value_is_valid_for_its_preset_field(flag: bool) -> None:
    kinds = preset_kinds()
    body = full_posting(flag).to_body()
    checked: set[str] = set()
    for rule in RULES:
        name = rule.source_path.removeprefix("/")
        if not rule.target.startswith(JOBSEARCH_TARGET_PREFIX) or rule.transform:
            continue
        assert name in body
        spec = kinds[rule.target.removeprefix(JOBSEARCH_TARGET_PREFIX)]
        # The same rendering the installed mapping applies, then the field's check.
        validate_value(spec, transform_value(body[name], rule.transform, None))
        checked.add(name)
    assert set(FLAG_FIELDS) <= checked


def test_a_python_bool_would_be_refused_by_the_boolean_kind() -> None:
    # Pins why flags are strings: the mapping renders True as "True".
    with pytest.raises(OperationRefused):
        validate_value({"type": "boolean"}, transform_value(True, None, None))


def test_body_keys_are_exactly_what_the_installed_mapping_reads() -> None:
    read = {
        rule.source_path.removeprefix("/")
        for rule in RULES
        if rule.transform != "const"
    } | {"observed_at"}
    assert set(BODY_KEYS) == {
        "title",
        "description",
        "posting_url",
        "posting_id",
        "job_type",
        "budget_min",
        "budget_max",
        "proposal_count",
        "proposal_count_exact",
        "proposal_observed_at",
        "posted_on",
        "client_payment_verified",
        "client_spend",
        "client_rating",
        "client_reviews",
        "client_hire_rate",
        "applied",
        "invited",
        "observed_at",
    }
    assert set(BODY_KEYS) <= read
    body = full_posting(False).to_body()
    assert set(body) == set(BODY_KEYS)


def test_comparable_fields_omit_observation_times_and_nulls() -> None:
    fields = full_posting(False).comparable_fields()
    assert set(fields) == set(COMPARABLE_FIELDS)
    assert "observed_at" not in fields
    assert "proposal_observed_at" not in fields
    assert "posting_id" not in fields
    assert all(type(value) is str for value in fields.values())
    assert fields["proposal_count"] == "12"
    assert fields["client_hire_rate"] == "0.4"
    thin = posting(proposals_tier=None, client={}).comparable_fields()
    assert "proposal_count" not in thin
    assert "client_spend" not in thin


def test_comparable_fields_ignore_the_observation_time() -> None:
    first = posting(proposal_count=5)
    later = normalize_row(
        feed_row(proposal_count=5),
        HOSTS,
        observed_at=OBSERVED + timedelta(hours=1),
        purpose="refresh_only",
        search_ordinal=2,
    )
    assert later is not None
    assert first.comparable_fields() == later.comparable_fields()
    assert (later.purpose, later.search_ordinal) == ("refresh_only", 2)


def test_observed_at_must_carry_a_time_zone() -> None:
    with pytest.raises(ValueError):
        normalize_row(
            feed_row(),
            HOSTS,
            observed_at=datetime(2026, 10, 9, 12, 0),
            purpose="discover",
            search_ordinal=0,
        )
    other_zone = datetime(2026, 10, 9, 5, 0, tzinfo=timezone(timedelta(hours=-7)))
    result = normalize_row(
        feed_row(), HOSTS, observed_at=other_zone, purpose="discover", search_ordinal=0
    )
    assert result is not None
    assert result.to_body()["observed_at"] == other_zone.isoformat()


# --- detail reads --------------------------------------------------------------


def test_a_successful_detail_adds_description_and_client_record() -> None:
    result = apply_detail(
        posting(proposal_count=9),
        detail(
            spend_total="$523.00",
            feedback_count=3,
            feedback_score=4.84,
            hire_rate_percent=50,
        ),
        HOSTS,
    )
    assert result.description == "Full text"
    assert result.client_spend == Decimal("523")
    assert result.client_reviews == 3
    assert result.client_rating == Decimal("4.84")
    assert result.client_hire_rate == Decimal("0.5")
    body = result.to_body()
    assert (body["description"], body["client_hire_rate"], body["client_spend"]) == (
        "Full text",
        "0.5",
        "523",
    )
    assert body["proposal_count"] == 9


@pytest.mark.parametrize(
    "response",
    [
        None,
        "not a response",
        {**detail(spend_total="$600"), "status": "error"},
        {**detail(spend_total="$600"), "status": None},
        {
            "status": "ok",
            "data": {"marketplaceJobPosting": {"content": {"description": "   "}}},
            "client_record": {"spend_total": "$600"},
        },
        {
            "status": "ok",
            "data": {"marketplaceJobPosting": {"content": {}}},
            "client_record": {"spend_total": "$600"},
        },
        {"status": "ok", "client_record": {"spend_total": "$600"}},
    ],
)
def test_a_failed_or_empty_detail_adds_nothing(response: object) -> None:
    before = posting(client={"verification_status": "VERIFIED", "total_spent": "$1"})
    assert apply_detail(before, response, HOSTS) == before


def test_a_detail_never_supplies_a_proposal_count() -> None:
    response = {**detail(), "proposal_count": 3, "proposals_tier": "Fewer than 5"}
    before = posting(proposals_tier="20 to 50")
    after = apply_detail(before, response, HOSTS)
    assert (after.proposal_count, after.proposal_count_exact) == (50, False)


def test_duplicate_detail_reads_never_restore_stale_client_metrics() -> None:
    first_read = detail(spend_total="$600.00")
    fresh = apply_detail(posting(), first_read, HOSTS)
    duplicate = apply_detail(
        posting(client={"verification_status": "VERIFIED", "total_spent": "$1.00"}),
        first_read,
        HOSTS,
    )
    assert fresh.client_spend == duplicate.client_spend == Decimal("600.00")
    # A second, thinner read keeps the first read's metrics.
    again = apply_detail(duplicate, detail(), HOSTS)
    assert again.client_spend == Decimal("600.00")


# --- the feed row's own id ------------------------------------------------------------

FEED_ID = "feedid-7c41e9"


def test_the_feed_id_is_carried_but_never_delivered() -> None:
    carried = apply_detail(posting(id=FEED_ID), detail(hire_rate_percent=40), HOSTS)
    assert carried.feed_id == FEED_ID
    body = carried.to_body()
    assert "feed_id" not in body and "feed_id" not in BODY_KEYS
    assert FEED_ID not in repr(body)
    assert FEED_ID not in repr(carried.comparable_fields())
    # The id never makes two otherwise equal sightings differ.
    other = apply_detail(posting(id="feedid-0000"), detail(hire_rate_percent=40), HOSTS)
    assert carried.comparable_fields() == other.comparable_fields()


@pytest.mark.parametrize(
    "value",
    [None, "", "has space", "x" * 65, "dot.ted", 10001, True, ["10001"]],
    ids=["missing", "empty", "space", "long", "dot", "integer", "bool", "list"],
)
def test_an_unusable_feed_id_is_left_unknown(value: object) -> None:
    row = feed_row(id=value)
    if value is None:
        del row["id"]
    result = normalize(row)
    assert result is not None and result.feed_id is None


# --- invitations ----------------------------------------------------------------

INVITE_URL = "https://www.example.com/jobs/~0220002?utm_source=invite&utm_medium=x"


def invitation(**job: object) -> dict[str, object]:
    posting_fields: dict[str, object] = {
        "id": "20002",
        "content": {"title": "Synthetic invited build"},
        "url": INVITE_URL,
    }
    posting_fields.update(job)
    return {
        "id": "300003",
        "context": "Freelancer",
        "client": {"user": {"id": "1", "name": "Pat E."}, "parentOrganization": {}},
        "invitationLetter": "<untrusted_participant_content>Hi</untrusted_participant_content>",  # noqa: E501
        "jobPosting": posting_fields,
    }


def invited(row: object) -> NormalizedPosting | None:
    return normalize_invitation(row, HOSTS, observed_at=OBSERVED, search_ordinal=-1)


def test_an_invitation_is_an_invited_discovery_posting_of_its_job() -> None:
    result = invited(invitation())
    assert result is not None
    assert result.posting_id == "~0220002"
    assert result.posting_url == "https://www.example.com/jobs/~0220002"
    assert result.title == "Synthetic invited build"
    assert result.feed_id == "20002"
    assert result.invited is True
    assert result.purpose == "discover"
    assert result.applied is False
    assert result.description is None
    assert result.proposal_count is None
    assert result.search_ordinal == -1


def test_an_invitation_keeps_nothing_of_the_client_or_the_letter() -> None:
    result = invited(invitation())
    assert result is not None
    body = result.to_body()
    assert body["invited"] == "true"
    text = repr(result) + repr(body)
    assert "Pat E." not in text and "Hi" not in text and "300003" not in text


@pytest.mark.parametrize(
    "row",
    [
        None,
        {},
        {"jobPosting": "nope"},
        invitation(url="https://elsewhere.example.org/jobs/~0220002"),
        invitation(url="http://www.example.com/jobs/~0220002"),
        invitation(url="https://www.example.com/jobs/~01abc"),
        invitation(content={"title": "  "}),
        invitation(content=None),
    ],
)
def test_an_invitation_without_a_valid_job_is_invalid(row: object) -> None:
    assert invited(row) is None


def test_an_invitation_with_an_odd_id_reads_no_detail() -> None:
    result = invited(invitation(id="20002/../x"))
    assert result is not None
    assert result.feed_id is None


def test_invited_is_delivered_only_when_true() -> None:
    plain = posting()
    assert "invited" not in plain.to_body()
    assert "invited" not in plain.comparable_fields()
    flagged = replace(plain, invited=True)
    assert flagged.to_body()["invited"] == "true"
    assert flagged.comparable_fields()["invited"] == "true"
