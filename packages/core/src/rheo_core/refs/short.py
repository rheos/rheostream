"""Short IDs (#345): a record's UUID tail, shown to people and accepted back.

A UUIDv7 stays the only identity. People see the last 8 hex characters of it (the head
is a millisecond timestamp, so records made in the same minute share it; the tail is
random). Like a git short hash, display lengthens a tail that two records of one type
share, and every place that takes a reference also takes ``<module>.<type>:<tail>``, or
a bare tail where the type is known, resolving it by a unique tail match within that
type. An ambiguous tail is refused, never guessed.

A module opts a record type in by registering a :data:`TailLookup`. Input validation
reaches storage through pydantic's validation context: ``dispatch``, the tool façade
and in-transaction calls pass :func:`validation_context`, and a reference field's
validator calls :func:`ref_value`. Without that context a short form is refused, so a
model validated anywhere else accepts only full references, exactly as before.

Imported by module path, never re-exported from ``rheo_core.refs`` (the same import
cycle ``resolver.py`` documents).
"""

from collections.abc import Callable, Iterable, Sequence
from typing import Any, Final
from uuid import UUID

from pydantic import ValidationInfo
from rheo_contracts import (
    SHORT_ID_LENGTH,
    RecordRef,
    RecordRefMalformed,
    WorkspaceContext,
)
from sqlalchemy.exc import SQLAlchemyError

from rheo_core.operations.refusals import OperationRefused, RegistrationRefused
from rheo_core.operations.registry import check_origin
from rheo_core.storage.backend import StorageRefusal, UnitOfWork
from rheo_core.storage.routing import open_unit_of_work

TailLookup = Callable[[UnitOfWork, int, Sequence[str]], Sequence[UUID]]
"""The ids of one record type whose last ``length`` hex characters are in ``tails``."""

SHORT_REFS: Final = "rheo.short_refs"
"""The validation-context key :class:`ShortRefExpander` travels under."""

AMBIGUOUS: Final = "this short ID matches more than one record; give more characters"
NOT_FOUND: Final = "no record has this short ID"
FULL_ONLY: Final = "this record type takes full references only"
UNAVAILABLE: Final = "short IDs cannot be looked up now; use the full reference"


class TailLookups:
    """The record-type → tail lookup table. One instance is exported as ``TAILS``."""

    def __init__(self) -> None:
        self._lookups: dict[tuple[str, str], TailLookup] = {}

    def register(
        self, module_id: str, record_type: str, lookup: TailLookup, *, origin: str
    ) -> None:
        key = f"{module_id}.{record_type}"
        check_origin(origin, module_id, name=key)
        existing = self._lookups.get((module_id, record_type))
        if existing is not None and existing is not lookup:
            raise RegistrationRefused(key, "a tail lookup is already registered")
        self._lookups[(module_id, record_type)] = lookup

    def lookup(self, module_id: str, record_type: str) -> TailLookup | None:
        return self._lookups.get((module_id, record_type))


TAILS: Final = TailLookups()


def register_tail_lookup(
    module_id: str,
    record_type: str,
    lookup: TailLookup,
    *,
    origin: str,
    registry: TailLookups = TAILS,
) -> None:
    """Give ``<module_id>.<record_type>`` short IDs; see ``TailLookups.register``."""
    registry.register(module_id, record_type, lookup, origin=origin)


def expand_in(
    uow: UnitOfWork,
    module_id: str,
    record_type: str,
    tail: str,
    *,
    registry: TailLookups = TAILS,
) -> RecordRef:
    """The one record of the type whose UUID ends in ``tail``, or ``ValueError``."""
    lookup = registry.lookup(module_id, record_type)
    if lookup is None:
        raise ValueError(FULL_ONLY)
    found = set(lookup(uow, len(tail), [tail]))
    if not found:
        raise ValueError(NOT_FOUND)
    if len(found) > 1:
        raise ValueError(AMBIGUOUS)
    return RecordRef(module=module_id, record_type=record_type, id=found.pop())


def shortest_tail(identifier: UUID, among: Iterable[UUID]) -> str:
    """``identifier``'s shortest tail no other id in ``among`` ends with: 8, 10, 12…"""
    own = identifier.hex
    rivals = [
        other.hex
        for other in among
        if other != identifier
        and other.hex[-SHORT_ID_LENGTH:] == own[-SHORT_ID_LENGTH:]
    ]
    length = SHORT_ID_LENGTH
    while length < len(own) and any(r.endswith(own[-length:]) for r in rivals):
        length += 2
    return own[-length:]


