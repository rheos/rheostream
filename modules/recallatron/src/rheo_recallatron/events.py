"""Recallatron's two ratified manifest events, and the one way this module publishes.

``recallatron.memory.recorded`` and ``recallatron.memory.invalidated``, schema version
1, carrying ``memory_ref``/``kind``/``reason`` (``intake-and-events.md``'s release-one
event table, § A5). Both are declared together because the manifest names them
together: a run that declared only the one it publishes would leave the other's shape
for a later run to invent, and a consumer cannot be written against a declaration that
does not exist yet. ``.recorded`` is published by this run's write handlers and by
trusted acceptance; ``.invalidated`` is published by the lifecycle closure, which is a
later prompt's — the declaration is what this run owes it.

``subscriptions`` stays empty. No consumer exists in 1a1, and a publish that reaches
none still writes its outbox row and completes with zero deliveries: the outbox is the
record of what happened, and what happened does not depend on who was listening.

**Every publish happens inside the caller's own transaction**, beside the row insert
and the dispatcher's success audit row, so a rolled-back write leaves no outbox row and
no delivery. There is no second publish path in this module and no post-commit hook: an
event written after the commit would be an event for a mutation that might not have
landed.
"""

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from rheo_contracts import WorkspaceContext
from rheo_core.events import NewEvent, publish
from rheo_core.events.consumers import ConsumerRegistry, HandlerUnitOfWork
from rheo_core.modules.manifest import EventDeclaration
from rheo_core.operations import CONSUMERS_MISSING
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork

from rheo_recallatron.configuration import MEMORY_RECORD_TYPE, MODULE_ID

MEMORY_RECORDED: Final = f"{MODULE_ID}.{MEMORY_RECORD_TYPE}.recorded"
MEMORY_INVALIDATED: Final = f"{MODULE_ID}.{MEMORY_RECORD_TYPE}.invalidated"

EVENT_SCHEMA_VERSION: Final = 1


class MemoryEvent(BaseModel):
    """The three ratified fields both events carry.

    ``reason`` is null on ``.recorded`` — nothing was invalidated — and on
    ``.invalidated`` it is the row's own ``invalidation_reason``. One model for both
    rather than two that differ by a nullable field, because the ratified table gives
    them one field list and a second model would be a second place to keep it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_ref: str
    kind: str
    reason: str | None


RECORDED_DECLARATION: Final = EventDeclaration(
    type=MEMORY_RECORDED, schema_version=EVENT_SCHEMA_VERSION, data=MemoryEvent
)

INVALIDATED_DECLARATION: Final = EventDeclaration(
    type=MEMORY_INVALIDATED, schema_version=EVENT_SCHEMA_VERSION, data=MemoryEvent
)

EVENTS: Final[tuple[EventDeclaration, ...]] = (
    RECORDED_DECLARATION,
    INVALIDATED_DECLARATION,
)


def consumers_for_dispatch(uow: UnitOfWork) -> ConsumerRegistry:
    """The registry this dispatch was wired with, or a refusal.

    The guard is the one ``dispatch()``'s own docstring prescribes, verbatim: a
    publishing handler reads the registry off the sealed view it was handed and
    refuses when there is none. The alternative — building a throwaway
    ``ConsumerRegistry()`` per call — is ruled out, because a publish's fan-out would
    then depend on which call built the registry, which is a silent wrong answer
    rather than a refusal a deployment can see.
    """
    consumers = uow.consumers if isinstance(uow, HandlerUnitOfWork) else None
    if consumers is None:
        raise OperationRefused(
            CONSUMERS_MISSING, "no ConsumerRegistry is wired for this dispatch"
        )
    return consumers


def publish_memory_event(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    *,
    event_type: str,
    memory_ref: str,
    kind: str,
    reason: str | None,
    revision: int,
    consumers: ConsumerRegistry,
) -> UUID:
    """Write one outbox row for this memory, in the caller's transaction.

    ``subject_ref`` is the memory's canonical reference and ``subject_revision`` the
    row's revision at the moment of the write — the initial revision for a
    ``.recorded``. § A5 words the subject as "``memory_id``"; the column is the same
    ``subject_ref`` every other event in the tree fills with a canonical reference, and
    a bare uuid there would be the one subject a reader could not resolve.

    ``now`` is read here rather than threaded through the handler, which is the
    convention ``dispatch()``'s own three internal clock reads already follow: nothing
    in this codebase carries a clock on the unit of work.
    """
    return publish(
        ctx,
        uow,
        NewEvent(
            type=event_type,
            schema_version=EVENT_SCHEMA_VERSION,
            subject_ref=memory_ref,
            subject_revision=revision,
            data=MemoryEvent(
                memory_ref=memory_ref, kind=kind, reason=reason
            ).model_dump(),
        ),
        now=datetime.now(UTC),
        consumers=consumers,
    )
