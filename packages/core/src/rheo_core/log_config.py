"""The one logging setup shared by every process entry point (issue #228).

Without this module the tree had no ``logging.config`` and no formatter that reads
``extra``: the content-free operational lines the evidence paths log —
``evidence_extraction_failed``, ``evidence_record_failed``,
``automatic_memory_mentions_dropped``, ``automatic_memory_acceptance_deferred`` and
``evidence_acceptance_failed`` — reached stderr as a bare message name, and the only
diagnostics those lines carry (``error_type``, ``unit_count``, ``evidence_id``,
``sqlstate``, ...) were silently dropped. :func:`configure_logging` is the one call
each entry point (``apps/core``, ``apps/worker``) makes, once, before anything else
logs; :class:`JsonLogFormatter` is what renders ``extra`` at all, one JSON object per
line, so Coolify's captured stdout/stderr keeps every field instead of the message
name alone.

**The content-free guard never raises.** The five lines above are content-free by
design — a provider's exception carries no evidence text, so the log call passes
only a type name, a count or an id — and that is an invariant worth keeping
automatically rather than by review alone. :func:`content_free_extra` is what those
call sites wrap their ``extra`` mapping in, but every one of those call sites logs a
deliberately non-fatal failure (the runtime hook's recording failure that #197 made
never fail a user's run; the drain's deferral and acceptance-failed lines that
#215/#220 made back off a poison unit instead of rolling the drain back). A validator
that raised there would hand both bugs back the moment an unregistered field or an
overlong value reached it. So it sanitises instead: a field name outside
:data:`ALLOWED_EXTRA_FIELDS`, or a string value long enough to plausibly be quoted
evidence rather than a short diagnostic token, is dropped, and the count of dropped
fields is reported under the fixed, allowlisted ``extra_dropped`` key. What the
allowlist is *for* — proving every call site names only reviewed fields — is a static
check in ``tests/test_logging.py``, not a runtime raise.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "ALLOWED_EXTRA_FIELDS",
    "JsonLogFormatter",
    "configure_logging",
    "content_free_extra",
]

_RESERVED_RECORD_ATTRS: Final = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
) | {"message", "asctime"}
"""Every attribute a bare :class:`logging.LogRecord` already carries. Anything else on
the record came in through ``extra`` and is what :class:`JsonLogFormatter` renders
beside the standard fields."""


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger name, message, ``extra``.

    The formatter, not the log call, decides how ``extra`` is rendered — every call
    site keeps passing a plain ``dict``, and a JSON-lines sink (or a future key=value
    one) is a formatter swap in :func:`configure_logging`, not a change to the
    evidence paths that log."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


_CONFIGURED: Final = "_rheo_log_config_configured"


def configure_logging(level: int | str = logging.INFO) -> None:
    """Attach one stderr handler with :class:`JsonLogFormatter` to the root logger.

    Idempotent: a second call (a test fixture, a second lifespan in the same
    process) replaces the level but adds no second handler, keyed off a private
    attribute on the root logger rather than a module-level flag, so two
    interpreters that both import this module in one process (unlikely, but cheap
    to guard) do not double-log either.
    """
    root = logging.getLogger()
    root.setLevel(level)
    if getattr(root, _CONFIGURED, False):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLogFormatter())
    root.addHandler(handler)
    setattr(root, _CONFIGURED, True)


ALLOWED_EXTRA_FIELDS: Final = frozenset(
    {
        "error_type",
        "unit_count",
        "operation_id",
        "evidence_id",
        "attempts",
        "sqlstate",
        "mention_count",
        "extra_dropped",
    }
)
"""Every field name a content-free evidence log line may carry (issue #228's
acceptance: "a check that nothing in a field can carry evidence text ... perhaps
with an allowlist of field names"). Adding a field here is a deliberate, reviewed
decision that the new name can only ever hold a type name, a count or an id —
never a native key, a record body or anything a provider said. ``extra_dropped`` is
the one field :func:`content_free_extra` itself writes, never a call site."""

_MAX_FIELD_LENGTH: Final = 100
"""Longer than any id, count or exception class name this tree names
(``EvidenceWorkspaceUnresolved`` and its siblings are well under this), short
enough that a stray sentence of evidence text cannot pass as a token."""


def content_free_extra(fields: Mapping[str, object]) -> dict[str, object]:
    """Sanitise one content-free log line's ``extra`` before it is logged.

    Never raises: every one of this function's call sites logs a deliberately
    non-fatal failure (the runtime hook's recording failure, the drain's deferral
    and acceptance-failed lines), and a validator that raised there would fail a
    user's run or roll back a drain over a logging mistake — precisely the bugs
    #197, #215 and #220 already fixed. Instead, a field name outside
    :data:`ALLOWED_EXTRA_FIELDS`, or a string value longer than
    :data:`_MAX_FIELD_LENGTH`, is dropped, and the number dropped is reported under
    the fixed, allowlisted ``extra_dropped`` key so the drop itself is visible in
    the rendered line rather than silent.

    That every *call site* only ever names an allowlisted field is a static
    property, proved once for all of them by ``tests/test_logging.py``, not a
    runtime check — a call site cannot un-register a field at runtime, so nothing
    here needs to raise to keep it true.
    """
    sanitized: dict[str, object] = {}
    dropped = 0
    for key, value in fields.items():
        if key not in ALLOWED_EXTRA_FIELDS:
            dropped += 1
            continue
        if isinstance(value, str) and len(value) > _MAX_FIELD_LENGTH:
            dropped += 1
            continue
        sanitized[key] = value
    if dropped:
        sanitized["extra_dropped"] = dropped
    return sanitized
