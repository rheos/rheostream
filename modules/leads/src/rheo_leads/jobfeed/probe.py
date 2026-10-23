"""A temporary, opt-in look at the provider's invitations tool.

With the deployment setting ``leads.jobfeed.probe_invitations`` on, a run asks for
the invitations tool's help, lists one page of invitations as the selected account,
and keeps the first detail read's document; each goes to one INFO line. It is how
the invitations' parameters and response shape are learned without a hand-made
call: every session starts with a token refresh that rotates the stored refresh
token, so the probe rides inside a real run.

What reaches the log:

- **The help text**, as the provider wrote it, reduced to printable ASCII, joined
  on one line and cut at :data:`HELP_TEXT_MAX`. The help call carries no account
  and returns the provider's own documentation of a tool, never account data.
- **Shapes, never values.** :func:`shape` keeps a JSON document's keys that pass a
  fixed pattern, list lengths, and for each scalar only its kind: ``int``,
  ``bool``, ``digits19``, ``posting_id``, ``time``, a URL's host with its path
  reduced to kinds, an upper-snake enum value, or ``text`` with a length bucket.
  Lower-case words are kept only under a status-like key. No name, title, id,
  description or message survives.

It is best effort: a failed call is logged by its code, a run cut ends the probe,
and either way the run goes on to collect exactly as it would have. Remove the
setting once the shapes are known.
"""

import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Final
from urllib.parse import urlsplit

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
HELP_TEXT_MAX: Final = 8000
SHAPE_DEPTH: Final = 10
SHAPE_KEYS: Final = 60
SHAPE_ITEMS: Final = 3
"""How many leading list items have their shapes merged."""

_KEY: Final = re.compile(r"[A-Za-z0-9_.:@$-]{1,64}")
_ENUM: Final = re.compile(r"[A-Z][A-Z0-9_]{0,39}")
_WORD: Final = re.compile(r"[a-z][a-z_]{0,23}")
_POSTING_ID: Final = re.compile(r"~0[12][0-9a-f]+")
_DIGITS: Final = re.compile(r"[0-9]+")
_HOST: Final = re.compile(r"[a-z0-9.-]{1,64}")
_STATUS_KEYS: Final = frozenset({"status", "state", "type", "kind", "mode", "role"})
_NOT_PRINTABLE: Final = re.compile(r"[^\x20-\x7e]+")


def _text_kind(value: str) -> str:
    if _DIGITS.fullmatch(value):
        return f"digits{len(value)}"
    if _POSTING_ID.fullmatch(value):
        return "posting_id" if value.startswith("~02") else "tilde01"
    if value.startswith("https://") or value.startswith("http://"):
        parts = urlsplit(value)
        host = parts.hostname or ""
        segments = [
            _segment_kind(segment) for segment in parts.path.split("/") if segment
        ]
        shown = host if _HOST.fullmatch(host) else "?"
        query = "?q" if parts.query else ""
        return f"url:{shown}/{'/'.join(segments)}{query}"
    try:
        datetime.fromisoformat(value)
    except ValueError:
        pass
    else:
        return "time"
    if _ENUM.fullmatch(value):
        return f"enum:{value}"
    length = len(value)
    if length == 0:
        return "text0"
    if length < 32:
        return "text<32"
    return "text<256" if length < 256 else "text256+"


def _segment_kind(segment: str) -> str:
    if _DIGITS.fullmatch(segment):
        return "N"
    token = segment.rsplit("_", 1)[-1]
    if _POSTING_ID.fullmatch(token):
        return "posting_id" if token.startswith("~02") else "tilde01"
    # Path words such as ``proposals`` or ``jobs`` say which page a URL opens;
    # anything longer, or mixed, may be a title slug.
    return segment if _WORD.fullmatch(segment) else "s"


def shape(value: object, *, key: str | None = None, depth: int = 0) -> object:
    """The value's shape with every value replaced by its kind (module docstring)."""
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
        if key in _STATUS_KEYS and _WORD.fullmatch(value):
            return f"word:{value}"
        return _text_kind(value)
    if isinstance(value, Mapping):
        kept: dict[str, object] = {}
        skipped = 0
        for name, item in value.items():
            if (
                not isinstance(name, str)
                or not _KEY.fullmatch(name)
                or _DIGITS.fullmatch(name)
                or _POSTING_ID.fullmatch(name)
            ):
                # An id used as a key is a value, so it is counted, not kept.
                skipped += 1
            elif len(kept) == SHAPE_KEYS:
                skipped += 1
            else:
                kept[name] = shape(item, key=name, depth=depth + 1)
        if skipped:
            kept["?skipped"] = skipped
        return kept
    if isinstance(value, list):
        items = [shape(item, key=key, depth=depth + 1) for item in value[:SHAPE_ITEMS]]
        distinct: list[object] = []
        for item in items:
            if item not in distinct:
                distinct.append(item)
        return {"?len": len(value), "?items": distinct}
    return type(value).__name__


def help_text(result: Mapping[str, object]) -> str:
    """The result's text blocks as one printable ASCII line, cut to length."""
    content = result.get("content")
    blocks = content if isinstance(content, list) else []
    texts = [
        block["text"]
        for block in blocks
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    flat = " ".join(" ".join(_NOT_PRINTABLE.sub(" ", text).split()) for text in texts)
    return flat[:HELP_TEXT_MAX]


def _code(failure: UpstreamFailure) -> str:
    if failure.code == "tool_error" and failure.tool_code:
        return f"tool_error:{failure.tool_code}"
    return failure.code


def _dumps(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def probe_invitations(upstream: Upstream, org_uid: str, tool_prefix: str) -> None:
    """The help line, then the invitations line; never raises (module docstring).

    The help is asked for under the name ``tools/list`` gives (with the prefix),
    then, if that fails, under the bare name."""
    names = [tool_prefix + PROPOSALS]
    if tool_prefix:
        names.append(PROPOSALS)
    try:
        for name in names:
            try:
                result = upstream.tool_help(name)
            except UpstreamFailure as failure:
                _log.info(HELP_LOG, name, _code(failure), '""')
                continue
            _log.info(HELP_LOG, name, "ok", json.dumps(help_text(result)))
            break
        try:
            document = upstream.invitations(org_uid, {})
        except UpstreamFailure as failure:
            _log.info(INVITATIONS_LOG, _code(failure), "null")
        else:
            _log.info(INVITATIONS_LOG, "ok", _dumps(shape(document)))
    except RunCut:
        _log.info(INVITATIONS_LOG, RunCut.code, "null")


def log_detail_shape(detail: dict[str, object] | None) -> None:
    """The first detail read's shape, or ``none`` when no read succeeded."""
    if detail is None:
        _log.info(DETAIL_LOG, "none", "null")
    else:
        _log.info(DETAIL_LOG, "ok", _dumps(shape(detail)))
