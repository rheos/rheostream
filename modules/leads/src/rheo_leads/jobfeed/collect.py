"""Collection: the invitations, the search pages, the merge of overlapping sightings,
the detail reads.

No database access. Every collaborator arrives as an argument: the run's
:class:`~rheo_leads.jobfeed.upstream.Upstream`, the wall clock that stamps each page's
rows, and the capability re-read the run handler performs between pages.

:func:`collect_invitations` lists the account's pending invitations first, one page,
best effort. :func:`collect_presets` is the one place the run's raw search row count
is bounded (:data:`ROW_CAP`). Together they are the only producers of the rows that
detail reads and delivery receive. A failed call or page stops its own search (or
the listing) and counts one failure; the rest still run. A terminal upstream failure
(a refused credential) is raised, because every later call would be refused too.
"""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final

from rheo_leads.jobfeed.normalize import (
    NormalizedPosting,
    apply_detail,
    normalize_invitation,
    normalize_row,
)
from rheo_leads.jobfeed.presets import DISCOVERY_CAP, MAX_LIMIT, Preset
from rheo_leads.jobfeed.upstream import RunCut, Upstream, UpstreamFailure

ROW_CAP: Final = 80
INVITATIONS_LIMIT: Final = 10
"""The most invitations one run lists: the provider's page size. The search row
cap does not count them."""
INVITATIONS_FAILED: Final = "invitations"
"""The run-history reason prefix for a failed invitations listing."""
INVITATIONS_MORE: Final = "invitations:more"
"""The run-history reason for a listing that had more than one page."""
SEARCHES_FAILED: Final = "searches"
"""The run-history reason prefix for searches that all failed in a run that still
delivered an invitation."""
MAX_DETAIL_READS: Final = 60
# The job service refuses ``find_jobs get`` once the account's daily allowance of
# detail reads is spent (#377: about 400 a day since Nov 6 2026, resetting at
# 00:00 UTC), and with PERMISSION alone for the odd private job. This many
# refusals in a row means the allowance is spent: the run reads nothing more.
REFUSED_TOOL_CODES: Final = frozenset({"PERMISSION", "JOB_BROWSE_COOLDOWN"})
STOP_AFTER_REFUSALS: Final = 5
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
    pages_ok: int = 0
    """Pages that passed every page check."""


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
    reasons: Mapping[str, int] = field(default_factory=dict)
    known: int = 0
    """Postings not read because their description is already stored (#377)."""
    """Why each of the ``failures`` happened, as fixed codes and their counts
    (:func:`read_failure_reason`): the run history keeps these, content-free."""


# Why a discovery posting's detail read gave no description. Only these codes, an
# upstream code, or ``tool_error:<CODE>`` ever reach the run history.
NO_FEED_ID: Final = "no_feed_id"
NOT_READ_DEADLINE: Final = "not_read_deadline"
NOT_READ_CAP: Final = "not_read_cap"
NOT_READ_REFUSED: Final = "not_read_refused"
"""Not read: the run stopped reading after ``STOP_AFTER_REFUSALS`` refusals in a row."""
NO_DESCRIPTION: Final = "no_description"


def read_failure_reason(failure: UpstreamFailure) -> str:
    """An upstream failure as a run-history reason: its fixed code, and for a
    tool error the tool's own code when it gave one."""
    if failure.code == "tool_error" and failure.tool_code:
        return f"tool_error:{failure.tool_code}"
    return failure.code


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
    raw = pages = pages_ok = page_failures = invalid = 0
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
            pages_ok=pages_ok,
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
            pages_ok += 1
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
            if not more:
                break
            # A page that claims more must name a fresh cursor, even when no
            # further page will be requested.
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                page_failures += 1
                break
            if page + 1 >= preset.pages or raw >= ROW_CAP:
                break
            seen.add(cursor)
            params = {**params, "cursor": cursor}
        if terminal is not None:
            raise terminal
    return result()


