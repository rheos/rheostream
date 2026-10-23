"""A temporary, opt-in look at the provider's invitations and every tool's help.

With the deployment setting ``leads.jobfeed.probe_invitations`` on, a run is a
probe instead of a collection (:func:`probe_run`): it lists one page of
invitations as the selected account, reads one search page and the detail of its
first row, then asks for the help of every tool in :data:`HELP_TOOLS`; each answer
goes to one INFO line. Nothing is delivered and nothing is called beyond the help,
the invitations listing and the usual ``find_jobs``: the other tools are named as
``get_tool_help`` arguments, never called. It is how parameters and response
shapes are learned without a hand-made call: every session starts with a token
refresh that rotates the stored refresh token, so the probe rides inside a real
run. The help calls take most of a run's budget, which is why the run collects
nothing else.

What reaches the log:

- **The help text**, as the provider wrote it, reduced to printable ASCII, joined
  on one line, redacted and cut at :data:`HELP_TEXT_MAX`. The help call carries no
  account and returns the provider's own documentation of a tool, never account
  data; even so, the organization id, any run of ten or more digits and any long
  run that mixes letters and digits (a credential's shape) are replaced with
  ``[redacted]`` before it is cut.
- **Shapes, never values.** :func:`shape` keeps a JSON document's keys that pass a
  fixed identifier pattern (a key carrying a long digit run is counted, not kept),
  list lengths, and for each scalar only its kind: ``int``, ``bool``, ``digits19``,
  ``posting_id``, ``time``, ``text`` or ``upper`` with a length bucket, or a URL
  whose host is shown only when it is one of the run's own hosts and whose path
  keeps only the words in :data:`PATH_WORDS`. A word or upper-snake value is shown
  only under a status-like key (:data:`STATUS_KEYS`). No name, title, id,
  description or message survives.

It never raises for anything the provider answers: a failed or refused call is
logged by its code, a rate limit ends the probe after the provider's wait, a run
cut ends it at once, and a shape that cannot be built is logged as
``shape_failed``. A cancelled or lost job still stops the run. Remove the setting
once the shapes are known.
"""

import json
import logging
import re
from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from typing import Final
from urllib.parse import urlsplit

from rheo_core.connectors.mcp_client import ToolRefused

from rheo_leads.jobfeed.upstream import (
    PROPOSALS,
    RunCut,
    Upstream,
    UpstreamFailure,
)

_log = logging.getLogger(__name__)

HELP_LOG: Final = "jobfeed_probe_help tool=%s outcome=%s text=%s"
INVITATIONS_LOG: Final = "jobfeed_probe_invitations outcome=%s shape=%s"
SEARCH_LOG: Final = "jobfeed_probe_search outcome=%s shape=%s"
DETAIL_LOG: Final = "jobfeed_probe_detail outcome=%s shape=%s"
DONE_LOG: Final = "jobfeed_probe_done outcome=%s helps=%d of=%d"
HELP_TEXT_MAX: Final = 16000
HELP_TOOLS: Final = (
    # What a proposal submission from Leads would use, first.
    PROPOSALS,
    "manage_proposals",
    "get_draft",
    "confirm_draft",
    "get_preview",
    "confirm_preview",
    "find_jobs",
    "list_milestones",
    "manage_milestones",
    "submit_milestones",
    "start_attachment_upload",
    "confirm_attachment_upload",
    "get_upload_status",
    "store_uploaded_files",
    "get_rate_insights",
    "get_freelancer_dashboard",
    # Then every other tool the provider lists.
    "agency_rooms",
    "boost_profile",
    "end_contract",
    "find_freelancers",
    "find_saved_jobs",
    "get_account",
    "get_agency",
    "get_agency_dashboard",
    "get_client_dashboard",
    "get_client_financials",
    "get_freelancer_financials",
    "get_job_posting",
    "get_messages",
    "get_profile",
    "get_tool_help",
    "invite_freelancer",
    "list_accounts",
    "list_client_invitations",
    "list_client_proposals",
    "list_contracts",
    "list_offers",
    "manage_client_proposals",
    "manage_meetings",
    "manage_offers",
    "manage_talent_lists",
    "manage_uma_shortlist",
    "post_job",
    "respond_to_offer",
    "save_job",
    "send_message",
    "set_tool_mode",
    "set_tool_permission",
    "submit_feedback",
    "update_account",
    "update_agency",
    "update_contract",
    "update_profile",
)
"""The tools whose help the probe asks for, by their unprefixed names: all 53 the
provider listed on 2026-10-22. Naming a tool here sends its name to
``get_tool_help``; it never calls the tool."""
SEARCH_PARAMS: Final = {"mode": "best_match", "limit": 10}
"""The one search page a probe reads, for its own shape and a row to read."""
REDACTED: Final = "[redacted]"
SHAPE_FAILED: Final = "shape_failed"
REFUSED: Final = "refused"
SHAPE_DEPTH: Final = 10
SHAPE_KEYS: Final = 60
SHAPE_ITEMS: Final = 3
"""How many leading list items have their shapes merged."""
STATUS_KEYS: Final = frozenset(
    {"status", "state", "type", "kind", "mode", "role", "level", "category"}
)
"""Keys whose word or upper-snake value is shown: an enum, not a name."""
PATH_WORDS: Final = frozenset(
    {
        "ab",
        "api",
        "apply",
        "details",
        "freelancers",
        "interview",
        "invitations",
        "job",
        "jobs",
        "nx",
        "offers",
        "proposals",
        "uid",
    }
)
"""URL path segments shown as they are; any other segment is reduced to a kind."""