def short_ids(
    uow: UnitOfWork, lookup: TailLookup, ids: Iterable[UUID]
) -> dict[UUID, str]:
    """Each id's display short ID, lengthened where its type has a shared tail.

    ``lookup`` is the type's own :data:`TailLookup`. One call over the 8-character
    tails finds every record that could collide: two ids that share a longer tail share
    the 8-character one too.
    """
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return {}
    tails = sorted({RecordRef.short_id(i) for i in wanted})
    among = set(lookup(uow, SHORT_ID_LENGTH, tails)) | set(wanted)
    return {i: shortest_tail(i, among) for i in wanted}


class ShortRefExpander:
    """Resolves short references while an operation's input is validated.

    ``dispatch`` validates before it opens the work transaction, so without ``uow`` the
    expander opens its own short read; inside a transaction it reuses that one. It is
    only consulted for a value that is not a full reference.
    """

    def __init__(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork | None = None,
        *,
        registry: TailLookups = TAILS,
    ) -> None:
        self._ctx = ctx
        self._uow = uow
        self._registry = registry

    def expand(self, module_id: str, record_type: str, tail: str) -> RecordRef:
        # Checked first, so a type without short IDs opens no connection.
        if self._registry.lookup(module_id, record_type) is None:
            raise ValueError(FULL_ONLY)
        # Inside a caller's transaction a storage failure propagates, as every other
        # failure there does. On the short read of our own, validation was pure before
        # short IDs, so it must still answer ``input_invalid`` (pydantic wraps only
        # ``ValueError``) rather than raise out of ``dispatch``.
        if self._uow is not None:
            return self._expand(self._uow, module_id, record_type, tail)
        try:
            with open_unit_of_work(self._ctx) as uow:
                return self._expand(uow, module_id, record_type, tail)
        except (StorageRefusal, SQLAlchemyError):
            raise ValueError(UNAVAILABLE) from None

    def _expand(
        self, uow: UnitOfWork, module_id: str, record_type: str, tail: str
    ) -> RecordRef:
        from rheo_core.modules.readiness import require_ready

        try:
            require_ready(uow.connection, module_id)
        except OperationRefused:
            raise ValueError(UNAVAILABLE) from None
        return expand_in(uow, module_id, record_type, tail, registry=self._registry)


def validation_context(
    ctx: object, uow: UnitOfWork | None = None
) -> dict[str, object] | None:
    """The pydantic context that lets reference fields accept short IDs."""
    if not isinstance(ctx, WorkspaceContext):
        return None
    return {SHORT_REFS: ShortRefExpander(ctx, uow)}


def ref_value(
    value: Any, info: ValidationInfo, module_id: str, record_type: str
) -> Any:
    """A reference field's before-validator: a full reference parses, a short one
    (typed, or bare since the field knows its type) expands through the context.

    Anything else is returned unchanged, for the field's own check to refuse.
    """
    if not isinstance(value, str):
        return value
    try:
        return RecordRef.parse(value)
    except RecordRefMalformed:
        pass
    tail = RecordRef.tail(value, module_id, record_type)
    context = info.context if isinstance(info.context, dict) else {}
    expander = context.get(SHORT_REFS)
    if tail is None or not isinstance(expander, ShortRefExpander):
        raise ValueError("invalid reference")
    return expander.expand(module_id, record_type, tail)


def full_ref_text(value: Any, info: ValidationInfo) -> Any:
    """A free-form reference string, with a typed short form expanded to the full one.

    For fields that take references of any type as text (a runtime run's ``refs``),
    so what is stored and later resolved is the full reference, fixed at the moment
    the caller named it. Anything that is not a typed short form for a type with
    short IDs is returned unchanged.
    """
    if not isinstance(value, str):
        return value
    try:
        RecordRef.parse(value)
        return value
    except RecordRefMalformed:
        pass
    short = RecordRef.short(value)
    context = info.context if isinstance(info.context, dict) else {}
    expander = context.get(SHORT_REFS)
    if short is None or not isinstance(expander, ShortRefExpander):
        return value
    if TAILS.lookup(short[0], short[1]) is None:
        return value
    return expander.expand(*short).format()
