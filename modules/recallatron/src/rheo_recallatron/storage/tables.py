"""The six-table memory spine and the ``source_receipt`` seam, as SQLAlchemy Core
``Table`` objects in schema ``recallatron``.

Columns, constraints and indexes are verbatim from Architecture § A3 — the ratified
schema, not a proposal. Construction mirrors ``packages/core/src/rheo_core/storage/
work_tables.py``: one ``MetaData`` per owning schema, a ``_timestamptz()`` helper, and
an ``_in()`` helper rendering each closed vocabulary as a ``CheckConstraint`` so the
database refuses a value the service never should have written.

**One ``MetaData`` for this revision, for the reason ``work_tables.py`` gives.**
``0002_memory_records`` calls ``memory_metadata.create_all(checkfirst=False)``, so a
later revision adding a table must carry its own ``MetaData`` rather than attaching to
this one — otherwise revision 0002 would create it too, editing a frozen revision by
side effect.

**Schema only, deliberately.** Nothing here reads or writes: eligibility, permission,
retention and the lifecycle closure are Prompts 6-9's, and a table with no reader is
the correct state at this revision. ``repository.py`` beside this file is the thin
row mapping over the same shapes, and is equally free of service logic.

**Every addressable identifier is UUIDv7** (``rheo_core.refs.uuid7``), minted in the
transaction that creates the row. The four association tables — ``memory_purpose``,
``memory_mention``, ``memory_link``, ``memory_embedding`` — carry only their ratified
composite primary keys; none gains a synthetic id.
"""

from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.types import UserDefinedType

MEMORY_SCHEMA: Final = "recallatron"

MEMORY_KINDS: Final = ("note", "fact", "decision", "summary")
"""The four ratified kinds.

The database half of AC 14's absence proof: ``topic_thread`` and ``procedural_notes``
are not values here, so no migration, tool or import can introduce one without
changing this tuple and its ``CheckConstraint`` — which is a visible schema change,
not a quiet row.
"""

MEMORY_ORIGINS: Final = ("told", "derived", "migrated")
AUDIENCE_KINDS: Final = ("workspace", "member")
INVALIDATION_REASONS: Final = (
    "source_deleted",
    "source_corrected",
    "source_superseded",
)
PURPOSES: Final = ("respond", "follow_up", "share_with_referral", "internal_analysis")
"""The same four members ``rheo_contracts.purposes.ContextPurpose`` declares.

Spelled as strings rather than imported from that enum so the DDL is readable as DDL
and this module imports nothing it does not store; the service layer that writes these
rows is what holds the two in step, and it types its input against the enum.
"""

ENTITY_KINDS: Final = (
    "person",
    "organization",
    "project",
    "topic",
    "place",
    "thing",
)
LINK_RELATIONS: Final = ("derived_from", "about")
REPRESENTATION_TYPES: Final = ("memory", "entity")
RECEIPT_STATES: Final = ("active", "noop", "denied", "erased", "expired", "orphaned")
PRODUCER_KINDS: Final = ("migration", "rheo_runtime", "claude_code_local")

ROLE_MAX_LENGTH: Final = 100
"""Architecture § A13's bound on a mention's role, as the database's own check.

The typed contracts own the same limit (Prompt 7); this is the half that holds for a
row the service did not write — a validated import, or a future adapter.
"""

DIGEST_BYTES: Final = 32
"""A SHA-256 digest, so a 32-byte ``bytea`` and nothing else."""

MEMORY_REF_PREFIX: Final = f"{MEMORY_SCHEMA}.memory:"
"""The canonical reference prefix for this module's ``memory`` record type.

``rheo_contracts.refs.RecordRef`` renders ``<module>.<record_type>:<uuid>``, and
``memory_link``'s lineage check below is the one place the DDL has to recognise one.
"""

memory_metadata = MetaData(schema=MEMORY_SCHEMA)