_KEY: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,40}")
_KEY_DIGITS: Final = re.compile(r"[0-9]{5,}")
_HEX_KEY: Final = re.compile(r"[0-9a-fA-F]{12,}")
_ENUM: Final = re.compile(r"[A-Z][A-Z0-9_]{0,39}")
_WORD: Final = re.compile(r"[a-z][a-z_]{0,23}")
_POSTING_ID: Final = re.compile(r"~0[12][0-9a-f]+")
_DIGITS: Final = re.compile(r"[0-9]+")
_NOT_PRINTABLE: Final = re.compile(r"[^\x20-\x7e]+")
_LONG_RUN: Final = re.compile(r"[A-Za-z0-9._~+/=-]{24,}")
_DIGIT_RUN: Final = re.compile(r"[0-9]{10,}")
_FEED_ID: Final = re.compile(r"[0-9A-Za-z_~-]{1,64}")
"""What a detail read may send as a row's id, as collection accepts it."""


def _bucket(length: int) -> str:
    if length == 0:
        return "0"
    if length < 32:
        return "<32"
    return "<256" if length < 256 else "256+"


def _id_kind(value: str) -> str | None:
    if _DIGITS.fullmatch(value):
        return f"digits{len(value)}"
    if _POSTING_ID.fullmatch(value):
        return "posting_id" if value.startswith("~02") else "tilde01"
    return None


def _url_kind(value: str, hosts: Collection[str]) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
    except ValueError:
        return "url:?"
    segments = [_segment_kind(segment) for segment in parts.path.split("/") if segment]
    shown = host if host in hosts else "host"
    query = "?q" if parts.query else ""
    return f"url:{shown}/{'/'.join(segments)}{query}"


def _segment_kind(segment: str) -> str:
    if segment in PATH_WORDS:
        return segment
    kind = _id_kind(segment) or _id_kind(segment.rsplit("_", 1)[-1])
    if kind is not None:
        return "N" if kind.startswith("digits") else kind
    return "s"


def _text_kind(value: str, hosts: Collection[str]) -> str:
    kind = _id_kind(value)
    if kind is not None:
        return kind
    if value.startswith("https://") or value.startswith("http://"):
        return _url_kind(value, hosts)
    try:
        datetime.fromisoformat(value)
    except ValueError:
        pass
    else:
        return "time"
    if _ENUM.fullmatch(value):
        return f"upper{_bucket(len(value))}"
    return f"text{_bucket(len(value))}"


def _key_kept(name: object) -> bool:
    """An identifier-shaped key that is not itself an id: no five-digit run and
    not a long hexadecimal string. A UUID or an email fails the pattern."""
    return (
        isinstance(name, str)
        and _KEY.fullmatch(name) is not None
        and _KEY_DIGITS.search(name) is None
        and _HEX_KEY.fullmatch(name) is None
    )


def shape(
    value: object,
    *,
    hosts: Collection[str] = (),
    key: str | None = None,
    depth: int = 0,
) -> object:
    """The value's shape with every value replaced by its kind (module docstring).
    ``hosts`` are the hosts a URL may name in the shape."""
    if depth >= SHAPE_DEPTH:
        return "..."
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "num"
    if isinstance(value, str):
        if key in STATUS_KEYS and (_WORD.fullmatch(value) or _ENUM.fullmatch(value)):
            return f"word:{value}"
        return _text_kind(value, hosts)
    if isinstance(value, Mapping):
        kept: dict[str, object] = {}
        skipped = 0
        for name, item in value.items():
            if not _key_kept(name) or len(kept) == SHAPE_KEYS:
                # A key that may be data (an id, an address) is counted, not kept.
                skipped += 1
            else:
                kept[name] = shape(item, hosts=hosts, key=name, depth=depth + 1)
        if skipped:
            kept["?skipped"] = skipped
        return kept
    if isinstance(value, list):
        items = [
            shape(item, hosts=hosts, key=key, depth=depth + 1)
            for item in value[:SHAPE_ITEMS]
        ]
        distinct: list[object] = []
        for item in items:
            if item not in distinct:
                distinct.append(item)
        return {"?len": len(value), "?items": distinct}
    return type(value).__name__


def _credential_shaped(match: re.Match[str]) -> str:
    run = match.group(0)
    mixed = any(c.isdigit() for c in run) and any(c.isalpha() for c in run)
    return REDACTED if mixed else run


