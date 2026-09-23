"""``LexicalStrategy``: full-text ranking over the stored ``search_tsv`` column.

The statement is the one first-cut recall ran inline, relocated and not changed:
``plainto_tsquery``, ``ts_rank_cd`` over ``search_tsv``, the row-local candidate
filter, ``ORDER BY score DESC, recorded_at, id`` and the request's limit.
"""

from typing import Any, Final

from rheo_contracts import WorkspaceContext
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import ColumnElement, func, literal_column, select

from rheo_recallatron.configuration import STRATEGY_LEXICAL
from rheo_recallatron.eligibility import row_local_conditions
from rheo_recallatron.retrieval.protocol import (
    ArmProvenance,
    Hit,
    IndexItem,
    SearchRequest,
    SearchResult,
)
from rheo_recallatron.storage import tables as t

_SEARCH_CONFIG: Final[ColumnElement[Any]] = literal_column("'english'::regconfig")
"""The text-search configuration the query must use.

It has to be the one the stored generated column was built with — see the
``search_tsv`` column in this package's tables module — or the query would be matched
against lexemes produced by a different dictionary. Cast explicitly for the same
reason the generated column casts: the one-argument form reads a session setting and
is only ``STABLE``.
"""


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
        query = func.plainto_tsquery(_SEARCH_CONFIG, request.query)
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
            Hit(ref=candidate, score=float(candidate_score), strategy=self.name)
            for candidate, candidate_score in uow.connection.execute(statement).all()
        )
        return SearchResult(
            hits=hits,
            arms=ArmProvenance(lexical=len(hits), dense=0),
            dense_available=False,
        )
