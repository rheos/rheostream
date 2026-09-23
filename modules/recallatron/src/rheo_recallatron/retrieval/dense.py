"""``DenseStrategy``: ranks by cosine similarity to the embedded query, and is the one
writer of ``memory_embedding`` rows.

**The write side takes no context.** :meth:`DenseStrategy.index_many` takes a unit of
work and a resolved provider and nothing else, because its callers are worker jobs: a
job handler is ``(HandlerUnitOfWork, payload, CancellationToken)``, holds no
``WorkspaceContext``, and no boundary factory can mint one for it. ``index`` and
``invalidate`` carry ``ctx`` only because the ``RetrievalStrategy`` protocol does.

**The read side degrades; it never refuses and never falls back.** :meth:`search`
answers no hits and ``dense_available=False`` for four causes: no provider resolves,
the provider raises, the dense statement raises, or the workspace holds no row for the
provider's model. Each one logs a warning naming the provider and the failure class.
None of them hands back lexical results under the ``dense`` label: a caller who chose
``dense`` would have no way to tell.

**The statement runs inside a savepoint, and the ``try`` sits outside it.** Postgres
aborts the whole transaction on an in-statement error, so without the savepoint a
failing dense arm fails the ``recall()`` around it. The ``except`` has to be outside
the ``with`` block: the exception must cross the block so the context manager rolls
back to the savepoint. Caught inside, the block exits normally, ``RELEASE SAVEPOINT``
runs against a failed savepoint, and ``InFailedSqlTransaction`` takes the outer
transaction down (both spellings were run against the cluster).

Registered as ``dense``; ``HybridStrategy`` holds an instance of its own as its dense
arm and asks it for a narrower ``limit``.

**The lifecycle closure does not call** :meth:`DenseStrategy.invalidate`. Correction,
supersession and deletion delete a memory's vectors through the repository directly and
unconditionally, whichever strategy is configured: a workspace that moved from ``dense``
back to ``lexical`` must still drop the vectors its dense period wrote.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from rheo_contracts import WorkspaceContext
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings.schema import SettingTypeMismatch, decode_text
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from sqlalchemy import (
    ColumnElement,
    Float,
    Integer,
    Numeric,
    Select,
    bindparam,
    cast,
    func,
    literal_column,
    select,
)
from sqlalchemy.exc import DBAPIError

from rheo_recallatron.configuration import (
    DENSE_FLOOR_PERCENT_DEFAULT,
    DENSE_FLOOR_PERCENT_KEY,
    DENSE_FLOOR_PERCENT_SPEC,
    HNSW_EF_SEARCH_MAX,
    HNSW_EF_SEARCH_MIN,
    HNSW_EF_SEARCH_MULTIPLIER,
    HNSW_MAX_SCAN_TUPLES,
    STRATEGY_DENSE,
)
from rheo_recallatron.eligibility import row_local_conditions
from rheo_recallatron.embedding import embed_input
from rheo_recallatron.embedding.protocol import EmbeddingProvider
from rheo_recallatron.embedding.registry import resolve_provider
from rheo_recallatron.references import canonical_ref
from rheo_recallatron.retrieval.protocol import (
    ArmProvenance,
    Hit,
    IndexItem,
    SearchRequest,
    SearchResult,
)
from rheo_recallatron.storage import tables as t
from rheo_recallatron.storage.repository import (
    MemoryEmbeddingRow,
    delete_memory_embeddings,
    embedding_exists_for_model,
    insert_memory_embedding,
    is_live_memory,
    lock_memory_for_embedding,
    vector_literal,
)

_log = logging.getLogger(__name__)

NO_PROVIDER: Final = "no provider"
PROVIDER_RAISED: Final = "provider raised"
STATEMENT_RAISED: Final = "statement raised"
NO_EMBEDDINGS: Final = "no embedding rows for the model"
"""The four causes of ``dense_available=False``, as the warning names them."""


# --- the relevance floor --------------------------------------------------------------


def dense_floor_percent_in(stored_rows: Mapping[str, str]) -> int:
    """The workspace's dense floor, in percent, from override rows already read.

    An absent row, one that will not decode and one outside ``0..100`` all read as the
    package default: a read degrades, it never raises on a bad stored value.
    """
    stored = stored_rows.get(DENSE_FLOOR_PERCENT_KEY)
    if stored is None:
        return DENSE_FLOOR_PERCENT_DEFAULT
    try:
        value = decode_text(
            DENSE_FLOOR_PERCENT_SPEC, stored, source="the stored workspace override"
        )
    except SettingTypeMismatch:
        return DENSE_FLOOR_PERCENT_DEFAULT
    return value if isinstance(value, int) else DENSE_FLOOR_PERCENT_DEFAULT


def dense_floor_percent(ctx: WorkspaceContext, uow: UnitOfWork) -> int:
    """Read through the transaction-bound override source, like every workspace key
    this module reads, never through the process-wide resolver."""
    return dense_floor_percent_in(
        TransactionBoundOverrideSource(
            uow, workspace_id=ctx.workspace_id
        ).workspace_overrides(ctx.workspace_id)
    )


# --- the statement --------------------------------------------------------------------


def dense_distance(query_vector: Sequence[float]) -> ColumnElement[float]:
    """``memory_embedding.vector <=> :query_vector``, cosine **distance**.

    The ``ORDER BY`` key, ascending, and nothing else. The query vector is bound in
    pgvector's text form and cast to the column's type, the same form a stored vector
    is written in. Its width is not checked here: the ``vector(384)`` column is the
    one authority, and a wrong width fails inside the statement, where the savepoint
    catches it.
    """
    query = cast(
        bindparam("query_vector", vector_literal(query_vector), type_=t.Vector),
        t.Vector,
    )
    return t.memory_embedding.c.vector.op("<=>", return_type=Float)(query)


def dense_similarity(distance: ColumnElement[float]) -> ColumnElement[float]:
    """``1 - distance``, cosine **similarity**: the score, and what the floor bounds."""
    return literal_column("1", Float) - distance


def dense_floor(
    similarity: ColumnElement[float], floor_percent: int
) -> ColumnElement[bool]:
    """``similarity >= :dense_floor_percent / 100.0``.

    Written once, here, as a similarity comparison. ``<=>`` is a distance, so a
    predicate that compared the distance against the floor, or flipped the operator,
    would invert the filter silently: every relevant row out, every irrelevant one in.
    """
    # A bare ``/`` rather than SQLAlchemy's division, so the SQL reads exactly as the
    # spec writes it: an integer over a numeric literal, a numeric result.
    threshold = bindparam("dense_floor_percent", floor_percent, type_=Integer).op(
        "/", return_type=Numeric
    )(literal_column("100.0", Numeric))
    return similarity >= threshold


def build_dense_statement(
    request: SearchRequest,
    *,
    model_id: str,
    query_vector: Sequence[float],
    floor_percent: int,
) -> Select[tuple[Any, float]]:
    """The dense arm's one statement, exactly as :meth:`DenseStrategy.search` runs it.

    The lexical arm's row-local candidate set, joined to the rows ``model_id`` wrote,
    kept only where the similarity clears the floor, nearest first, at most
    ``request.limit`` rows.

    **Ordered by the distance alone.** HNSW can serve an ``ORDER BY`` only when it is
    exactly ``vector <=> constant``; a second sort key leaves it no ordered path, the
    planner falls back to a full sort, and the index is off the plan.
    """
    distance = dense_distance(query_vector)
    similarity = dense_similarity(distance)
    return (
        select(t.memory.c.id, similarity.label("score"))
        .select_from(
            t.memory.join(
                t.memory_embedding, t.memory_embedding.c.memory_id == t.memory.c.id
            )
        )
        .where(
            t.memory_embedding.c.model_id == model_id,
            dense_floor(similarity, floor_percent),
            *row_local_conditions(request.memory, request.mode),
        )
        .order_by(distance)
        .limit(request.limit)
    )


def hnsw_ef_search(limit: int) -> int:
    """``hnsw.ef_search`` for an arm of ``limit`` rows.

    **The clamp is not optional.** The setting's range is ``1..1000``, and a
    ``dense``-only arm asks for 500 rows: unclamped that is 2000, which Postgres
    refuses, so every such recall would fail its statement.
    """
    return min(
        max(limit * HNSW_EF_SEARCH_MULTIPLIER, HNSW_EF_SEARCH_MIN), HNSW_EF_SEARCH_MAX
    )


def hnsw_scan_settings(limit: int) -> Select[Any]:
    """The three settings every dense statement runs under, as ``SET LOCAL``.

    ``set_config(name, value, true)`` is ``SET LOCAL``, and unlike ``SET`` it takes
    bound parameters. ``iterative_scan = strict_order`` lets a filtered scan resume
    past its first ``ef_search`` candidates until the ``LIMIT`` fills, in true distance
    order; ``max_scan_tuples`` bounds that work, and is the knob that decides whether
    the arm fills its ``LIMIT`` and what it costs when it cannot.

    In a backend that has not loaded pgvector yet these write placeholders, which the
    library adopts, still transaction-local, when the statement loads it (verified). An
    out-of-range value is the exception: on load it is dropped to the default with only
    a warning, while in a warm backend it raises.
    """
    return select(
        func.set_config("hnsw.ef_search", str(hnsw_ef_search(limit)), True),
        func.set_config("hnsw.iterative_scan", "strict_order", True),
        func.set_config("hnsw.max_scan_tuples", str(HNSW_MAX_SCAN_TUPLES), True),
    )


# --- the strategy ---------------------------------------------------------------------


_UNAVAILABLE: Final = SearchResult(
    hits=(), arms=ArmProvenance(lexical=0, dense=0), dense_available=False
)


def _error_class(error: BaseException) -> str:
    """The failure's class, the driver's own for a database error: ``DataException``,
    not SQLAlchemy's wrapper around it."""
    if isinstance(error, DBAPIError) and error.orig is not None:
        return type(error.orig).__name__
    return type(error).__name__


