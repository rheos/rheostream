"""A temporary, opt-in look at the provider's invitations and proposal tools.

With the deployment setting ``leads.jobfeed.probe_invitations`` on, a run asks for
the help of each tool in :data:`HELP_TOOLS` (the invitations listing and the
proposal, milestone and attachment tools a later proposal submission would use),
lists one page of invitations as the selected account, and keeps the first detail
read's document; each goes to one INFO line. Only the help and the invitations
listing are ever called: the other tools are named as ``get_tool_help`` arguments,
never called. It is how the parameters and response shapes are learned without a
hand-made call: every session starts with a token refresh that rotates the stored
refresh token, so the probe rides inside a real run.

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

It is best effort: a failed or refused call is logged by its code, a rate limit
ends the probe after the provider's wait, a run cut ends it at once, a shape that
cannot be built is logged as ``shape_failed``, and the run goes on to collect
exactly as it would have. A cancelled or lost job still stops the run. Remove the
setting once the shapes are known.
"""

import json
import logging
import re
from collections.abc import Collection, Mapping
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
DETAIL_LOG: Final = "jobfeed_probe_detail outcome=%s shape=%s"
HELP_TEXT_MAX: Final = 16000
HELP_TOOLS: Final = (
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
    "get_rate_insights",
    "get_freelancer_dashboard",
)
"""The tools whose help the probe asks for, by their unprefixed names. Naming a
tool here sends its name to ``get_tool_help``; it never calls the tool."""
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
    """The probe ends here; the run goes on."""


def _failed(upstream: Upstream, failure: UpstreamFailure) -> None:
    """After a rate limit, wait out the provider and end the probe, so the
    searches do not start into the same limit."""
    if failure.code == "rate_limited":
        upstream.hold_off(failure.retry_after)
        raise _Stop()


def _help(
    upstream: Upstream, tool: str, org_uid: str, prefixes: list[str]
) -> list[str]:
    """One tool's help line. The help is asked for under each of ``prefixes`` in
    turn until one answers; the prefixes left to try for the next tool are
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
            raise _Stop() from None
        _log.info(HELP_LOG, name, "ok", json.dumps(help_text(result, (org_uid,))))
        return [prefix]
    return prefixes


def probe_invitations(
    upstream: Upstream,
    org_uid: str,
    tool_prefix: str,
    hosts: Collection[str] = (),
) -> None:
    """A help line per :data:`HELP_TOOLS` entry, then the invitations line; never
    raises for anything the provider answers (module docstring). The listed
    (prefixed) name is tried first, then the bare one."""
    prefixes = [tool_prefix, ""] if tool_prefix else [""]
    try:
        for tool in HELP_TOOLS:
            prefixes = _help(upstream, tool, org_uid, prefixes)
        try:
            document = upstream.invitations(org_uid, {})
        except UpstreamFailure as failure:
            _log.info(INVITATIONS_LOG, _code(failure), "null")
            _failed(upstream, failure)
        except ToolRefused:
            _log.info(INVITATIONS_LOG, REFUSED, "null")
        else:
            _log.info(INVITATIONS_LOG, *_dumps_shape(document, hosts))
    except RunCut:
        _log.info(INVITATIONS_LOG, RunCut.code, "null")
    except _Stop:
        pass


def log_detail_shape(
    detail: dict[str, object] | None, hosts: Collection[str] = ()
) -> None:
    """The first detail read's shape, or ``none`` when no read succeeded."""
    if detail is None:
        _log.info(DETAIL_LOG, "none", "null")
    else:
        _log.info(DETAIL_LOG, *_dumps_shape(detail, hosts))
