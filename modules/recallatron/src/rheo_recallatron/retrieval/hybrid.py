"""``HybridStrategy``: the lexical and dense arms, fused by reciprocal rank.

**Two arms at two widths, on purpose.** The lexical arm runs the ``lexical`` path's own
statement with the request unchanged, so its ``LIMIT`` is the request's, which
``recall()`` sets to the scan bound exactly as under ``lexical``. The dense arm is
``min(k * overfetch_multiplier, request.limit)`` wide. The multiplier is the dense
arm's alone: on the lexical arm it could only narrow a list already at the scan bound,
and the permission walk would then have fewer positions to find ``k`` readable rows in
than a ``lexical`` call gets. The fused list is cut to ``request.limit`` as well, so no
caller is handed more positions than it asked for.

**Fusion always runs, and hybrid never refuses.** A dense arm that cannot contribute
(no provider, a provider that raises, a dense statement that fails inside its
savepoint, or no row for the provider's model) is an empty list. One-armed fusion keeps
the lexical order exactly, since ``1 / (60 + rank)`` falls strictly with rank, so such
a response has the ``lexical`` rows in the ``lexical`` order, labelled ``hybrid``, with
``dense_available=false``. Every ``hybrid`` score is on the fused scale.

**The arms are this strategy's own instances.** It never looks them up in the strategy
registry, so what a workspace's setting chooses is the only thing that runs. A test may
hand it stand-in arms; the registered one builds its own.

Fusion and rerank are written here by hand, a few lines each, and borrow from nothing.
"""

import dataclasses
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings.schema import SettingTypeMismatch, decode_text
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from sqlalchemy import select

from rheo_recallatron.configuration import (
    OVERFETCH_MULTIPLIER_DEFAULT,
    OVERFETCH_MULTIPLIER_KEY,
    OVERFETCH_MULTIPLIER_SPEC,
    STRATEGY_HYBRID,
)
from rheo_recallatron.retrieval.dense import DenseStrategy
from rheo_recallatron.retrieval.lexical import LexicalStrategy
from rheo_recallatron.retrieval.protocol import (
    ArmProvenance,
    Hit,
    IndexItem,
    RetrievalStrategy,
    SearchRequest,
    SearchResult,
)
from rheo_recallatron.storage import tables as t

RRF_CONSTANT: Final = 60
"""The reciprocal-rank-fusion constant, fixed by the storage architecture's retrieval
adapter. It damps the head of each list: rank 1 scores ``1/61``, rank 2 ``1/62``."""

_NOT_READ: Final = datetime.min.replace(tzinfo=UTC)
"""The rerank's timestamp for a ranked id whose row was gone by the time its
``recorded_at`` was read (deleted by a commit between two of this call's statements).
It sorts oldest, and the permission walk then drops it as not found."""


# --- the over-fetch multiplier --------------------------------------------------------


def overfetch_multiplier_in(stored_rows: Mapping[str, str]) -> int:
    """The workspace's over-fetch multiplier, from override rows already read.

    An absent row, one that will not decode and one outside ``1..10`` all read as the
    package default: a read degrades, it never raises on a bad stored value.
    """
    stored = stored_rows.get(OVERFETCH_MULTIPLIER_KEY)
    if stored is None:
        return OVERFETCH_MULTIPLIER_DEFAULT
    try:
        value = decode_text(
            OVERFETCH_MULTIPLIER_SPEC, stored, source="the stored workspace override"
        )
    except SettingTypeMismatch:
        return OVERFETCH_MULTIPLIER_DEFAULT
    return value if isinstance(value, int) else OVERFETCH_MULTIPLIER_DEFAULT


def overfetch_multiplier(ctx: WorkspaceContext, uow: UnitOfWork) -> int:
    """Read through the transaction-bound override source, like every workspace key
    this module reads, never through the process-wide resolver."""
    return overfetch_multiplier_in(
        TransactionBoundOverrideSource(
            uow, workspace_id=ctx.workspace_id
        ).workspace_overrides(ctx.workspace_id)
    )


# --- fusion and rerank ----------------------------------------------------------------


