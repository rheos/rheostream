"""The owned-delete declaration, the participant table, and the one registry of both.

``core`` cannot declare anything through a ``ModuleManifest`` — ``core`` is the
reserved module segment (``rheo_contracts.refs.is_reserved_module``) — so the deletion
coordinator lives in core code and reaches a module's own delete through a declaration
the module registers here. **Core never imports a module's code**; it calls two
callables the module handed it.

**Two shapes, and they are not the same thing.**

- An :class:`OwnedDeletion` is the *owner's* declaration for one ``(module_id,
  record_type)`` pair: how to authorise deleting one of its records, and how to delete
  it. Exactly one declaration may own a record type, mirroring ``modules/manifest.py``
  's "a record type appears in exactly one manifest".
- A :class:`~rheo_core.modules.manifest.DeletionParticipant` is somebody *else's* hook
  on a type they do not own — ``recallatron.on_record_deleted`` on a Leads observation,
  say. Many modules may participate in one type, and each runs only where its own
  module is enabled in the workspace.

**Every callable here is content-free at its boundary.** ``authorize_delete`` answers
a reference and a revision or a refusal; it never returns the record and is never
required to have one that is current, so an authorised expired or superseded row can
still be erased (``docs/architecture/deletion-export-migration.md`` § Record-level
deletion). ``delete_owned`` and a participant's handler answer :class:`RemovedMemories`
— identifiers of memory rows they physically removed, and nothing else.

**The registry is process-global, like ``RESOLVERS`` and ``TOOL_REGISTRY``.** The
coordinator is a core operation handler, which receives ``(ctx, uow, input)`` and has
no channel to be handed a registry; a composition-root-local instance (the shape
``JobKindRegistry`` and ``ConsumerRegistry`` take) would therefore be unreachable from
the one caller that matters. ``load_modules(deletions=...)`` still takes an explicit
instance and defaults to ``None``, so a process that passes none registers no
module-owned types at all — see that function's own docstring.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID

from rheo_contracts import RecordRef, WorkspaceContext, is_reserved_module

from rheo_core.boundary.context import Refusal
from rheo_core.operations.refusals import RegistrationRefused
from rheo_core.operations.registry import check_origin
from rheo_core.storage.backend import UnitOfWork

RECORD_NOT_DELETABLE: Final = "record_not_deletable"
"""The one refusal a caller gets for a reference core will not delete.

**Deliberately generic, and the same state and the same sentence for three different
situations**: no declaration owns the reference's ``(module, record_type)`` pair, the
owning module is not enabled in this workspace, and the reference names a core-owned
record (which no module declaration may claim). Distinguishing them would turn
``core.record.delete`` into a probe for which record types a deployment has and which
modules a workspace runs, answerable by anyone who may call the operation at all. The
owner's *own* authorizer refusal is passed through unchanged, because that one is the
owner's to phrase for its own records.
"""


@dataclass(frozen=True, slots=True)
class DeleteAuthorization:
    """What an owner's ``authorize_delete`` answers with: a reference and a revision.

    Content-free by shape: there is nowhere on this object to put a title, a body or
    a payload, so an authorizer cannot leak one through the value it returns. The
    revision is what the coordinator captures before the effect and rechecks under the
    lifecycle lock, so an approver cannot widen the scope of what was approved by
    letting the record move underneath it.
    """

    ref: RecordRef
    revision: int


@dataclass(frozen=True, slots=True)
class RemovedMemories:
    """The memory rows one participant, or the owner itself, physically removed.

    Identifiers only — the coordinator counts them and stores the count, and no
    caller ever receives them (``deletion-export-migration.md``: every caller,
    including an owner, receives only ``{deletion_ref}``).

    **What belongs in here is narrow and is the ledger's whole meaning**: distinct
    memory rows that were physically removed, including the target itself when the
    target is a memory. Never a child row, never a vector, never a row that was
    already absent, never a retained replacement, and never a row merely marked
    invalidated. A union of these across the owner and every participant is what the
    ledger's ``invalidated_memory_count`` counts, so overlapping reports collapse
    rather than double-counting.
    """

    memory_ids: frozenset[UUID] = frozenset()

    def union(self, other: "RemovedMemories") -> "RemovedMemories":
        return RemovedMemories(self.memory_ids | other.memory_ids)


NOTHING_REMOVED: Final = RemovedMemories()
"""What an owner or participant answers when it removed no memory row at all.

The common case in release one: only Recallatron owns memory, so every other owner's
delete and every participant on a non-memory type answers this.
"""


class DeleteAuthorizer(Protocol):
    """``(ctx, uow, ref) -> DeleteAuthorization | Refusal``, content-free."""

    def __call__(
        self, ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
    ) -> DeleteAuthorization | Refusal: ...


class OwnedDeleter(Protocol):
    """``(ctx, uow, authorization) -> RemovedMemories``: remove the record it owns.

    It takes the authorization rather than the bare reference so that the revision the
    coordinator authorised is the revision the owner deletes at, with no second read of
    a value the two could disagree about.
    """

    def __call__(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        authorization: DeleteAuthorization,
    ) -> RemovedMemories: ...


class DeletionHandler(Protocol):
    """``(ctx, uow, ref) -> RemovedMemories``: one participant's hook.

    Narrowed from ``Callable[..., object]`` in this run, which is the first one that
    calls it. A participant that raises aborts the whole deletion — the record survives
    untouched and the operation reports the participant's error — so a handler has no
    refusal channel of its own and does not need one.
    """

    def __call__(
        self, ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
    ) -> RemovedMemories: ...


@dataclass(frozen=True, slots=True)
class OwnedDeletion:
    """One module's declaration that it owns deleting one of its record types."""

    module_id: str
    record_type: str
    authorize_delete: DeleteAuthorizer
    delete_owned: OwnedDeleter

    @property
    def qualified_type(self) -> str:
        return f"{self.module_id}.{self.record_type}"


