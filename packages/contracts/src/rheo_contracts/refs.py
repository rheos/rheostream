"""The typed record reference: ``<module>.<record_type>:<uuid>``.

The string form the core, the modules, the MCP facade, events, audit records and exports
all use to name a record without owning it (``docs/architecture/identifiers.md``
§ Record references). A reference is a value, never a join.

Two things deliberately absent: there is **no resolver** here (that is C4's
``rheo_core/refs/resolver.py``, which needs permission and storage), and there is **no
manifest lookup** — validating a record type against the owning module's manifest is
phase 2. This module answers only "is this a well-formed reference, and what are its
parts".
"""

import re
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

_SEGMENT_PATTERN = r"[a-z][a-z0-9_]*"
_REFERENCE = re.compile(
    rf"^(?P<module>{_SEGMENT_PATTERN})\.(?P<record_type>{_SEGMENT_PATTERN})"
    r":(?P<record_id>[^:]+)$"
)

Segment = Annotated[str, StringConstraints(pattern=rf"^{_SEGMENT_PATTERN}$")]

SHORT_ID_LENGTH = 8
"""How many hex characters a short ID starts at: the UUID's random tail (#345).

A UUIDv7 begins with a millisecond timestamp, so records made in the same minute share
their head; the tail is random. Display lengthens it, like a git short hash, when two
records of a type share it.
"""
_TAIL = re.compile(rf"[0-9a-f]{{{SHORT_ID_LENGTH},32}}")


def _clean_tail(value: str) -> str | None:
    candidate = value.strip().lower().replace("-", "")
    return candidate if _TAIL.fullmatch(candidate) else None


RESERVED_MODULE_SEGMENT = "core"
"""The module segment reserved for core-owned records (operations, approvals).

Not a parse-time refusal: the core itself mints ``core.*`` references. Callers that must
reject a module's claim on a core record use :func:`is_reserved_module`.
"""


def is_reserved_module(module: str) -> bool:
    """True when ``module`` is the segment reserved for core-owned records."""
    return module == RESERVED_MODULE_SEGMENT


class RecordRefMalformed(ValueError):
    """A string that is not a well-formed record reference.

    Carries the offending value and nothing else: no parser internals, no partial parse,
    nothing that would widen what a caller can put in a log line.
    """

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


class RecordRef(BaseModel):
    """A reference to a record owned by ``module``, of type ``record_type``."""

    model_config = ConfigDict(frozen=True)

    module: Segment
    record_type: Segment
    id: UUID

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse the string form, or raise :class:`RecordRefMalformed`."""
        match = _REFERENCE.fullmatch(value)
        if match is None:
            raise RecordRefMalformed(value)
        raw_id = match["record_id"]
        try:
            record_id = UUID(raw_id)
        except ValueError as exc:
            raise RecordRefMalformed(value) from exc
        # Reject the non-canonical spellings ``UUID`` accepts (braced, urn:, undashed,
        # uppercase) so ``parse`` and ``format`` round-trip a string exactly.
        if str(record_id) != raw_id:
            raise RecordRefMalformed(value)
        return cls(
            module=match["module"], record_type=match["record_type"], id=record_id
        )

    @staticmethod
    def short(value: str) -> tuple[str, str, str] | None:
        """``(module, record_type, tail)`` for ``<module>.<record_type>:<tail>``.

        The typed short form (#345). A tail is 8 to 32 hex characters, read without
        dashes or case. This only reads the string: finding the one record a tail names
        needs the workspace's storage (``rheo_core.refs.short``).
        """
        match = _REFERENCE.fullmatch(value.strip())
        if match is None:
            return None
        tail = _clean_tail(match["record_id"])
        return None if tail is None else (match["module"], match["record_type"], tail)

    @classmethod
    def tail(cls, value: str, module: str, record_type: str) -> str | None:
        """The tail ``value`` names for ``<module>.<record_type>``, or ``None``.

        Accepts the typed short form and, since the type is known, a bare tail.
        """
        short = cls.short(value)
        if short is not None:
            return short[2] if short[:2] == (module, record_type) else None
        return _clean_tail(value)

    @staticmethod
    def short_id(identifier: UUID, length: int = SHORT_ID_LENGTH) -> str:
        """The last ``length`` hex characters of ``identifier``."""
        return identifier.hex[-length:]

    def format(self) -> str:
        """The canonical string form, round-tripping through :meth:`parse`."""
        return f"{self.module}.{self.record_type}:{self.id}"

    def __str__(self) -> str:
        return self.format()