def fuse(
    arms: Sequence[Sequence[UUID]], recorded_at: Mapping[UUID, datetime]
) -> list[tuple[UUID, float]]:
    """Reciprocal rank fusion, then the bounded rerank: every ranked id, best first,
    with its fused score.

    ``score(d) = Σ 1 / (60 + rank)`` over the arms that returned ``d``, ranks 1-based.
    An arm that did not return ``d`` adds nothing, so a memory both arms found outranks
    one only a single arm found at the same rank.

    **Within an equal fused score, newer first, then ``id`` ascending.** That makes the
    order total, so the same arms always fuse to the same list. Three stable sorts,
    least significant key first.
    """
    scores: dict[UUID, float] = {}
    for arm in arms:
        for rank, ref in enumerate(arm, start=1):
            scores[ref] = scores.get(ref, 0.0) + 1.0 / (RRF_CONSTANT + rank)
    ordered = sorted(scores)
    ordered.sort(key=lambda ref: recorded_at.get(ref, _NOT_READ), reverse=True)
    ordered.sort(key=scores.__getitem__, reverse=True)
    return [(ref, scores[ref]) for ref in ordered]


def _recorded_at(uow: UnitOfWork, ids: Collection[UUID]) -> dict[UUID, datetime]:
    """``recorded_at`` for every ranked id, in one statement. A ``Hit`` carries no
    timestamp, and the rerank needs one only for the ids that were ranked."""
    if not ids:
        return {}
    memory = t.memory
    rows = uow.connection.execute(
        select(memory.c.id, memory.c.recorded_at).where(memory.c.id.in_(sorted(ids)))
    ).all()
    return {memory_id: recorded for memory_id, recorded in rows}


# --- the strategy ---------------------------------------------------------------------


class HybridStrategy:
    """Lexical and dense, fused by reciprocal rank and reranked by recency."""

    name: Final = STRATEGY_HYBRID

    def __init__(
        self,
        *,
        lexical: RetrievalStrategy | None = None,
        dense: RetrievalStrategy | None = None,
    ) -> None:
        self._lexical: RetrievalStrategy = (
            LexicalStrategy() if lexical is None else lexical
        )
        self._dense: RetrievalStrategy = DenseStrategy() if dense is None else dense

    def index(self, ctx: WorkspaceContext, uow: UnitOfWork, item: IndexItem) -> None:
        """Both arms' indexes. The lexical one lives on the row and needs nothing."""
        self._lexical.index(ctx, uow, item)
        self._dense.index(ctx, uow, item)

    def invalidate(self, ctx: WorkspaceContext, uow: UnitOfWork, ref: str) -> None:
        """Both arms' entries for ``ref``."""
        self._lexical.invalidate(ctx, uow, ref)
        self._dense.invalidate(ctx, uow, ref)

    def search(
        self, ctx: WorkspaceContext, uow: UnitOfWork, request: SearchRequest
    ) -> SearchResult:
        """Both arms, fused, reranked, at most ``request.limit`` positions.

        The dense arm runs first. Its statement runs inside the dense strategy's own
        savepoint, so when it fails the lexical arm, the ``recorded_at`` read and the
        permission walk after it all run on the transaction that survived.

        ``arms`` counts each arm's list as it entered fusion; ``dense_available`` is
        the dense arm's own answer.
        """
        width = min(request.k * overfetch_multiplier(ctx, uow), request.limit)
        dense = self._dense.search(ctx, uow, dataclasses.replace(request, limit=width))
        lexical = self._lexical.search(ctx, uow, request)

        ranked = (
            [hit.ref for hit in lexical.hits],
            [hit.ref for hit in dense.hits],
        )
        fused = fuse(ranked, _recorded_at(uow, {ref for arm in ranked for ref in arm}))
        return SearchResult(
            hits=tuple(
                Hit(ref=ref, score=score, strategy=self.name)
                for ref, score in fused[: request.limit]
            ),
            arms=ArmProvenance(lexical=len(lexical.hits), dense=len(dense.hits)),
            dense_available=dense.dense_available,
        )