@dataclass(frozen=True)
class InvitationResult:
    rows: tuple[NormalizedPosting, ...]
    """The invited jobs, valid rows only, in the listing's order."""
    invalid_rows: int
    failure: UpstreamFailure | None = None
    """The listing's failed call, when it failed; collection goes on without it."""
    cut: bool = False
    more: bool = False
    """The provider said more invitations wait past this page; they are read on a
    later run once earlier ones are answered."""


def invitation_reason(failure: UpstreamFailure) -> str:
    """A failed invitations listing as a run-history reason, content-free."""
    return f"{INVITATIONS_FAILED}:{read_failure_reason(failure)}"


def collect_invitations(
    upstream: Upstream,
    org_uid: str,
    job_hosts: Sequence[str],
    *,
    clock: Callable[[], datetime],
) -> InvitationResult:
    """The account's pending invitations, one page of at most
    :data:`INVITATIONS_LIMIT`, best effort: a failed call or page is returned as a
    failure and the run carries on to the searches. A terminal failure (a refused
    credential) is raised, because every later call would be refused too.

    Each row's job becomes an invited discovery posting
    (:func:`~rheo_leads.jobfeed.normalize.normalize_invitation`), so it is read in
    detail and delivered like a feed row. Its ``search_ordinal`` is -1, ahead of
    every search."""
    try:
        document = upstream.invitations(org_uid, {"limit": INVITATIONS_LIMIT})
    except RunCut:
        return InvitationResult(rows=(), invalid_rows=0, cut=True)
    except UpstreamFailure as failure:
        if failure.terminal:
            raise
        return InvitationResult(rows=(), invalid_rows=0, failure=failure)
    observed_at = clock()
    data = document.get("data")
    listed = data.get("invitations") if isinstance(data, Mapping) else None
    if (
        document.get("status") != "ok"
        or not isinstance(listed, list)
        or len(listed) > INVITATIONS_LIMIT
    ):
        return InvitationResult(
            rows=(), invalid_rows=0, failure=UpstreamFailure("protocol")
        )
    rows: list[NormalizedPosting] = []
    invalid = 0
    for entry in listed:
        posting = normalize_invitation(
            entry, job_hosts, observed_at=observed_at, search_ordinal=-1
        )
        if posting is None:
            invalid += 1
        else:
            rows.append(posting)
    return InvitationResult(
        rows=tuple(rows), invalid_rows=invalid, more=document.get("hasMore") is True
    )


def _preference(posting: NormalizedPosting) -> tuple[datetime, int, int]:
    exactness = {True: 2, False: 1, None: 0}[posting.proposal_count_exact]
    return (posting.observed_at, exactness, -posting.search_ordinal)


def _applied(group: Sequence[NormalizedPosting]) -> bool | None:
    """True if any row says so, false if one says not, else unknown."""
    flags = [row.applied for row in group]
    if any(flags):
        return True
    return False if any(flag is False for flag in flags) else None