class Vector(UserDefinedType[Any]):
    """pgvector's unbounded ``vector`` column type, declared rather than imported.

    The alternative is a ``pgvector`` Python dependency, and 1a1 has no use for what
    it adds: this run stores no embedding, builds no index over one, and declares no
    provider. What it needs is the *column type*, so that a later run's writer has
    somewhere to put a vector and this run's census can prove no dense index exists.
    Six lines of DDL spelling beats a dependency and a lock churn for that.

    Unbounded on purpose — ``vector`` with no dimension — because a persisted
    dimension authority is explicitly not 1a1's (§ A3); the row's own ``dimensions``
    column records what was written.
    """

    cache_ok = True

    def get_col_spec(self, **_kw: object) -> str:
        return "vector"


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _paired(first: str, second: str) -> str:
    """Both columns null or both present — the shape § A3 calls a "nullable paired"."""
    return f"({first} IS NULL) = ({second} IS NULL)"


# ``search_tsv`` is ``GENERATED ALWAYS AS (...) STORED`` rather than a trigger: there is
# no generated-column precedent in ``rheo_core``'s own chain to mirror, and a generated
# column cannot drift from its source the way a trigger that somebody forgets to attach
# to a bulk import can. The regconfig is cast explicitly because the one-argument
# ``to_tsvector(text)`` is only STABLE — it reads ``default_text_search_config`` — and
# Postgres refuses a non-immutable expression in a generated column.
#
# ``superseded_by_id`` is a self-FK with ON DELETE SET NULL, so physically removing a
# successor leaves its predecessor addressable rather than cascading the removal
# backwards into history.
memory = Table(
    "memory",
    memory_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column(
        "search_tsv",
        TSVECTOR,
        Computed(
            "to_tsvector('english'::regconfig, title || ' ' || body)", persisted=True
        ),
        nullable=False,
    ),
    Column("audience_kind", Text, nullable=False),
    Column("audience_id", UUID(as_uuid=True), nullable=True),
    Column("confidence", Float, nullable=True),
    Column("occurred_at", _timestamptz(), nullable=True),
    Column("recorded_at", _timestamptz(), nullable=False),
    Column("recorded_by_kind", Text, nullable=False),
    Column("recorded_by_id", UUID(as_uuid=True), nullable=True),
    Column("origin", Text, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("corrected_at", _timestamptz(), nullable=True),
    Column(
        "superseded_by_id",
        UUID(as_uuid=True),
        ForeignKey("recallatron.memory.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("invalidated_at", _timestamptz(), nullable=True),
    Column("invalidation_reason", Text, nullable=True),
    Column("source_namespace", Text, nullable=True),
    Column("external_source_key", Text, nullable=True),
    CheckConstraint(_in("kind", MEMORY_KINDS), name="memory_kind"),
    CheckConstraint(_in("audience_kind", AUDIENCE_KINDS), name="memory_audience_kind"),
    # "``audience_id`` null exactly for workspace" — an equality between two booleans,
    # so neither a workspace memory carrying a member id nor a member memory carrying
    # none is representable.
    CheckConstraint(
        "(audience_kind = 'workspace') = (audience_id IS NULL)",
        name="memory_audience_pairing",
    ),
    CheckConstraint(
        "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
        name="memory_confidence_range",
    ),
    CheckConstraint(_in("origin", MEMORY_ORIGINS), name="memory_origin"),
    CheckConstraint("revision >= 1", name="memory_revision_positive"),
    CheckConstraint(
        _paired("invalidated_at", "invalidation_reason"),
        name="memory_invalidation_pairing",
    ),
    CheckConstraint(
        "invalidation_reason IS NULL OR "
        + _in("invalidation_reason", INVALIDATION_REASONS),
        name="memory_invalidation_reason",
    ),
    CheckConstraint(
        _paired("source_namespace", "external_source_key"),
        name="memory_source_key_pairing",
    ),
    Index(
        "memory_search_tsv_gin",
        "search_tsv",
        postgresql_using="gin",
    ),
    Index(
        "memory_title_trgm",
        "title",
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    ),
    Index("memory_recorded_at_id", "recorded_at", "id"),
    Index("memory_audience", "audience_kind", "audience_id"),
    # Per representation type and immutable, never globally unique: the partial
    # predicate is what keeps the index off every ordinary memory, which carries no
    # source key at all.
    Index(
        "memory_source_key",
        "source_namespace",
        "external_source_key",
        unique=True,
        postgresql_where=text("source_namespace IS NOT NULL"),
    ),
)

memory_purpose = Table(
    "memory_purpose",
    memory_metadata,
    Column(
        "memory_id",
        UUID(as_uuid=True),
        ForeignKey("recallatron.memory.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("purpose", Text, nullable=False),
    PrimaryKeyConstraint("memory_id", "purpose", name="memory_purpose_pkey"),
    CheckConstraint(_in("purpose", PURPOSES), name="memory_purpose_purpose"),
    Index("memory_purpose_purpose_memory", "purpose", "memory_id"),
)

# **No uniqueness constraint on ``name`` or ``normalized_name``, and no global name
# probe.** Two entities with the same normalized name are two entities; the index below
# is nonunique because its job is to filter a caller's *already eligible* set, never to
# answer "does this name exist here" — which would be a cross-audience oracle.
memory_entity = Table(
    "memory_entity",
    memory_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("normalized_name", Text, nullable=False),
    Column("ref", Text, nullable=True),
    Column("created_at", _timestamptz(), nullable=False),
    Column("source_namespace", Text, nullable=True),
    Column("external_source_key", Text, nullable=True),
    CheckConstraint(_in("kind", ENTITY_KINDS), name="memory_entity_kind"),
    CheckConstraint(
        _paired("source_namespace", "external_source_key"),
        name="memory_entity_source_key_pairing",
    ),
    Index("memory_entity_kind_name", "kind", "normalized_name", "id"),
    Index(
        "memory_entity_source_key",
        "source_namespace",
        "external_source_key",
        unique=True,
        postgresql_where=text("source_namespace IS NOT NULL"),
    ),
)

memory_mention = Table(
    "memory_mention",
    memory_metadata,
    Column(
        "memory_id",
        UUID(as_uuid=True),
        ForeignKey("recallatron.memory.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "entity_id",
        UUID(as_uuid=True),
        ForeignKey("recallatron.memory_entity.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("role", Text, nullable=True),
    PrimaryKeyConstraint("memory_id", "entity_id", name="memory_mention_pkey"),
    CheckConstraint(
        f"role IS NULL OR char_length(role) BETWEEN 1 AND {ROLE_MAX_LENGTH}",
        name="memory_mention_role_length",
    ),
    Index("memory_mention_entity_memory", "entity_id", "memory_id"),
)

# **No cross-module foreign key.** ``ref`` is a canonical reference string, and a
# reference is a value rather than a join (``rheo_contracts.refs``): the row it names
# may live in another module's schema, or in no schema at all once its record is
# deleted. ``supersession_lineage`` marks the rows that retain *ancestry* after a
# predecessor's content is gone, so it is allowed only where an ancestry hop can be —
# a ``derived_from`` relation naming a memory.
memory_link = Table(
    "memory_link",
    memory_metadata,
    Column(
        "memory_id",
        UUID(as_uuid=True),
        ForeignKey("recallatron.memory.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ref", Text, nullable=False),
    Column("relation", Text, nullable=False),
    Column("created_at", _timestamptz(), nullable=False),
    Column(
        "supersession_lineage",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
    PrimaryKeyConstraint("memory_id", "ref", "relation", name="memory_link_pkey"),
    CheckConstraint(_in("relation", LINK_RELATIONS), name="memory_link_relation"),
    CheckConstraint(
        "NOT supersession_lineage OR (relation = 'derived_from' AND ref LIKE "
        f"'{MEMORY_REF_PREFIX}%')",
        name="memory_link_lineage_derived_memory",
    ),
    Index("memory_link_ref_relation_memory", "ref", "relation", "memory_id"),
)

# **The composite primary key is the only index this table gets, and that is the
# claim.** No HNSW, no IVFFlat, no expression index, no per-model registry table and no
# persisted dimension authority: dense retrieval is run 1a2's, and 1a1 builds none of
# the machinery that would decide it early. ``tests/postgres/test_memory_records.py``
# proves the absence against ``pg_catalog`` rather than against this comment.
memory_embedding = Table(
    "memory_embedding",
    memory_metadata,
    Column(
        "memory_id",
        UUID(as_uuid=True),
        ForeignKey("recallatron.memory.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("model_id", Text, nullable=False),
    Column("dimensions", Integer, nullable=False),
    Column("vector", Vector, nullable=False),
    Column("embedded_at", _timestamptz(), nullable=False),
    PrimaryKeyConstraint("memory_id", "model_id", name="memory_embedding_pkey"),
)

# Internal and exportable: no operation reaches it and no MCP tool names it, but its
# rows travel in an export so a restored deployment cannot replay an accepted source
# unit into a second memory.
#
# **What is deliberately absent is the point of the table.** No source body, label,
# filename, transcript or session id, model prompt or output, raw payload, or prior
# value — only an opaque partitioned identity, a digest of the sanitized envelope, and
# the immutable authority context the unit was accepted under. The workspace is the
# routed database, so there is no workspace column either.
#
# Which field combinations each ``state`` and ``producer_kind`` requires is § A5's
# contract and Prompt 7's code, not DDL: the columns below carry the closed
# vocabularies and the pairings § A3 states, and nothing further.
source_receipt = Table(
    "source_receipt",
    memory_metadata,
    Column("representation_type", Text, nullable=False),
    Column("source_namespace", Text, nullable=False),
    Column("external_source_key", Text, nullable=False),
    Column("record_id", UUID(as_uuid=True), nullable=True),
    Column("payload_digest", LargeBinary, nullable=False),
    Column("state", Text, nullable=False),
    Column("producer_kind", Text, nullable=False),
    Column("authority_id", UUID(as_uuid=True), nullable=False),
    Column("principal_account_id", UUID(as_uuid=True), nullable=True),
    Column("audience_kind", Text, nullable=False),
    Column("audience_id", UUID(as_uuid=True), nullable=True),
    Column("bound_purpose", Text, nullable=True),
    Column("source_recorded_at", _timestamptz(), nullable=True),
    Column("source_expires_at", _timestamptz(), nullable=True),
    PrimaryKeyConstraint(
        "representation_type",
        "source_namespace",
        "external_source_key",
        name="source_receipt_pkey",
    ),
    CheckConstraint(
        _in("representation_type", REPRESENTATION_TYPES),
        name="source_receipt_representation_type",
    ),
    CheckConstraint(_in("state", RECEIPT_STATES), name="source_receipt_state"),
    CheckConstraint(
        _in("producer_kind", PRODUCER_KINDS), name="source_receipt_producer_kind"
    ),
    CheckConstraint(
        _in("audience_kind", AUDIENCE_KINDS), name="source_receipt_audience_kind"
    ),
    CheckConstraint(
        "(audience_kind = 'workspace') = (audience_id IS NULL)",
        name="source_receipt_audience_pairing",
    ),
    CheckConstraint(
        f"bound_purpose IS NULL OR {_in('bound_purpose', PURPOSES)}",
        name="source_receipt_bound_purpose",
    ),
    CheckConstraint(
        _paired("source_recorded_at", "source_expires_at"),
        name="source_receipt_source_window_pairing",
    ),
    CheckConstraint(
        f"octet_length(payload_digest) = {DIGEST_BYTES}",
        name="source_receipt_digest_length",
    ),
)
