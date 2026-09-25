"""``LexicalStrategy``: full-text ranking over the stored ``search_tsv`` column.

The statement is the one first-cut recall ran inline — ``ts_rank_cd`` over
``search_tsv``, the row-local candidate filter, ``ORDER BY score DESC, recorded_at,
id`` and the request's limit — with one deliberate change: the query is built by
:mod:`rheo_recallatron.retrieval.lexical_query` (an OR over the discriminating lexemes)
rather than by ``plainto_tsquery`` (an AND over all of them).
"""

from typing import Final

from rheo_contracts import WorkspaceContext
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import func, select

from rheo_recallatron.configuration import STRATEGY_LEXICAL
from rheo_recallatron.eligibility import row_local_conditions
from rheo_recallatron.retrieval.lexical_query import lexical_tsquery
from rheo_recallatron.retrieval.protocol import (
    ARM_LEXICAL,
    ArmProvenance,
    Hit,
    IndexItem,
    SearchRequest,
    SearchResult,
)
from rheo_recallatron.storage import tables as t


class LexicalStrategy:
    """Ranks by ``ts_rank_cd`` cover density. Needs no provider and no network."""

    name: Final = STRATEGY_LEXICAL

    def index(self, ctx: WorkspaceContext, uow: UnitOfWork, item: IndexItem) -> None:
        """Nothing to do: ``search_tsv`` is a generated column, so the database
        rebuilds it in the same statement that writes or corrects the row."""

    def invalidate(self, ctx: WorkspaceContext, uow: UnitOfWork, ref: str) -> None:
        """Nothing to do, for the same reason: the index entry lives on the row."""

    def search(
        self, ctx: WorkspaceContext, uow: UnitOfWork, request: SearchRequest
    ) -> SearchResult:
        query = lexical_tsquery(uow.connection, request.query)
        score = func.ts_rank_cd(t.memory.c.search_tsv, query)
        statement = (
            select(t.memory.c.id, score.label("score"))
            .where(
                t.memory.c.search_tsv.bool_op("@@")(query),
                *row_local_conditions(request.memory, request.mode),
            )
            .order_by(score.desc(), t.memory.c.recorded_at, t.memory.c.id)
            .limit(request.limit)
        )
        hits = tuple(
            Hit(
                ref=candidate,
                score=float(candidate_score),
                strategy=self.name,
                arms=frozenset({ARM_LEXICAL}),
            )
            for candidate, candidate_score in uow.connection.execute(statement).all()
        )
        return SearchResult(
            hits=hits,
            arms=ArmProvenance(lexical=len(hits), dense=0),
            dense_available=False,
        )