def merge_sightings(rows: Sequence[NormalizedPosting]) -> list[Sighting]:
    """One sighting per posting id, in first-seen order.

    The newest row (ties: an exact count over a band, then the earlier search, then
    the earlier row) supplies the competition fields, the observation time and every
    other feed field; ``applied`` and ``invited`` hold if any row says so (``applied``
    stays unknown when only invitations were seen); the purpose is discovery if any
    row came from a discovery search or an invitation.
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
                    applied=_applied(group),
                    invited=any(row.invited for row in group),
                    feed_id=preferred.feed_id
                    or next((row.feed_id for row in group if row.feed_id), None),
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
    """Whether a posting gets a detail read. An invited job always does: the
    client asked, so the discovery filters (applied, the proposal cap) do not
    apply."""
    return posting.invited or (
        posting.purpose == "discover"
        and not posting.applied
        and posting.count_band_label != OVER_CAP_BAND
        and (posting.proposal_count is None or posting.proposal_count <= DISCOVERY_CAP)
    )


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


def _unread_known(sighting: Sighting) -> Sighting:
    """A posting not read again because its description is stored, without the
    client figures a detail read supplies (spend, rating, reviews, hire rate).
    The feed row's own versions of those differ from the detail's, so carrying
    them would make every known posting look changed and overwrite the stored
    detail figures each run; left out, the stored ones stand."""
    return Sighting(
        replace(
            sighting.posting,
            client_spend=None,
            client_rating=None,
            client_reviews=None,
            client_hire_rate=None,
        )
    )


def read_details(
    upstream: Upstream,
    sightings: Sequence[Sighting],
    org_uid: str,
    job_hosts: Sequence[str],
    *,
    deadline: float,
    max_reads: int = MAX_DETAIL_READS,
    known: frozenset[str] = frozenset(),
) -> DetailResult:
    """One ``find_jobs get`` per unique eligible posting, at most ``max_reads``,
    none starting with the ``deadline`` too close. A ``get`` sends the feed row's
    own id; a posting whose row carried none gets no read. A discovery posting
    whose read failed or was not made is downgraded to refresh-only with no
    description and counts one failure; other sightings pass through unchanged.

    A posting in ``known`` already has its description stored, so it is not read
    again (#377): it passes through as a refresh, its stored description kept,
    and the daily allowance goes on postings not seen before. After
    ``STOP_AFTER_REFUSALS`` refusals in a row the allowance is spent, and every
    posting still unread counts ``not_read_refused`` instead of a call."""
    # Each posting id's one read: its detail document when it succeeded, else None.
    details: dict[str, dict[str, object] | None] = {}
    updated: list[Sighting] = []
    reads = failures = 0
    reasons: Counter[str] = Counter()
    cut = False
    skipped_known = refusals = 0
    terminal: UpstreamFailure | None = None
    for sighting in sightings:
        posting = sighting.posting
        if not _eligible(posting):
            updated.append(sighting)
            continue
        key = posting.posting_id
        if key in known:
            skipped_known += 1
            updated.append(_unread_known(sighting))
            continue
        if key not in details:
            detail: dict[str, object] | None = None
            reason: str | None = None
            feed_id = posting.feed_id
            if feed_id is None:
                reason = NO_FEED_ID
            elif cut:
                reason = NOT_READ_DEADLINE
            elif reads >= max_reads:
                reason = NOT_READ_CAP
            elif refusals >= STOP_AFTER_REFUSALS:
                reason = NOT_READ_REFUSED
            else:
                try:
                    detail = upstream.find_jobs(
                        "get", org_uid, {"id": feed_id}, deadline=deadline
                    )
                except RunCut:
                    cut = True
                    reason = NOT_READ_DEADLINE
                except UpstreamFailure as failure:
                    reads += 1
                    reason = read_failure_reason(failure)
                    refused = (
                        failure.code == "tool_error"
                        and failure.tool_code in REFUSED_TOOL_CODES
                    )
                    refusals = refusals + 1 if refused else 0
                    if failure.terminal:
                        terminal = failure
                else:
                    reads += 1
                    refusals = 0
            if terminal is not None:
                raise terminal
            if detail is not None and _described(posting, detail, job_hosts) is None:
                detail = None
                reason = NO_DESCRIPTION
            details[key] = detail
            if detail is None:
                failures += 1
                reasons[reason or NO_DESCRIPTION] += 1
        found = details[key]
        described = None if found is None else _described(posting, found, job_hosts)
        updated.append(
            _downgraded(posting)
            if described is None
            else Sighting(described, detail_ok=True)
        )
    return DetailResult(
        sightings=tuple(updated),
        detail_reads=reads,
        failures=failures,
        cut=cut,
        reasons=dict(reasons),
        known=skipped_known,
    )