def help_text(result: Mapping[str, object], secrets: tuple[str, ...] = ()) -> str:
    """The result's text blocks as one printable ASCII line, redacted (module
    docstring), then cut to length."""
    content = result.get("content")
    blocks = content if isinstance(content, list) else []
    texts = [
        block["text"]
        for block in blocks
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    flat = " ".join(" ".join(_NOT_PRINTABLE.sub(" ", text).split()) for text in texts)
    for secret in secrets:
        if secret:
            flat = flat.replace(secret, REDACTED)
    flat = _DIGIT_RUN.sub(REDACTED, flat)
    flat = _LONG_RUN.sub(_credential_shaped, flat)
    return flat[:HELP_TEXT_MAX]


def _code(failure: UpstreamFailure) -> str:
    if failure.code == "tool_error" and failure.tool_code:
        return f"tool_error:{failure.tool_code}"
    return failure.code


def _dumps_shape(document: object, hosts: Collection[str]) -> tuple[str, str]:
    """``("ok", shape)``, or ``("shape_failed", "null")`` when it cannot be built."""
    try:
        return "ok", json.dumps(
            shape(document, hosts=hosts), separators=(",", ":"), sort_keys=True
        )
    except Exception:
        return SHAPE_FAILED, "null"


class _Stop(Exception):
    """The probe ends here, with ``code``; the run then ends as a probe."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _failed(upstream: Upstream, failure: UpstreamFailure) -> None:
    """After a rate limit, wait out the provider and end the probe, so no further
    call starts into the same limit."""
    if failure.code == "rate_limited":
        upstream.hold_off(failure.retry_after)
        raise _Stop(failure.code)


def _document(
    upstream: Upstream, line: str, call: Callable[[], dict[str, object]]
) -> dict[str, object] | None:
    """One probe call's document, or ``None`` after its failure line."""
    try:
        return call()
    except UpstreamFailure as failure:
        _log.info(line, _code(failure), "null")
        _failed(upstream, failure)
        return None
    except ToolRefused:
        _log.info(line, REFUSED, "null")
        raise _Stop(REFUSED) from None


def _first_feed_id(page: Mapping[str, object] | None) -> str | None:
    """The first row's own id on a search page, when it is a short plain token."""
    jobs = page.get("jobs") if page is not None else None
    first = jobs[0] if isinstance(jobs, list) and jobs else None
    found = first.get("id") if isinstance(first, Mapping) else None
    return found if isinstance(found, str) and _FEED_ID.fullmatch(found) else None


def _help(
    upstream: Upstream, tool: str, org_uid: str, prefixes: list[str]
) -> tuple[list[str], bool]:
    """One tool's help line, and whether it answered. The help is asked for under
    each of ``prefixes`` in turn until one answers; the prefixes left to try are
    returned, so once a form of the name works, only that form is sent."""
    for prefix in prefixes:
        name = prefix + tool
        try:
            result = upstream.tool_help(name)
        except UpstreamFailure as failure:
            _log.info(HELP_LOG, name, _code(failure), '""')
            _failed(upstream, failure)
            continue
        except ToolRefused:
            _log.info(HELP_LOG, name, REFUSED, '""')
            raise _Stop(REFUSED) from None
        _log.info(HELP_LOG, name, "ok", json.dumps(help_text(result, (org_uid,))))
        return [prefix], True
    return prefixes, False


def probe_run(
    upstream: Upstream,
    org_uid: str,
    tool_prefix: str,
    hosts: Collection[str] = (),
) -> None:
    """The invitations, search and detail lines, a help line per
    :data:`HELP_TOOLS` entry, then one ``jobfeed_probe_done`` line saying how it
    ended and how many tools answered; never raises for anything the provider
    answers (module docstring). The listed (prefixed) tool name is tried first,
    then the bare one."""
    answered = 0
    outcome = "ok"
    try:
        invitations = _document(
            upstream, INVITATIONS_LOG, lambda: upstream.invitations(org_uid, {})
        )
        if invitations is not None:
            _log.info(INVITATIONS_LOG, *_dumps_shape(invitations, hosts))
        page = _document(
            upstream,
            SEARCH_LOG,
            lambda: upstream.find_jobs("smart_search", org_uid, SEARCH_PARAMS),
        )
        if page is not None:
            _log.info(SEARCH_LOG, *_dumps_shape(page, hosts))
        feed_id = _first_feed_id(page)
        detail = None
        if feed_id is None:
            _log.info(DETAIL_LOG, "none", "null")
        else:
            detail = _document(
                upstream,
                DETAIL_LOG,
                lambda: upstream.find_jobs("get", org_uid, {"id": feed_id}),
            )
        if detail is not None:
            _log.info(DETAIL_LOG, *_dumps_shape(detail, hosts))
        prefixes = [tool_prefix, ""] if tool_prefix else [""]
        for tool in HELP_TOOLS:
            prefixes, ok = _help(upstream, tool, org_uid, prefixes)
            answered += ok
    except RunCut:
        outcome = RunCut.code
    except _Stop as stop:
        outcome = stop.code
    _log.info(DONE_LOG, outcome, answered, len(HELP_TOOLS))
