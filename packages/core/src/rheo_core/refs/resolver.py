"""``resolve(ref, ctx) -> RecordHead | Unavailable``: resolution of a typed record
reference under the caller's permission (``docs/architecture/identifiers.md``
§ Resolution under permission), and the registration of per-record-type resolvers.

The resolver parses the reference, finds the owning module's registered resolver for
the record type (``Unavailable`` when there is none, or when the module is not in
``ctx.enabled_modules``; ``core`` is always enabled), and calls it with a
``UnitOfWork`` opened through ``route(ctx)``. A cross-workspace reference resolves to
``Unavailable`` because the lookup runs against the caller's own database and the row
is not there: that is criterion 7's seam, and 0b1 claims nothing of criterion 7 (the
HTTP and MCP halves are C8's). This run ships no production record type; the test
harness registers ``harness.note``.

Imported by module path (``rheo_core.refs.resolver``), never re-exported from
``rheo_core.refs``: that package's ``__init__`` is imported by the storage layer for
``uuid7``, and this module imports the storage layer, so a re-export would close an
import cycle.

**``UnitOfWork`` is re-exported from here, explicitly, and that is a contract decision
rather than a convenience.** ``RecordResolver`` below is the interface a domain module
implements, and its signature names three types: ``WorkspaceContext``, ``RecordRef``
and ``UnitOfWork``. A module distribution may not import the core's storage package —
``tests/postgres/test_module_storage_ownership.py`` scans ``modules/`` for exactly that
and refuses it, because a module reaching into core storage is how a module ends up
holding another module's table handle. Without this line a module implementing the
contract would have no way to *name* the transaction the contract hands it. So the
module that declares the interface publishes the names in it, the same way
``operations/refusals.py`` re-exports ``HANDLER_MAY_NOT_COMMIT`` for the callers that
meet it as a dispatch state. Nothing else from the storage package is published here,
and the scan that forbids the direct import stays exactly as strict as it was.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from rheo_contracts import (
    RecordRef,
    RecordRefMalformed,
    WorkspaceContext,
    is_reserved_module,
)

from rheo_core.boundary.context import CONTEXT_REQUIRED
from rheo_core.operations.refusals import MODULE_DISABLED, RegistrationRefused
from rheo_core.operations.registry import check_origin
from rheo_core.storage.backend import StorageRefusal
from rheo_core.storage.backend import UnitOfWork as UnitOfWork  # see the docstring
from rheo_core.storage.routing import open_unit_of_work

LIVE: Final = "live"
DELETED: Final = "deleted"
UNAVAILABLE: Final = "unavailable"
RECORD_STATES: Final = frozenset({LIVE, DELETED, UNAVAILABLE})

REFERENCE_MALFORMED: Final = "reference_malformed"
UNRESOLVABLE: Final = "unresolvable"
NOT_FOUND: Final = "not_found"

_SEGMENT: Final = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True, slots=True)
class RecordHead:
    """The ratified resolution result (``identifiers.md`` § Resolution under
    permission): the reference, a showable label, whether ``ctx`` may read the full
    record, the record's state, and its revision when ``live``."""

    ref: RecordRef
    display: str
    readable: bool
    state: str
    revision: int | None


@dataclass(frozen=True, slots=True)
class Unavailable:
    """The reference could not be resolved for this caller. ``reason`` names why at
    the type level (``reference_malformed``, ``unresolvable``, ``module_disabled``,
    a storage refusal state) or ``not_found`` from the module's resolver — which is
    what a cross-workspace reference and a reference that never existed both get,
    indistinguishably."""

    reference: str
    reason: str


RecordResolver = Callable[
    [WorkspaceContext, UnitOfWork, RecordRef], RecordHead | Unavailable
]
"""What a module registers per owned record type; it runs its own permission check."""


class ResolverRegistry:
    """The record-type → resolver table. One instance is exported as ``RESOLVERS``."""

    def __init__(self) -> None:
        self._resolvers: dict[tuple[str, str], RecordResolver] = {}

    def register(
        self,
        module_id: str,
        record_type: str,
        resolver: RecordResolver,
        *,
        origin: str,
    ) -> None:
        """Bind ``resolver`` to ``<module_id>.<record_type>``.

        The module id must be the registering origin's, ``core`` and ``harness``
        are reserved for their origins, and ``origin = "test_harness"`` is accepted
        under ``profile = test`` only — the operation registry's ``check_origin``,
        applied here too. Re-registering the same resolver is a no-op.
        """
        key = f"{module_id}.{record_type}"
        if not (_SEGMENT.fullmatch(module_id) and _SEGMENT.fullmatch(record_type)):
            raise RegistrationRefused(key, "record types are <module_id>.<type>")
        check_origin(origin, module_id, name=key)
        if not callable(resolver):
            raise TypeError("a record resolver must be callable")
        existing = self._resolvers.get((module_id, record_type))
        if existing is not None:
            if existing is resolver:
                return
            raise RegistrationRefused(key, "a resolver is already registered")
        self._resolvers[(module_id, record_type)] = resolver

    def lookup(self, module_id: str, record_type: str) -> RecordResolver | None:
        return self._resolvers.get((module_id, record_type))

    def record_types(self) -> frozenset[str]:
        return frozenset(f"{m}.{t}" for m, t in self._resolvers)


RESOLVERS: Final = ResolverRegistry()


def register_resolver(
    module_id: str,
    record_type: str,
    resolver: RecordResolver,
    *,
    origin: str,
    registry: ResolverRegistry = RESOLVERS,
) -> None:
    """Register on the process-wide table; see ``ResolverRegistry.register``."""
    registry.register(module_id, record_type, resolver, origin=origin)


def resolve_in(
    ref: RecordRef | str,
    ctx: object,
    uow: UnitOfWork,
    *,
    registry: ResolverRegistry = RESOLVERS,
) -> RecordHead | Unavailable:
    """Resolve inside a unit of work the caller already holds (a handler running
    under ``dispatch`` uses this so one dispatch is one transaction)."""
    if not isinstance(ctx, WorkspaceContext):
        return Unavailable(str(ref), CONTEXT_REQUIRED)
    if isinstance(ref, RecordRef):
        parsed = ref
    else:
        try:
            parsed = RecordRef.parse(ref)
        except RecordRefMalformed:
            return Unavailable(str(ref), REFERENCE_MALFORMED)
        except TypeError:
            return Unavailable(str(ref), REFERENCE_MALFORMED)
    reference = parsed.format()
    resolver = registry.lookup(parsed.module, parsed.record_type)
    if resolver is None:
        return Unavailable(reference, UNRESOLVABLE)
    if not is_reserved_module(parsed.module) and (
        parsed.module not in ctx.enabled_modules
    ):
        return Unavailable(reference, MODULE_DISABLED)
    return resolver(ctx, uow, parsed)


def resolve(
    ref: RecordRef | str, ctx: object, *, registry: ResolverRegistry = RESOLVERS
) -> RecordHead | Unavailable:
    """The ratified entry point: one unit of work through ``route(ctx)``."""
    if not isinstance(ctx, WorkspaceContext):
        return Unavailable(str(ref), CONTEXT_REQUIRED)
    try:
        uow = open_unit_of_work(ctx)
    except StorageRefusal as refusal:
        return Unavailable(str(ref), refusal.state)
    with uow:
        return resolve_in(ref, ctx, uow, registry=registry)
