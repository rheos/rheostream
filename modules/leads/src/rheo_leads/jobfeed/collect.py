"""Collection: the search pages, the merge of overlapping sightings, the detail reads.

No database access. Every collaborator arrives as an argument: the run's
:class:`~rheo_leads.jobfeed.upstream.Upstream`, the wall clock that stamps each page's
rows, and the capability re-read the run handler performs between pages.

:func:`collect_presets` is the one place the run's raw row count is bounded
(:data:`ROW_CAP`) and the only producer of the rows that detail reads and delivery
receive. A failed call or page stops its own search and counts one failure; the
other searches still run. A terminal upstream failure (a refused credential) is
raised, because every later call would be refused too.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

from rheo_leads.jobfeed.normalize import NormalizedPosting, apply_detail, normalize_row
from rheo_leads.jobfeed.presets import DISCOVERY_CAP, MAX_LIMIT, Preset
from rheo_leads.jobfeed.upstream import RunCut, Upstream, UpstreamFailure

ROW_CAP: Final = 80
MAX_DETAIL_READS: Final = 60
OVER_CAP_BAND: Final = "50+"


@dataclass(frozen=True)
class CollectResult:
    rows: tuple[NormalizedPosting, ...]
    """Valid rows in arrival order."""
    pages_used: int
    """Search pages requested (a call was made, whatever its outcome)."""
    search_failures: int
    invalid_rows: int
    cut: bool
    """The deadline stopped collection before every search finished."""
    capability_lost: bool
    upstream_failures: tuple[UpstreamFailure, ...] = ()
    """The failed calls among ``search_failures``, in order, for the run's health
    code and back-off."""


@dataclass(frozen=True)
class Sighting:
    posting: NormalizedPosting
    detail_ok: bool = False


@dataclass(frozen=True)
class DetailResult:
    sightings: tuple[Sighting, ...]
    detail_reads: int
    """``get`` calls made."""
    failures: int
    """Discovery postings downgraded because their read failed or was not made."""
    cut: bool


def _limit(params: Mapping[str, object]) -> int:
    limit = params.get("limit", MAX_LIMIT)
    return limit if type(limit) is int else MAX_LIMIT


def _page_jobs(
    document: Mapping[str, object], params: Mapping[str, object]
) -> list[object] | None:
    """The page's rows when it passes every check, else ``None``."""
    if document.get("status") != "ok":
        return None
    if document.get("filters_ignored") or document.get("filters_rejected"):
        return None
    mode = params.get("mode")
    if mode is not None and document.get("mode") != mode:
        return None
    jobs = document.get("jobs")
    if not isinstance(jobs, list) or len(jobs) > _limit(params):
        return None
    return jobs


def _continuation(
    document: Mapping[str, object], preset: Preset
) -> tuple[bool, object]:
    """Whether the page claims more, and the cursor it names for the next one."""
    if preset.action == "search":
        info = document.get("pageInfo")
        info = info if isinstance(info, Mapping) else {}
        return info.get("hasNextPage") is True, info.get("endCursor")
    return document.get("hasMore") is True, document.get("next_cursor")


def collect_presets(
    upstream: Upstream,
    presets: Sequence[Preset],
    org_uid: str,
    job_hosts: Sequence[str],
    *,
    clock: Callable[[], datetime],
    capability_on: Callable[[], bool],
) -> CollectResult:
    """Run each preset's pages in order (module docstring). ``search_ordinal`` on
    every row is its preset's index in ``presets``."""
    rows: list[NormalizedPosting] = []
    failures: list[UpstreamFailure] = []
    raw = pages = page_failures = invalid = 0
    terminal: UpstreamFailure | None = None

    def result(*, cut: bool = False, capability_lost: bool = False) -> CollectResult:
        return CollectResult(
            rows=tuple(rows),
            pages_used=pages,
            search_failures=page_failures + len(failures),
            invalid_rows=invalid,
            cut=cut,
            capability_lost=capability_lost,
            upstream_failures=tuple(failures),
        )

    for ordinal, preset in enumerate(presets):
        seen: set[str] = set()
        params: dict[str, object] = dict(preset.params)
        for page in range(preset.pages):
            if raw >= ROW_CAP:
                return result()
            if pages and not capability_on():
                return result(capability_lost=True)
            try:
                document = upstream.find_jobs(preset.action, org_uid, params)
            except RunCut:
                return result(cut=True)
            except UpstreamFailure as failure:
                pages += 1
                if failure.terminal:
                    terminal = failure
                else:
                    failures.append(failure)
                break
            pages += 1
            observed_at = clock()
            jobs = _page_jobs(document, params)
            if jobs is None:
                page_failures += 1
                break
            # The cap: the page that crosses it is truncated, and no page is
            # requested once it is reached.
            kept = jobs[: ROW_CAP - raw]
            raw += len(kept)
            for job in kept:
                posting = normalize_row(
                    job,
                    job_hosts,
                    observed_at=observed_at,
                    purpose=preset.purpose,
                    search_ordinal=ordinal,
                )
                if posting is None:
                    invalid += 1
                else:
                    rows.append(posting)
            more, cursor = _continuation(document, preset)
            if not more or page + 1 >= preset.pages or raw >= ROW_CAP:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                page_failures += 1
                break
            seen.add(cursor)
            params = {**params, "cursor": cursor}
        if terminal is not None:
            raise terminal
    return result()


