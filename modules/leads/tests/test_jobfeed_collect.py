"""Job feed collection on a fake clock: page checks, cursors, the 80-row cap, the
merge of overlapping sightings and the detail reads. No Postgres, no network.

Collection runs through the real upstream wrapper over a fake session, so pacing
and checkpoints are exercised as they will be in a run.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType, TracebackType
from typing import Any

import pytest
from rheo_core.connectors.mcp_client import McpFailure
from rheo_leads.jobfeed.collect import (
    MAX_DETAIL_READS,
    ROW_CAP,
    CollectResult,
    DetailResult,
    Sighting,
    collect_presets,
    merge_sightings,
    read_details,
)
from rheo_leads.jobfeed.normalize import NormalizedPosting, normalize_row
from rheo_leads.jobfeed.presets import DEFAULT_PRESETS, Preset, Purpose
from rheo_leads.jobfeed.upstream import Upstream, UpstreamFailure

ORG = "org-marker-9b3e1f72-4a6c-4d08-b5e3-2c7f9a1d6e84"
HOSTS = ("www.example.com",)
START = 1000.0
WALL = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
SNIPPET = "snippet-marker-shortened feed text"
FULL = "A full synthetic description of the dashboard build."

Search = Callable[[str, dict[str, Any]], object]
"""``(action, params)`` to a ``find_jobs`` document, or an exception to raise."""


class Clock:
    def __init__(self) -> None:
        self.now = START

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def wall(self) -> datetime:
        return WALL + timedelta(seconds=self.now - START)


class Token:
    def __init__(self) -> None:
        self.checkpoints = 0

    def checkpoint(self, now: object = None, *, force: bool = False) -> None:
        self.checkpoints += 1


@dataclass
class FakeSession:
    search: Search
    clock: Clock
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def initialize(self) -> None:
        return None

    def notify_initialized(self) -> None:
        return None

    def list_tools(
        self, cursor: str | None = None, *, deadline: float | None = None
    ) -> dict[str, object]:
        raise AssertionError("collection never lists tools")

    def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        assert name == "find_jobs"
        assert arguments["org_uid"] == ORG
        action = arguments["action"]
        params = arguments["params"]
        assert isinstance(action, str) and isinstance(params, dict)
        self.calls.append((action, dict(params)))
        answer = self.search(action, params)
        if isinstance(answer, BaseException):
            raise answer
        return {"content": [{"type": "text", "text": json.dumps(answer)}]}

    def actions(self) -> list[str]:
        return [action for action, _ in self.calls]


@dataclass
class Run:
    session: FakeSession
    clock: Clock
    upstream: Upstream
    capability_checks: int = 0
    capability: Callable[[int], bool] = lambda check: True

    def capability_on(self) -> bool:
        self.capability_checks += 1
        return self.capability(self.capability_checks)

    def collect(self, presets: Sequence[Preset] = DEFAULT_PRESETS) -> CollectResult:
        return collect_presets(
            self.upstream,
            presets,
            ORG,
            HOSTS,
            clock=self.clock.wall,
            capability_on=self.capability_on,
        )

    def details(
        self,
        sightings: Sequence[Sighting],
        *,
        deadline: float = START + 10_000.0,
        max_reads: int = MAX_DETAIL_READS,
    ) -> DetailResult:
        return read_details(
            self.upstream,
            sightings,
            ORG,
            HOSTS,
            deadline=deadline,
            max_reads=max_reads,
        )


def run(search: Search, *, deadline: float = START + 10_000.0) -> Run:
    clock = Clock()
    session = FakeSession(search, clock)
    upstream = Upstream(
        session,
        Token(),
        deadline=deadline,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    upstream.__enter__()
    return Run(session, clock, upstream)


counter = iter(range(10_000, 1_000_000))


def job(number: int | None = None, **changes: object) -> dict[str, object]:
    number = next(counter) if number is None else number
    row: dict[str, object] = {
        "id": str(number),
        "title": f"Synthetic build {number}",
        "url": f"https://www.example.com/jobs/Synthetic-build_~02{number}?source=feed",
        "job_type": "fixed",
        "budget": "$500 - $1,500",
        "proposals_tier": "5 to 10",
        "description": SNIPPET,
        "description_snippet": SNIPPET,
    }
    row.update(changes)
    return row


def page(
    action: str,
    params: Mapping[str, Any],
    jobs: list[object],
    *,
    more: bool = True,
    cursor: object = "auto",
    **changes: object,
) -> dict[str, object]:
    """A page answering the request: the requested mode echoed, the cursor in the
    field its action uses."""
    if cursor == "auto":
        cursor = f"cursor-{next(counter)}"
    document: dict[str, object] = {"status": "ok", "jobs": jobs}
    if action == "smart_search":
        document.update(mode=params.get("mode"), hasMore=more, next_cursor=cursor)
    else:
        document["pageInfo"] = {"hasNextPage": more, "endCursor": cursor}
    document.update(changes)
    return document


def full_pages(action: str, params: dict[str, Any]) -> object:
    return page(action, params, [job() for _ in range(params["limit"])])


def preset(
    name: str, *, action: str = "smart_search", pages: int = 1, **params: object
) -> Preset:
    """A preset built directly, not through the validator."""
    filled: dict[str, object] = {"limit": 10, **params}
    if action == "smart_search":
        filled.setdefault("mode", "best_match")
    return Preset(
        name=name,
        action=action,  # type: ignore[arg-type]
        purpose="discover",
        pages=pages,
        params=MappingProxyType(filled),
    )


# --- page checks and cursors (AC 33) ----------------------------------------------


BAD_PAGES: dict[str, Callable[[str, dict[str, Any]], object]] = {
    "status": lambda a, p: page(a, p, [job()], status="error"),
    "filters-ignored": lambda a, p: page(a, p, [job()], filters_ignored=["skills"]),
    "filters-rejected": lambda a, p: page(a, p, [job()], filters_rejected=["x"]),
    "mode": lambda a, p: page(a, p, [job()], mode="most_recent"),
    "too-many-rows": lambda a, p: page(a, p, [job() for _ in range(11)]),
    "jobs-not-a-list": lambda a, p: {**page(a, p, []), "jobs": {"0": job()}},
    "call-failed": lambda a, p: McpFailure("unavailable"),
}


@pytest.mark.parametrize("bad", BAD_PAGES.values(), ids=BAD_PAGES.keys())
def test_a_bad_page_stops_its_search_while_the_others_run(
    bad: Callable[[str, dict[str, Any]], object],
) -> None:
    def search(action: str, params: dict[str, Any]) -> object:
        if params.get("mode") == "best_match":
            return bad(action, params)
        return full_pages(action, params)

    r = run(search)
    result = r.collect()
    assert r.session.actions() == ["smart_search"] * 6 + ["search"]
    assert result.search_failures == 1
    assert len(result.rows) == 60
    assert all(row.search_ordinal in (1, 2) for row in result.rows)
    assert result.pages_used == 7
    assert not result.cut and not result.capability_lost


def test_a_failed_call_is_reported_for_the_health_code() -> None:
    def search(action: str, params: dict[str, Any]) -> object:
        if action == "search":
            return McpFailure("rate_limited", retry_after=90)
        return full_pages(action, params)

    result = run(search).collect()
    (failure,) = result.upstream_failures
    assert failure.code == "rate_limited" and failure.retry_after == 90
    assert result.search_failures == 1


def test_a_terminal_failure_ends_collection() -> None:
    def search(action: str, params: dict[str, Any]) -> object:
        return McpFailure("unauthorized")

    r = run(search)
    with pytest.raises(UpstreamFailure) as caught:
        r.collect()
    assert caught.value.terminal and caught.value.__context__ is None
    assert len(r.session.calls) == 1


@pytest.mark.parametrize("cursor", [None, "", 7], ids=["missing", "empty", "number"])
def test_a_missing_cursor_while_more_is_claimed_stops_that_search(
    cursor: object,
) -> None:
    def search(action: str, params: dict[str, Any]) -> object:
        return page(action, params, [job()], cursor=cursor)

    r = run(search)
    result = r.collect()
    # Most Recent asks for five pages but stops after the first. A page that claims
    # more without a usable cursor is a failure even when it is its search's last.
    assert r.session.actions() == ["smart_search", "smart_search", "search"]
    assert result.search_failures == 3
    assert len(result.rows) == 3


def test_a_cursor_is_checked_even_on_the_page_that_reaches_the_cap() -> None:
    def search(action: str, params: dict[str, Any]) -> object:
        cursor = None if len(r.session.calls) == 8 else f"cursor-{next(counter)}"
        return page(action, params, [job() for _ in range(10)], cursor=cursor)

    r = run(search)
    result = r.collect([preset(f"p{n}", pages=4) for n in range(3)])
    assert len(r.session.calls) == 8 and len(result.rows) == ROW_CAP
    assert result.search_failures == 1


def test_a_repeated_cursor_stops_that_search() -> None:
    def search(action: str, params: dict[str, Any]) -> object:
        return page(action, params, [job()], cursor="same")

    r = run(search)
    result = r.collect()
    recent = [p for a, p in r.session.calls if p.get("mode") == "most_recent"]
    assert [p.get("cursor") for p in recent] == [None, "same"]
    assert result.search_failures == 1


def test_cursor_fields_follow_the_action() -> None:
    def search(action: str, params: dict[str, Any]) -> object:
        n = len(r.session.calls)
        return page(action, params, [job()], cursor=f"{action}-{n}")

    r = run(search)
    presets = [
        preset("feed", pages=3),
        preset("query", action="search", pages=3, query="dashboards"),
    ]
    result = r.collect(presets)
    assert [p.get("cursor") for _, p in r.session.calls] == [
        None,
        "smart_search-1",
        "smart_search-2",
        None,
        "search-4",
        "search-5",
    ]
    assert result.search_failures == 0 and len(result.rows) == 6


def test_the_other_actions_cursor_fields_are_ignored() -> None:
    """A smart search page carrying only ``pageInfo`` claims no more pages, and a
    search page carrying only ``hasMore`` claims none either."""

    def search(action: str, params: dict[str, Any]) -> object:
        swapped = "search" if action == "smart_search" else "smart_search"
        document = page(swapped, params, [job()])
        document["mode"] = params.get("mode")
        return document

    r = run(search)
    result = r.collect(
        [preset("feed", pages=3), preset("query", action="search", pages=3)]
    )
    assert len(r.session.calls) == 2
    assert result.search_failures == 0


def test_no_more_pages_ends_a_search_without_a_failure() -> None:
    def search(action: str, params: dict[str, Any]) -> object:
        return page(action, params, [job()], more=False, cursor=None)

    r = run(search)
    result = r.collect()
    assert len(r.session.calls) == 3 and result.search_failures == 0


def test_rows_are_stamped_at_response_time_and_invalid_rows_counted() -> None:
    def search(action: str, params: dict[str, Any]) -> object:
        return page(action, params, [job(), job(url="https://elsewhere.example.org/x")])

    r = run(search)
    result = r.collect([preset("feed", pages=2)])
    assert result.invalid_rows == 2 and len(result.rows) == 2
    first, second = result.rows
    assert second.observed_at - first.observed_at >= timedelta(seconds=5.3)


# --- bounds (AC 31, I18) ----------------------------------------------------------


def test_the_default_searches_and_their_bound() -> None:
    r = run(full_pages)
    result = r.collect()
    assert [(a, p.get("mode")) for a, p in r.session.calls] == [
        ("smart_search", "best_match"),
        *[("smart_search", "most_recent")] * 5,
        ("search", None),
    ]
    refresh = r.session.calls[-1][1]
    assert refresh["proposals_min"] == 51 and "proposals_max" not in refresh
    assert len(result.rows) == 70 <= ROW_CAP
    assert result.pages_used == 7


def test_the_row_cap_holds_for_presets_built_directly() -> None:
    r = run(full_pages)
    presets = [preset(f"p{n}", pages=4) for n in range(3)]
    result = r.collect(presets)
    assert len(result.rows) == ROW_CAP
    # Eight full pages reach the cap; the ninth is never requested.
    assert len(r.session.calls) == 8 and result.pages_used == 8


def test_the_page_crossing_the_cap_is_truncated() -> None:
    r = run(full_pages)
    presets = [preset(f"p{n}", pages=4, limit=7) for n in range(3)]
    result = r.collect(presets)
    assert len(r.session.calls) == 12
    assert len(result.rows) == ROW_CAP
    last = [row for row in result.rows if row.search_ordinal == 2]
    assert len(last) == 3 * 7 + 3


def test_invalid_rows_count_toward_the_cap() -> None:
    def search(action: str, params: dict[str, Any]) -> object:
        return page(action, params, [job(title="")] * params["limit"])

    r = run(search)
    result = r.collect([preset(f"p{n}", pages=4) for n in range(3)])
    assert len(r.session.calls) == 8
    assert result.invalid_rows == ROW_CAP and result.rows == ()


def test_capability_turned_off_between_pages_stops_collection() -> None:
    r = run(full_pages)
    r.capability = lambda check: check < 3
    result = r.collect()
    assert result.capability_lost is True
    assert len(r.session.calls) == 3 and len(result.rows) == 30
    assert r.capability_checks == 3


def test_the_deadline_cuts_collection() -> None:
    r = run(full_pages, deadline=START + 12.0)
    result = r.collect()
    assert result.cut is True
    assert len(r.session.calls) == 2 and len(result.rows) == 20


# --- merge (AC 70, I24) -----------------------------------------------------------


def posting(
    number: int,
    *,
    ordinal: int,
    at: int,
    purpose: Purpose = "discover",
    **changes: object,
) -> NormalizedPosting:
    normalized = normalize_row(
        job(number, **changes),
        HOSTS,
        observed_at=WALL + timedelta(seconds=at),
        purpose=purpose,
        search_ordinal=ordinal,
    )
    assert normalized is not None
    return normalized


def test_the_same_posting_in_two_searches_is_one_sighting() -> None:
    best = posting(501, ordinal=0, at=10, applied=True, proposal_count=4)
    recent = posting(
        501, ordinal=1, at=20, proposals_tier="10 to 15", title="Newer title"
    )
    other = posting(502, ordinal=1, at=20)
    merged = merge_sightings([best, other, recent])
    assert [s.posting.posting_id for s in merged] == ["~02501", "~02502"]
    one = merged[0].posting
    assert (one.proposal_count, one.proposal_count_exact) == (15, False)
    assert one.proposal_observed_at == one.observed_at == WALL + timedelta(seconds=20)
    assert one.title == "Newer title" and one.search_ordinal == 1
    assert one.applied is True and one.purpose == "discover"
    assert one.description is None and merged[0].detail_ok is False


def test_a_tie_prefers_an_exact_count_then_the_earlier_search() -> None:
    band = posting(601, ordinal=0, at=10, title="Band")
    exact = posting(601, ordinal=1, at=10, title="Exact", proposal_count=9)
    (one,) = merge_sightings([band, exact])
    assert one.posting.title == "Exact" and one.posting.proposal_count_exact is True
    later = posting(602, ordinal=1, at=10, title="Later search")
    earlier = posting(602, ordinal=0, at=10, title="Earlier search")
    (two,) = merge_sightings([later, earlier])
    assert two.posting.title == "Earlier search"


def test_purpose_is_discovery_when_any_row_discovers() -> None:
    refresh = posting(701, ordinal=2, at=30, purpose="refresh_only")
    discover = posting(701, ordinal=1, at=10)
    (one,) = merge_sightings([refresh, discover])
    assert one.posting.purpose == "discover"
    assert one.posting.search_ordinal == 2
    (only,) = merge_sightings([refresh, replace(refresh, observed_at=WALL)])
    assert only.posting.purpose == "refresh_only"


def test_merged_posting_ids_are_unique() -> None:
    rows = [posting(800 + n % 4, ordinal=n % 3, at=n) for n in range(20)]
    merged = merge_sightings(rows)
    ids = [s.posting.posting_id for s in merged]
    assert ids == ["~02800", "~02801", "~02802", "~02803"]


# --- detail reads (AC 34, I17) ------------------------------------------------------


def detail_ok(params: dict[str, Any]) -> dict[str, object]:
    return {
        "status": "ok",
        "data": {"marketplaceJobPosting": {"content": {"description": FULL}}},
        "client_record": {"spend_total": "$1,200.00", "hire_rate_percent": 40},
    }


def details_answer(
    answer: Callable[[dict[str, Any]], object] = detail_ok,
) -> Search:
    def search(action: str, params: dict[str, Any]) -> object:
        assert action == "get"
        return answer(params)

    return search


def sightings(*postings: NormalizedPosting) -> list[Sighting]:
    return [Sighting(p) for p in postings]


def assert_no_snippet(result: DetailResult) -> None:
    for sighting in result.sightings:
        assert SNIPPET not in json.dumps(sighting.posting.to_body())


def test_a_successful_read_supplies_description_and_client_record() -> None:
    r = run(details_answer())
    result = r.details(sightings(posting(901, ordinal=0, at=0)))
    (one,) = result.sightings
    assert one.detail_ok and one.posting.description == FULL
    assert str(one.posting.client_spend) == "1200.00"
    assert str(one.posting.client_hire_rate) == "0.4"
    assert one.posting.purpose == "discover"
    assert r.session.calls == [("get", {"id": "901"})]
    assert (result.detail_reads, result.failures, result.cut) == (1, 0, False)


def test_at_most_sixty_reads_and_the_omitted_rest_downgrade() -> None:
    r = run(details_answer())
    result = r.details(
        sightings(*(posting(1000 + n, ordinal=1, at=n) for n in range(65)))
    )
    assert len(r.session.calls) == MAX_DETAIL_READS == result.detail_reads
    assert result.failures == 5
    assert result.reasons == {"not_read_cap": 5}
    read, omitted = result.sightings[:60], result.sightings[60:]
    assert all(s.detail_ok and s.posting.description == FULL for s in read)
    assert all(
        not s.detail_ok
        and s.posting.purpose == "refresh_only"
        and s.posting.description is None
        for s in omitted
    )
    assert_no_snippet(result)


def test_ineligible_sightings_are_never_read() -> None:
    r = run(details_answer())
    given = sightings(
        posting(1101, ordinal=2, at=0, purpose="refresh_only"),
        posting(1102, ordinal=0, at=0, applied=True),
        posting(1103, ordinal=0, at=0, proposal_count=62),
        posting(1104, ordinal=0, at=0, proposals_tier="50+"),
    )
    result = r.details(given)
    assert r.session.calls == []
    assert list(result.sightings) == given
    assert (result.detail_reads, result.failures) == (0, 0)


def test_one_read_per_unique_id_even_across_searches() -> None:
    r = run(details_answer())
    best = posting(1201, ordinal=0, at=0)
    recent = posting(1201, ordinal=1, at=5, proposal_count=3)
    result = r.details(sightings(best, recent))
    assert r.session.calls == [("get", {"id": "1201"})]
    assert all(s.detail_ok and s.posting.description == FULL for s in result.sightings)
    assert result.sightings[1].posting.proposal_count == 3
    assert result.detail_reads == 1


def test_overlapping_searches_read_details_once_end_to_end() -> None:
    shared = job(1301)

    def search(action: str, params: dict[str, Any]) -> object:
        if action == "get":
            return detail_ok(params)
        return page(action, params, [shared], more=False, cursor=None)

    r = run(search)
    collected = r.collect()
    assert len(collected.rows) == 3
    merged = merge_sightings(collected.rows)
    result = r.details(merged)
    assert [a for a, _ in r.session.calls].count("get") == 1
    (one,) = result.sightings
    assert one.detail_ok and one.posting.purpose == "discover"


# The run-history reason each failed read is kept as (#377).
FAILED_READ_REASONS = {
    "call-failed": "unavailable",
    "status": "no_description",
    "no-description": "no_description",
    "blank-description": "no_description",
}
FAILED_READS: dict[str, Callable[[dict[str, Any]], object]] = {
    "call-failed": lambda p: McpFailure("unavailable"),
    "status": lambda p: {"status": "error"},
    "no-description": lambda p: {"status": "ok", "data": {}},
    "blank-description": lambda p: {
        "status": "ok",
        "data": {"marketplaceJobPosting": {"content": {"description": "  "}}},
    },
}


@pytest.mark.parametrize("name", FAILED_READS.keys())
def test_a_failed_read_downgrades_without_a_description(name: str) -> None:
    r = run(details_answer(FAILED_READS[name]))
    result = r.details(sightings(posting(1401, ordinal=0, at=0)))
    (one,) = result.sightings
    assert not one.detail_ok
    assert one.posting.purpose == "refresh_only" and one.posting.description is None
    assert "description" not in one.posting.to_body()
    assert (result.detail_reads, result.failures) == (1, 1)
    assert result.reasons == {FAILED_READ_REASONS[name]: 1}
    assert_no_snippet(result)


def test_a_tool_error_keeps_the_tool_code_as_its_reason() -> None:
    r = run(
        details_answer(lambda p: McpFailure("tool_error", tool_code="JOB_NOT_VISIBLE"))
    )
    result = r.details(sightings(posting(1451, ordinal=0, at=0)))
    assert result.reasons == {"tool_error:JOB_NOT_VISIBLE": 1}


def test_a_posting_without_a_feed_id_is_not_read() -> None:
    r = run(details_answer())
    result = r.details(
        [Sighting(replace(posting(1461, ordinal=0, at=0), feed_id=None))]
    )
    assert r.session.calls == []
    assert (result.detail_reads, result.failures) == (0, 1)
    assert result.reasons == {"no_feed_id": 1}


def test_a_failed_read_is_not_repeated_for_a_duplicate_id() -> None:
    r = run(details_answer(lambda p: McpFailure("unavailable")))
    result = r.details(
        sightings(posting(1501, ordinal=0, at=0), posting(1501, ordinal=1, at=1))
    )
    assert len(r.session.calls) == 1 and result.failures == 1
    # One reason per failure: the duplicate shares the first read's.
    assert result.reasons == {"unavailable": 1}
    assert all(s.posting.purpose == "refresh_only" for s in result.sightings)


def test_the_deadline_omits_the_remaining_reads() -> None:
    r = run(details_answer())
    # The fourth read starts with 6 s left; the fifth would start with 0.7 s.
    deadline = START + 3 * 5.3 + 6.0
    result = r.details(
        sightings(*(posting(1600 + n, ordinal=1, at=n) for n in range(6))),
        deadline=deadline,
    )
    assert len(r.session.calls) == 4 == result.detail_reads
    assert result.cut is True and result.failures == 2
    assert result.reasons == {"not_read_deadline": 2}
    assert [s.detail_ok for s in result.sightings] == [True] * 4 + [False] * 2
    assert all(
        s.posting.purpose == "refresh_only" and s.posting.description is None
        for s in result.sightings[4:]
    )


def test_a_terminal_failure_ends_the_reads() -> None:
    r = run(details_answer(lambda p: McpFailure("unauthorized")))
    with pytest.raises(UpstreamFailure) as caught:
        r.details(
            sightings(posting(1701, ordinal=0, at=0), posting(1702, ordinal=0, at=0))
        )
    assert caught.value.terminal and caught.value.__context__ is None
    assert len(r.session.calls) == 1


# --- the id a detail read sends -------------------------------------------------


def test_a_detail_read_sends_the_feed_rows_own_id() -> None:
    r = run(details_answer())
    result = r.details(
        sightings(
            posting(1801, ordinal=0, at=0, id="feed-7741"),
            posting(1802, ordinal=0, at=0, id=None),
            posting(1803, ordinal=0, at=0, id="feed-7743"),
        )
    )
    # A row with no usable id of its own gets no read, nothing derived from its URL.
    assert r.session.calls == [
        ("get", {"id": "feed-7741"}),
        ("get", {"id": "feed-7743"}),
    ]
    read, unread, other = result.sightings
    assert read.detail_ok and other.detail_ok
    assert not unread.detail_ok
    assert unread.posting.purpose == "refresh_only"
    assert unread.posting.description is None
    assert (result.detail_reads, result.failures) == (2, 1)


def test_a_merged_sighting_keeps_a_feed_id_from_any_row() -> None:
    older = posting(1901, ordinal=0, at=0, id="feed-1901")
    newer = posting(1901, ordinal=1, at=9, id=None)
    (one,) = merge_sightings([older, newer])
    assert one.posting.observed_at == newer.observed_at
    assert one.posting.feed_id == "feed-1901"
    r = run(details_answer())
    r.details([one])
    assert r.session.calls == [("get", {"id": "feed-1901"})]