def _unavailable(
    cause: str,
    provider: EmbeddingProvider | None,
    error: BaseException | None = None,
) -> SearchResult:
    """Log why the dense arm cannot contribute, and answer that it cannot.

    Provider identity and failure class only: never memory text, never the query.
    Without the provider's identity a provider registered at the wrong width looks
    exactly like no provider at all, one silent zero on every call.
    """
    model_id = None if provider is None else provider.model_id
    dimensions = None if provider is None else provider.dimensions
    error_class = None if error is None else _error_class(error)
    _log.warning(
        "dense retrieval unavailable (%s): provider %s, %s dimensions, error %s",
        cause,
        model_id or "none resolved",
        dimensions or "no",
        error_class or "none",
        extra={
            "dense_cause": cause,
            "provider_model_id": model_id,
            "provider_dimensions": dimensions,
            "error_class": error_class,
        },
    )
    return _UNAVAILABLE


class DenseStrategy:
    """Ranks by cosine similarity to the embedded query, above the relevance floor."""

    name: Final = STRATEGY_DENSE

    def search(
        self, ctx: WorkspaceContext, uow: UnitOfWork, request: SearchRequest
    ) -> SearchResult:
        """At most ``request.limit`` candidates at or above the floor, nearest first.

        ``dense_available`` is true only when a provider resolved, the bare query
        embedded, the statement ran, **and** the workspace holds at least one row for
        the provider's model. The last condition is what tells a workspace nothing has
        embedded yet (unavailable) from one where only this candidate's row is missing
        (available, the row simply absent). ``arms.dense`` is the arm's list after its
        ``LIMIT`` and the floor.
        """
        provider = resolve_provider()
        if provider is None:
            return _unavailable(NO_PROVIDER, None)
        try:
            # The bare query, not ``embed_input``: a query has no title.
            vectors = provider.embed([request.query])
            if len(vectors) != 1:
                raise ValueError(f"answered {len(vectors)} vectors for one query")
            query_vector = tuple(float(value) for value in vectors[0])
        except Exception as error:  # any provider failure degrades; none refuses
            return _unavailable(PROVIDER_RAISED, provider, error)

        statement = build_dense_statement(
            request,
            model_id=provider.model_id,
            query_vector=query_vector,
            floor_percent=dense_floor_percent(ctx, uow),
        )
        connection = uow.connection
        # The ``try`` is outside the savepoint on purpose; the module docstring says
        # why, and moving it inside kills the outer transaction.
        try:
            with connection.begin_nested():
                connection.execute(hnsw_scan_settings(request.limit))
                rows = connection.execute(statement).all()
                embedded = embedding_exists_for_model(
                    connection, model_id=provider.model_id
                )
        except DBAPIError as error:
            if error.connection_invalidated:
                # A dropped backend takes the session whatever a savepoint does.
                raise
            return _unavailable(STATEMENT_RAISED, provider, error)
        if not embedded:
            return _unavailable(NO_EMBEDDINGS, provider)

        hits = tuple(
            Hit(ref=memory_id, score=float(score), strategy=self.name)
            for memory_id, score in rows
        )
        return SearchResult(
            hits=hits,
            arms=ArmProvenance(lexical=0, dense=len(hits)),
            dense_available=True,
        )

    def index_many(
        self,
        uow: UnitOfWork,
        items: Sequence[IndexItem],
        *,
        provider: EmbeddingProvider,
    ) -> int:
        """Embed the items in one provider call, write their rows, count the new ones.

        Each item embeds :func:`~rheo_recallatron.embedding.embed_input` of its title
        and body, under the provider's ``model_id`` and width. There is no clock in the
        signature, so the rows are stamped here, as a handler stamps its own writes. A
        provider that raises, or answers the wrong number of vectors, raises out of this
        call before a single row is written. An item whose memory already has a row for
        this model — another writer holding the same share lock got there first — is
        left as it is and not counted (see ``insert_memory_embedding``).

        **The caller's precondition: every item's memory row is held ``FOR SHARE`` by
        this transaction, and the item carries the text that row holds** — the
        repository's vector-write handshake. The embed job, the rebuild's fill and
        :meth:`index` each read their rows that way before calling this; a test that
        seeds rows single-threaded has nothing to race and may call it directly.
        """
        if not items:
            return 0
        vectors = provider.embed([embed_input(item.title, item.body) for item in items])
        if len(vectors) != len(items):
            raise ValueError(
                f"embedding provider {provider.model_id!r} answered {len(vectors)} "
                f"vectors for {len(items)} texts"
            )
        embedded_at = datetime.now(UTC)
        written = 0
        for item, vector in zip(items, vectors, strict=True):
            if insert_memory_embedding(
                uow.connection,
                MemoryEmbeddingRow(
                    memory_id=item.memory_id,
                    model_id=provider.model_id,
                    dimensions=provider.dimensions,
                    vector=vector,
                    embedded_at=embedded_at,
                ),
            ):
                written += 1
        return written

    def index(self, ctx: WorkspaceContext, uow: UnitOfWork, item: IndexItem) -> None:
        """Embed one memory with the configured provider; nothing when none resolves.

        The item's text is the caller's, so it is checked against the row this call
        locks ``FOR SHARE``: a row that is gone, no longer live, or holding other text
        gets no vector, because one written from that text would be stale on commit.
        """
        provider = resolve_provider()
        if provider is None:
            return
        row = lock_memory_for_embedding(uow.connection, item.memory_id)
        if (
            row is None
            or not is_live_memory(row)
            or (row.title, row.body) != (item.title, item.body)
        ):
            return
        self.index_many(uow, [item], provider=provider)

    def invalidate(self, ctx: WorkspaceContext, uow: UnitOfWork, ref: str) -> None:
        """Remove every vector of the memory ``ref`` names, whatever model wrote it."""
        delete_memory_embeddings(uow.connection, canonical_ref(ref).id)