@dataclass(frozen=True, slots=True)
class RegisteredParticipant:
    """One participant hook with the module id the enabled-module gate reads.

    ``module_id`` is the *declaring* module, not the owner of the type: that is the
    whole point of a participant, and it is what decides whether the hook runs in a
    given workspace.
    """

    module_id: str
    qualified_type: str
    handler: DeletionHandler


class OwnedDeletionRegistry:
    """Owned-delete declarations by ``(module_id, record_type)``, plus participants."""

    def __init__(self) -> None:
        self._owners: dict[tuple[str, str], OwnedDeletion] = {}
        self._participants: list[RegisteredParticipant] = []

    def register(self, declaration: OwnedDeletion, *, origin: str) -> None:
        """Record ``declaration`` as the one owner of its record type.

        ``check_origin`` is the operation registry's own gate, applied here for the
        same reason ``ResolverRegistry.register`` applies it: a module may only claim
        record types under its own id, ``core`` and ``harness`` are reserved to their
        origins, and ``origin = "test_harness"`` is accepted under ``profile = test``
        only. Re-registering the identical declaration is a no-op, so a process that
        loads its modules twice — ``apps/core``'s lifespan does, under test — registers
        once.
        """
        key = declaration.qualified_type
        if is_reserved_module(declaration.module_id):
            raise RegistrationRefused(
                key, "core owns the coordinator, not an owned-delete declaration"
            )
        check_origin(origin, declaration.module_id, name=key)
        for label, callable_ in (
            ("authorize_delete", declaration.authorize_delete),
            ("delete_owned", declaration.delete_owned),
        ):
            if not callable(callable_):
                raise TypeError(f"an owned-delete declaration's {label} is callable")
        existing = self._owners.get((declaration.module_id, declaration.record_type))
        if existing is not None:
            if existing == declaration:
                return
            raise RegistrationRefused(
                key, "a record type is owned by exactly one deletion declaration"
            )
        self._owners[(declaration.module_id, declaration.record_type)] = declaration

    def register_participant(
        self, module_id: str, record_types: Iterable[str], handler: DeletionHandler
    ) -> None:
        """Record one participant hook over ``record_types``.

        ``record_types`` are the **qualified** ``<module>.<record_type>`` names a
        participant hooks, because a participant hooks types it does not own and a
        bare second segment would name a type in no particular module.

        Last-writer-wins is not offered and neither is a refusal for a repeat: an
        identical registration is skipped and a second, different handler from the same
        module over the same type is appended, because two hooks from one module over
        one type is a module's own business and the coordinator runs both. What cannot
        happen is one module's hook silently replacing another's.
        """
        if not callable(handler):
            raise TypeError("a deletion participant's handler is callable")
        for qualified_type in record_types:
            entry = RegisteredParticipant(module_id, qualified_type, handler)
            if entry not in self._participants:
                self._participants.append(entry)

    def owner_of(self, ref: RecordRef) -> OwnedDeletion | None:
        """The declaration that owns ``ref``'s record type, or ``None``."""
        return self._owners.get((ref.module, ref.record_type))

    def participants_for(
        self, ref: RecordRef, *, enabled_modules: frozenset[str]
    ) -> tuple[RegisteredParticipant, ...]:
        """Every participant on ``ref``'s type whose own module is enabled here.

        Registration order, so a deployment's cascade is the same on every run. The
        core segment is always enabled, exactly as it is in the event fan-out and in
        the resolver, so a core-declared participant needs no ``module_state`` row.
        """
        qualified_type = f"{ref.module}.{ref.record_type}"
        return tuple(
            participant
            for participant in self._participants
            if participant.qualified_type == qualified_type
            and (
                is_reserved_module(participant.module_id)
                or participant.module_id in enabled_modules
            )
        )


OWNED_DELETIONS: Final = OwnedDeletionRegistry()
"""The process-wide table the coordinator resolves against; see the module docstring."""


def authorize_owned_delete(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    ref: RecordRef,
    *,
    registry: OwnedDeletionRegistry = OWNED_DELETIONS,
) -> tuple[OwnedDeletion, DeleteAuthorization] | Refusal:
    """The owned-delete authorization for ``ref``: the declaration and its answer.

    The one function the coordinator runs at all three of its checkpoints — before an
    approval is minted, again at approval execution under the reconstructed original
    caller, and a third time under the lifecycle lock immediately before the physical
    delete. One function rather than three call sites with their own conditions, so
    the three checks cannot come to differ.

    A reference core will not delete gets :data:`RECORD_NOT_DELETABLE` with one fixed
    sentence, whatever the reason; the owner's own refusal is returned unchanged.
    """
    # No core branch, unlike the participant gate: registration refuses a reserved
    # module id outright, so an owner is always a real module and always has to be
    # enabled in this workspace.
    owner = registry.owner_of(ref)
    if owner is None or owner.module_id not in ctx.enabled_modules:
        return Refusal(
            RECORD_NOT_DELETABLE,
            f"{ref.format()} is not a record this workspace can delete",
        )
    authorization = owner.authorize_delete(ctx, uow, ref)
    if isinstance(authorization, Refusal):
        return authorization
    return owner, authorization