def _preference(posting: NormalizedPosting) -> tuple[datetime, int, int]:
    exactness = {True: 2, False: 1, None: 0}[posting.proposal_count_exact]
    return (posting.observed_at, exactness, -posting.search_ordinal)


def merge_sightings(rows: Sequence[NormalizedPosting]) -> list[Sighting]:
    """One sighting per posting id, in first-seen order.

    The newest row (ties: an exact count over a band, then the earlier search, then
    the earlier row) supplies the competition fields, the observation time and every
    other feed field; ``applied`` holds if any row says so; the purpose is discovery
    if any row came from a discovery search.
    """
    groups: dict[str, list[NormalizedPosting]] = {}
    for row in rows:
        groups.setdefault(row.posting_id, []).append(row)
    merged: list[Sighting] = []
    for group in groups.values():
        preferred = group[0]
        for row in group[1:]:
            if _preference(row) > _preference(preferred):
                preferred = row
        merged.append(
            Sighting(
                replace(
                    preferred,
                    applied=any(row.applied for row in group),
                    purpose=(
                        "discover"
                        if any(row.purpose == "discover" for row in group)
                        else "refresh_only"
                    ),
                )
            )
        )
    return merged


def _eligible(posting: NormalizedPosting) -> bool:
    return (
        posting.purpose == "discover"
        and not posting.applied
        and posting.count_band_label != OVER_CAP_BAND
        and (posting.proposal_count is None or posting.proposal_count <= DISCOVERY_CAP)
    )


def _detail_id(posting_id: str) -> str:
    """The id a ``get`` takes: the posting token's digits after its ``~02``."""
    return posting_id.removeprefix("~02")


def _described(
    posting: NormalizedPosting, detail: dict[str, object], job_hosts: Sequence[str]
) -> NormalizedPosting | None:
    """``posting`` with the detail applied, or ``None`` when it carries no full
    description (``apply_detail`` then returns the posting itself)."""
    applied = apply_detail(posting, detail, job_hosts)
    return None if applied is posting or applied.description is None else applied


def _downgraded(posting: NormalizedPosting) -> Sighting:
    # No description: the saved one is preserved downstream, and a feed snippet is
    # never one.
    return Sighting(replace(posting, purpose="refresh_only", description=None))


def read_details(
    upstream: Upstream,
    sightings: Sequence[Sighting],
    org_uid: str,
    job_hosts: Sequence[str],
    *,
    deadline: float,
    max_reads: int = MAX_DETAIL_READS,
) -> DetailResult:
    """One ``find_jobs get`` per unique eligible posting, at most ``max_reads``,
    none starting with the ``deadline`` too close. A discovery posting whose read
    failed or was not made is downgraded to refresh-only with no description and
    counts one failure; other sightings pass through unchanged."""
    # Each posting id's one read: its detail document when it succeeded, else None.
    details: dict[str, dict[str, object] | None] = {}
    updated: list[Sighting] = []
    reads = failures = 0
    cut = False
    terminal: UpstreamFailure | None = None
    for sighting in sightings:
        posting = sighting.posting
        if not _eligible(posting):
            updated.append(sighting)
            continue
        key = posting.posting_id
        if key not in details:
            detail: dict[str, object] | None = None
            if not cut and reads < max_reads:
                try:
                    detail = upstream.find_jobs(
                        "get", org_uid, {"id": _detail_id(key)}, deadline=deadline
                    )
                except RunCut:
                    cut = True
                except UpstreamFailure as failure:
                    reads += 1
                    if failure.terminal:
                        terminal = failure
                else:
                    reads += 1
            if terminal is not None:
                raise terminal
            if detail is not None and _described(posting, detail, job_hosts) is None:
                detail = None
            details[key] = detail
            if detail is None:
                failures += 1
        found = details[key]
        described = None if found is None else _described(posting, found, job_hosts)
        updated.append(
            _downgraded(posting)
            if described is None
            else Sighting(described, detail_ok=True)
        )
    return DetailResult(
        sightings=tuple(updated), detail_reads=reads, failures=failures, cut=cut
    )
