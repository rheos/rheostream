"""The dense index: the vector's width, its HNSW index, the embed-input stamp, and the
memory table's analyze cadence.

Revision ID: 0003_dense_retrieval
Revises: 0002_memory_records

**Four statements, and none of them edits a table 0002 declared.** Revision 0002 builds
the spine with ``memory_metadata.create_all(checkfirst=False)``, so any change to
``memory_embedding``'s declaration in ``storage/tables.py`` would change what that
frozen revision creates, and this one would double-apply on a fresh database. So the
narrowed column type and the index are this revision's own DDL, and the new table is on
its own ``MetaData``.

1. ``memory_embedding.vector`` narrows to ``vector(EMBEDDING_DIMENSIONS)``. This is the
   dimension authority: Postgres refuses a row of any other width, rather than the
   service remembering to check. The ``ALTER`` fails while a row of another width is
   present, so a future model change is delete, alter, rebuild.
2. ``memory_embedding_vector_hnsw``, a cosine HNSW index, named explicitly: an unnamed
   index would be ``memory_embedding_vector_idx``, and the catalog census pins this
   name. pgvector cannot index a dimensionless column, which is why step 1 comes first.
   Every workspace reaches this over an empty table (no writer existed before this
   run), so the build is instant; a large one should be built serially.
3. ``embedding_state``, one row per workspace, created empty. The first
   ``recallatron.embedding.rebuild`` stamps it.
4. The memory table's autovacuum analyze cadence, tightened. See the comment on the
   statement for what it is for and where it stops.

**No downgrade.** Reversing step 1 (``ALTER COLUMN vector TYPE vector``) was probed and
works, which is a fact about the pin rather than a shipped path. A reversal, if one is
ever needed, is a forward revision. Downgrade is not supported in release one.
"""

from collections.abc import Sequence

from alembic import op
from rheo_recallatron.configuration import (
    EMBEDDING_DIMENSIONS,
    HNSW_EF_CONSTRUCTION,
    HNSW_M,
)
from rheo_recallatron.storage.tables import embedding_state_metadata

revision: str = "0003_dense_retrieval"
down_revision: str | None = "0002_memory_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE recallatron.memory_embedding "
        f"ALTER COLUMN vector TYPE vector({EMBEDDING_DIMENSIONS})"
    )
    op.execute(
        "CREATE INDEX memory_embedding_vector_hnsw ON recallatron.memory_embedding "
        "USING hnsw (vector vector_cosine_ops) "
        f"WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})"
    )
    # ``checkfirst=False``: a table already present means this chain is running against
    # a database it does not own, which must fail loudly, never skip.
    embedding_state_metadata.create_all(op.get_bind(), checkfirst=False)
    # The lexical query builder drops a multi-lexeme query's common terms by reading
    # this table's column statistics, and with no statistics it passes them through:
    # a fresh workspace, or a bulk import before the first analyze, then loses recent
    # hits off the end of the ranked window. These two settings make autovacuum
    # analyze the table after 50 changed rows plus 2% of it, rather than after 10%, so
    # an import is analyzed within about one autovacuum naptime of landing.
    #
    # **The limit, stated:** this is still autovacuum's schedule, not ours. On a
    # deployment where autovacuum is disabled or starved it never fires, the window
    # stays open, and nothing here detects that case.
    op.execute(
        "ALTER TABLE recallatron.memory SET ("
        "autovacuum_analyze_scale_factor = 0.02, "
        "autovacuum_analyze_threshold = 50)"
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
