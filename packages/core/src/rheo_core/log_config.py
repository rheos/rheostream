"""The one logging setup shared by every process entry point (issue #229).

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

**The content-free guard.** The five lines above are content-free by design — a
provider's exception carries no evidence text, so the log call passes only a type
name, a count or an id — and that is an invariant worth keeping automatically rather
than by review alone. :func:`content_free_extra` is what those call sites wrap their
``extra`` mapping in: a field name outside :data:`ALLOWED_EXTRA_FIELDS`, or a string
value long enough to plausibly be quoted evidence rather than a short diagnostic
token, raises :class:`ContentFreeExtraError` before the record reaches a handler.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "ALLOWED_EXTRA_FIELDS",
    "ContentFreeExtraError",
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
    }
)
"""Every field name a content-free evidence log line may carry (issue #229's
acceptance: "a check that nothing in a field can carry evidence text ... perhaps
with an allowlist of field names"). Adding a field here is a deliberate, reviewed
decision that the new name can only ever hold a type name, a count or an id —
never a native key, a record body or anything a provider said."""

_MAX_FIELD_LENGTH: Final = 100
"""Longer than any id, count or exception class name this tree names
(``EvidenceWorkspaceUnresolved`` and its siblings are well under this), short
enough that a stray sentence of evidence text cannot pass as a token."""


class ContentFreeExtraError(ValueError):
    """Raised by :func:`content_free_extra` when a field name or value looks like it
    could carry evidence text rather than a short diagnostic token."""


def content_free_extra(fields: Mapping[str, object]) -> dict[str, object]:
    """Validate one content-free log line's ``extra`` before it is logged.

    Every key must be in :data:`ALLOWED_EXTRA_FIELDS`, and every string value must be
    short. Both are structural, not content checks: they cannot prove a field holds no
    evidence text, but they stop the obvious ways one would arrive — a new field
    added without registering it here first, or a value that is a rendered object, a
    message or a body rather than a type name, a count or an id.
    """
    for key, value in fields.items():
        if key not in ALLOWED_EXTRA_FIELDS:
            raise ContentFreeExtraError(
                f"{key!r} is not in ALLOWED_EXTRA_FIELDS; a content-free log line "
                "may only carry a reviewed, registered field name"
            )
        if isinstance(value, str) and len(value) > _MAX_FIELD_LENGTH:
            raise ContentFreeExtraError(
                f"{key!r} is {len(value)} characters, longer than a type name, a "
                "count or an id should ever be"
            )
    return dict(fields)
