"""``DenseStrategy``, write side: the one writer of ``memory_embedding`` rows.

**The write side takes no context.** :meth:`DenseStrategy.index_many` takes a unit of
work and a resolved provider and nothing else, because its callers are worker jobs: a
job handler is ``(HandlerUnitOfWork, payload, CancellationToken)``, holds no
``WorkspaceContext``, and no boundary factory can mint one for it. ``index`` and
``invalidate`` carry ``ctx`` only because the ``RetrievalStrategy`` protocol does.

Not registered in the strategy registry yet, and it cannot be: registering it is the
same change that offers ``dense`` in the strategy key's ``choices``, and the registry's
import-time check refuses the other order.

**The lifecycle closure does not call** :meth:`DenseStrategy.invalidate`. Correction,
supersession and deletion delete a memory's vectors through the repository directly and
unconditionally, whichever strategy is configured: a workspace that moved from ``dense``
back to ``lexical`` must still drop the vectors its dense period wrote.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from rheo_contracts import WorkspaceContext
from rheo_core.refs.resolver import UnitOfWork

from rheo_recallatron.configuration import STRATEGY_DENSE
from rheo_recallatron.embedding import embed_input
from rheo_recallatron.embedding.protocol import EmbeddingProvider
from rheo_recallatron.embedding.registry import resolve_provider
from rheo_recallatron.references import canonical_ref
from rheo_recallatron.retrieval.protocol import IndexItem
from rheo_recallatron.storage.repository import (
    MemoryEmbeddingRow,
    delete_memory_embeddings,
    insert_memory_embedding,
    is_live_memory,
    lock_memory_for_embedding,
)


class DenseStrategy:
    """Ranks by cosine similarity to the embedded query (the read side is to come)."""

    name: Final = STRATEGY_DENSE

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
