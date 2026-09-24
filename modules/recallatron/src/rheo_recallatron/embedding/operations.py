"""``recallatron.embedding.rebuild``: the owner's one way to (re)fill the dense index;
and ``recallatron.embedding.coverage``, the owner's read of how full it is.

``MUTATE``, ``long_running``, owner only. The dispatch mints an operation record, this
handler enqueues exactly one job carrying that record's id, and the dispatch returns
``pending``; the job (``rebuild.py``) prunes, walks and reports progress, and the worker
closes the record when the job ends.

It is the only backfill path. Turning a provider on embeds nothing that already exists:
an owner runs this after enabling a provider, after changing it, and after a restore
(whose export deliberately carries no vectors).

**Refused, not guessed, when no provider resolves.** With nothing configured there is
no model to prune against or fill for.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings.schema import SettingTypeMismatch, decode_text
from rheo_core.settings.storage_source import TransactionBoundOverrideSource

from rheo_recallatron.configuration import (
    EMBEDDING_BATCH_SIZE_DEFAULT,
    EMBEDDING_BATCH_SIZE_KEY,
    EMBEDDING_BATCH_SIZE_SPEC,
    MODULE_ID,
)
from rheo_recallatron.contracts import Strict
from rheo_recallatron.embedding.enqueue import enqueue_rebuild_job
from rheo_recallatron.embedding.registry import resolve_provider
from rheo_recallatron.refusals import EMBEDDING_PROVIDER_UNAVAILABLE
from rheo_recallatron.storage.repository import embedding_coverage
from rheo_recallatron.writes import opened

EMBEDDING_REBUILD: Final = f"{MODULE_ID}.embedding.rebuild"
EMBEDDING_COVERAGE: Final = f"{MODULE_ID}.embedding.coverage"


class EmbeddingRebuildInput(Strict):
    """No fields. What to rebuild against is the deployment's configured provider, and
    how many to embed at a time is the workspace's own setting."""


class EmbeddingRebuildScheduled(Strict):
    """The record the rebuild's progress lands on, in the shape a scheduled export
    answers with."""

    operation_id: UUID | None


class RebuildOutsideDispatch(RuntimeError):
    """The handler ran with no operation record to report on.

    ``long_running`` dispatch always mints one before the handler runs, so this is a
    caller driving the handler some other way. Nothing would ever close the job's
    record or read its progress, so nothing is enqueued.
    """


def batch_size_in(stored_rows: Mapping[str, str]) -> int:
    """The workspace's rebuild batch size, from override rows already read.

    An absent row, one that will not decode and one outside the declared range all read
    as the package default: a read degrades, it never raises on a bad stored value.
    """
    stored = stored_rows.get(EMBEDDING_BATCH_SIZE_KEY)
    if stored is None:
        return EMBEDDING_BATCH_SIZE_DEFAULT
    try:
        value = decode_text(
            EMBEDDING_BATCH_SIZE_SPEC, stored, source="the stored workspace override"
        )
    except SettingTypeMismatch:
        return EMBEDDING_BATCH_SIZE_DEFAULT
    return value if isinstance(value, int) else EMBEDDING_BATCH_SIZE_DEFAULT


def rebuild_batch_size(ctx: WorkspaceContext, uow: UnitOfWork) -> int:
    """Read through the transaction-bound override source, like every workspace key
    this module reads, never through the process-wide resolver."""
    return batch_size_in(
        TransactionBoundOverrideSource(
            uow, workspace_id=ctx.workspace_id
        ).workspace_overrides(ctx.workspace_id)
    )


def rebuild(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: EmbeddingRebuildInput
) -> EmbeddingRebuildScheduled:
    """Refuse without a provider; otherwise enqueue the one rebuild job and return."""
    if resolve_provider() is None:
        raise OperationRefused(
            EMBEDDING_PROVIDER_UNAVAILABLE,
            "no embedding provider is configured for this deployment",
        )
    operation_id = uow.operation_id if isinstance(uow, HandlerUnitOfWork) else None
    if operation_id is None:
        raise RebuildOutsideDispatch(
            f"{EMBEDDING_REBUILD} runs only as a long-running dispatch"
        )
    enqueue_rebuild_job(
        uow,
        operation_id=operation_id,
        batch_size=rebuild_batch_size(ctx, uow),
        now=datetime.now(UTC),
    )
    return EmbeddingRebuildScheduled(operation_id=operation_id)


EMBEDDING_REBUILD_DECLARATION: Final = OperationDeclaration(
    name=EMBEDDING_REBUILD,
    safety_class=SafetyClass.MUTATE,
    # Owner alone: a rebuild deletes and rewrites every vector in the workspace and
    # holds a worker for the whole walk. That is an administrative act, not a write a
    # member or a service makes in the ordinary course.
    roles=frozenset({Role.OWNER}),
    input_model=EmbeddingRebuildInput,
    output=EmbeddingRebuildScheduled,
    idempotency=Idempotency.NONE,
    # ``subject_field=None``: the rebuild acts on the workspace's whole index rather
    # than on one record a reference could name.
    audit=AuditSpec(subject_field=None),
    long_running=True,
)


class EmbeddingCoverageInput(Strict):
    """No fields: the model is the deployment's, and the workspace is the context's."""


class EmbeddingCoverageReport(Strict):
    """This workspace's live memories, and how many carry a vector from the configured
    provider's model. All three ``None`` when no provider resolves: with nothing
    configured there is no model to count coverage against."""

    model_id: str | None
    live: int | None
    embedded: int | None


def coverage(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: EmbeddingCoverageInput
) -> EmbeddingCoverageReport:
    """Per workspace by construction, because a workspace is a database.

    The retention read comes first, as on every read path, so an unusable policy
    refuses ``retention_unavailable`` here too rather than answering a count.
    """
    opened(ctx, uow)
    provider = resolve_provider()
    if provider is None:
        return EmbeddingCoverageReport(model_id=None, live=None, embedded=None)
    counted = embedding_coverage(uow.connection, model_id=provider.model_id)
    return EmbeddingCoverageReport(
        model_id=provider.model_id, live=counted.live, embedded=counted.embedded
    )


EMBEDDING_COVERAGE_DECLARATION: Final = OperationDeclaration(
    name=EMBEDDING_COVERAGE,
    safety_class=SafetyClass.READ,
    # Owner alone, unlike every other read: ``live`` counts every live memory in the
    # workspace, other members' private ones included, and handing that total to a
    # member would report memories it may not see. The owner already sees the same
    # whole-workspace figure on the rebuild's progress record.
    roles=frozenset({Role.OWNER}),
    input_model=EmbeddingCoverageInput,
    output=EmbeddingCoverageReport,
    idempotency=Idempotency.NONE,
    audit=None,
)

EMBEDDING_OPERATIONS: Final[tuple[tuple[OperationDeclaration, Handler], ...]] = (
    (EMBEDDING_REBUILD_DECLARATION, rebuild),
    (EMBEDDING_COVERAGE_DECLARATION, coverage),
)
