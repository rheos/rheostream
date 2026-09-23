"""AC 2's schema half, and AC 5/6/8's read half over the schema it creates.

The first three tests are the census: applying the chain creates exactly the ratified
objects and nothing else. Everything below ``the read surface`` is the behaviour half —
the one eligibility function, first-cut lexical recall, and the centered read window
with its five-step precedence. They share a file because they share a subject; the
census answers "is the schema what § A3 ratified" and the rest answers "does anything
leave it that should not".

**AC 2's schema half: the migration creates the ratified schema, and only it.**

Two claims, both read off ``pg_catalog`` rather than off the Python model, because the
model is the thing under test — a census that enumerated
``rheo_recallatron.storage.tables`` and then compared it with the database would agree
with itself no matter what either one said:

1. Applying Recallatron's chain to a fresh workspace creates exactly the seven tables
   Architecture § A3 names, their declared indexes and constraints, and the chain's own
   version table — all inside the ``recallatron`` schema, nothing outside it.
2. ``memory_embedding`` carries no index beyond its composite primary key. No HNSW, no
   IVFFlat, no expression index: dense retrieval is run 1a2's, and 1a1 must not have
   decided it early by leaving an index behind.

**The census technique is copied from
``tests/postgres/test_module_storage_ownership.py`` rather than imported.** That file's
``_objects`` and its fixture are private to it, and
the two files make different claims — it owns "nothing leaves the schema" and this one
owns "these objects and no others". A shared helper would couple the contract inventory
here to that file's positive controls.

The expected set is written out as names rather than derived, deliberately. It is the
contract: an object appearing here that § A3 does not name should cost a reviewer a
line in this file, and an object § A3 names that the migration forgets should red.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import (
    LoadedSurfaces,
    create_required_extensions,
    install_and_enable_module,
    loaded_probe_modules,
)
from harness.registry import add_member
from pydantic import BaseModel, ConfigDict
from rheo_contracts import (
    ContextPurpose,
    Idempotency,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_contracts.source_units import (
    AuthorityGrant,
    AuthorityRefused,
    SanitizedEvidence,
    SourceLink,
    SourceMention,
    TrustedSourceUnit,
)
from rheo_core.audit import CORE_AUDIT_SINK, install_sink, reset_sinks
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.factories import context_from_operation
from rheo_core.events import ConsumerRegistry
from rheo_core.migrations.module_chain import run_module_chain
from rheo_core.operations import (
    CONSUMERS_MISSING,
    CORE_MODULE_ID,
    HARNESS_MODULE_ID,
    dispatch,
    register_core_operations,
)
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import OperationRegistry
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import (
    RecordHead,
    Unavailable,
    register_resolver,
    resolve_in,
)
from rheo_core.settings import ValueType
from rheo_core.storage import work_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.provisioning import core_version
from rheo_core.storage.repositories import (
    insert_module_state,
    upsert_workspace_setting,
)
from rheo_recallatron import MANIFEST
from rheo_recallatron import eligibility as memory_eligibility
from rheo_recallatron.configuration import (
    CANDIDATE_SCAN_LIMIT,
    MEMORY_RECORD_TYPE,
    RETENTION_DAYS_KEY,
    RETENTION_EXPIRE_BY_AGE_KEY,
    RETRIEVAL_STRATEGY_KEY,
    STRATEGY_DENSE,
    STRATEGY_LEXICAL,
    RetentionPolicy,
)
from rheo_recallatron.contracts import (
    ArmCounts,
    EntityItem,
    EntityList,
    MemoryWritten,
    RecallProvenance,
)
from rheo_recallatron.eligibility import (
    Denied,
    MemoryRequest,
    ReadMode,
    ReferenceBudget,
    begin_request,
    contact_permitted,
    effective_retention,
    eligible_memory,
    expire_by_age_enabled,
    memory_reference,
    row_local_conditions,
)
from rheo_recallatron.entities import ENTITY_GET, ENTITY_LIST, remove_mention
from rheo_recallatron.events import MEMORY_INVALIDATED, MEMORY_RECORDED
from rheo_recallatron.operations import (
    MEMORY_DERIVE,
    MEMORY_READ,
    MEMORY_RECALL,
    MEMORY_REMEMBER,
    ReadWindow,
    RecallResult,
)
from rheo_recallatron.references import entity_reference
from rheo_recallatron.resolvers import resolve_memory
from rheo_recallatron.retrieval import (
    STRATEGY_REGISTRY,
    ArmProvenance,
    Hit,
    IndexItem,
    SearchRequest,
    SearchResult,
)
from rheo_recallatron.source_units import (
    LIVE_REPRESENTATION,
    AcceptOutcome,
    accept_source_unit,
    retire_source_unit,
)
from rheo_recallatron.storage import tables as memory_tables
from rheo_recallatron.storage.repository import (
    MemoryLinkRow,
    MemoryPurposeRow,
    MemoryRow,
    insert_memory,
    insert_memory_link,
    insert_memory_purpose,
    list_memory_purposes,
    set_source_receipt_state,
)
from sqlalchemy import (
    Connection,
    Engine,
    Row,
    func,
    insert,
    literal_column,
    select,
    text,
)

pytestmark = pytest.mark.postgres

_OWNED_SCHEMA = MANIFEST.storage.schema_name
_VERSION_TABLE = f"alembic_version_{_OWNED_SCHEMA}"

_TABLES = (
    "memory",
    "memory_purpose",
    "memory_entity",
    "memory_mention",
    "memory_link",
    "memory_embedding",
    "source_receipt",
    _VERSION_TABLE,
)
"""§ A3's six-table memory spine, the ``source_receipt`` seam, and the chain's own
version table. No session table, no conversation library, no hook-configuration table,
no per-model embedding registry, no dimension-authority table — AC 2's absence claims,
as an inventory a stray ``CREATE TABLE`` reds rather than as prose."""

_INDEXES = (
    "memory_pkey",
    "memory_search_tsv_gin",
    "memory_title_trgm",
    "memory_recorded_at_id",
    "memory_audience",
    "memory_source_key",
    "memory_purpose_pkey",
    "memory_purpose_purpose_memory",
    "memory_entity_pkey",
    "memory_entity_kind_name",
    "memory_entity_source_key",
    "memory_mention_pkey",
    "memory_mention_entity_memory",
    "memory_link_pkey",
    "memory_link_ref_relation_memory",
    "memory_embedding_pkey",
    "source_receipt_pkey",
    f"{_VERSION_TABLE}_pkc",
)
"""Every index, primary keys included. § A3's closing paragraph names the non-key ones;
``memory_embedding_pkey`` is the only entry for that table, which is
:func:`test_memory_embedding_has_no_index_beyond_its_primary_key`'s claim read a second
way, from the whole-schema side."""

_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("memory", "memory_pkey"),
    ("memory", "memory_superseded_by_id_fkey"),
    ("memory", "memory_kind"),
    ("memory", "memory_audience_kind"),
    ("memory", "memory_audience_pairing"),
    ("memory", "memory_confidence_range"),
    ("memory", "memory_origin"),
    ("memory", "memory_revision_positive"),
    ("memory", "memory_invalidation_pairing"),
    ("memory", "memory_invalidation_reason"),
    ("memory", "memory_source_key_pairing"),
    ("memory_purpose", "memory_purpose_pkey"),
    ("memory_purpose", "memory_purpose_memory_id_fkey"),
    ("memory_purpose", "memory_purpose_purpose"),
    ("memory_entity", "memory_entity_pkey"),
    ("memory_entity", "memory_entity_kind"),
    ("memory_entity", "memory_entity_source_key_pairing"),
    ("memory_mention", "memory_mention_pkey"),
    ("memory_mention", "memory_mention_memory_id_fkey"),
    ("memory_mention", "memory_mention_entity_id_fkey"),
    ("memory_mention", "memory_mention_role_length"),
    ("memory_link", "memory_link_pkey"),
    ("memory_link", "memory_link_memory_id_fkey"),
    ("memory_link", "memory_link_relation"),
    ("memory_link", "memory_link_lineage_derived_memory"),
    ("memory_embedding", "memory_embedding_pkey"),
    ("memory_embedding", "memory_embedding_memory_id_fkey"),
    ("source_receipt", "source_receipt_pkey"),
    ("source_receipt", "source_receipt_representation_type"),
    ("source_receipt", "source_receipt_state"),
    ("source_receipt", "source_receipt_producer_kind"),
    ("source_receipt", "source_receipt_audience_kind"),
    ("source_receipt", "source_receipt_audience_pairing"),
    ("source_receipt", "source_receipt_bound_purpose"),
    ("source_receipt", "source_receipt_source_window_pairing"),
    ("source_receipt", "source_receipt_digest_length"),
    (_VERSION_TABLE, f"{_VERSION_TABLE}_pkc"),
)
"""Every primary key, foreign key and check, by the table that owns it.

The closed vocabularies (``kind``, ``origin``, ``purpose``, entity ``kind``, link
``relation``, receipt ``state``/``producer_kind``/``representation_type``), the
pairings § A3 words as "null exactly for" or "nullable paired", the confidence and
revision ranges, the role bound and the 32-byte digest — each one is a row the database
refuses rather than a rule only the service remembers. ``memory_kind`` is also AC 14's
database half: ``topic_thread`` and ``procedural_notes`` are not values it admits.

The four association tables contribute only their composite primary keys and their
foreign keys, which is AC 2's "no synthetic identifiers" read from the catalog."""

_EXPECTED_OBJECTS: frozenset[tuple[str, str, str]] = frozenset(
    {(_OWNED_SCHEMA, _OWNED_SCHEMA, "schema")}
    | {(_OWNED_SCHEMA, name, "r") for name in _TABLES}
    | {(_OWNED_SCHEMA, name, "i") for name in _INDEXES}
    | {
        (
            _OWNED_SCHEMA,
            f"{constraint} on {_OWNED_SCHEMA}.{table}",
            "table constraint",
        )
        for table, constraint in _CONSTRAINTS
    }
)


@pytest.fixture
def recallatron_loaded(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Recallatron loaded through the harness's one recipe, then gone."""
    with loaded_probe_modules(monkeypatch, MANIFEST.module_id):
        yield


def _workspace_engine(cluster: ClusterSession, workspace: UUID) -> tuple[str, Engine]:
    row = cluster.registry_row(workspace)
    return row.database_name, cluster.backend.pools.engine_for(row.database_name)


def _objects(connection: Connection) -> set[tuple[str, str, str]]:
    """Every non-system object in the database, as ``(schema, name, kind)``.

    The technique is ``tests/postgres/test_module_storage_ownership.py``'s: relations
    from ``pg_class``, everything else through ``pg_identify_object`` over
    ``pg_depend``, schemas from ``pg_namespace``, with table-owned trigger/policy/rules
    attributed
    to their relation's namespace (``pg_identify_object`` reports NULL for those) and
    internal triggers, view ``_RETURN`` rules and automatic row/array types excluded so
    nothing is counted twice.
    """
    rows = connection.execute(
        text(
            "WITH table_owned (classid, objid, relid, is_internal) AS ("
            "SELECT 'pg_catalog.pg_trigger'::regclass, oid, tgrelid, tgisinternal "
            "FROM pg_catalog.pg_trigger "
            "UNION ALL "
            "SELECT 'pg_catalog.pg_policy'::regclass, oid, polrelid, false "
            "FROM pg_catalog.pg_policy "
            "UNION ALL "
            "SELECT 'pg_catalog.pg_rewrite'::regclass, rule.oid, rule.ev_class, "
            "EXISTS (SELECT 1 FROM pg_catalog.pg_depend AS dependency "
            "WHERE dependency.classid = 'pg_catalog.pg_rewrite'::regclass "
            "AND dependency.objid = rule.oid AND dependency.deptype = 'i') "
            "FROM pg_catalog.pg_rewrite AS rule) "
            "SELECT namespace.nspname::text, object.relname::text, "
            "object.relkind::text "
            "FROM pg_catalog.pg_class AS object "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = object.relnamespace "
            "WHERE namespace.nspname <> 'information_schema' "
            "AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
            "AND object.relkind IN ('r', 'p', 'i', 'I', 'S', 't', 'v', 'm', 'f', 'c') "
            "UNION "
            "SELECT COALESCE(identified.schema, owner_namespace.nspname), "
            "identified.identity, identified.type "
            "FROM (SELECT DISTINCT classid, objid FROM pg_catalog.pg_depend "
            "      WHERE objsubid = 0 AND classid <> 'pg_catalog.pg_class'::regclass) "
            "AS dependent "
            "CROSS JOIN LATERAL pg_catalog.pg_identify_object("
            "dependent.classid, dependent.objid, 0) AS identified "
            "LEFT JOIN table_owned ON table_owned.classid = dependent.classid "
            "AND table_owned.objid = dependent.objid "
            "LEFT JOIN pg_catalog.pg_class AS owner_relation "
            "ON owner_relation.oid = table_owned.relid "
            "LEFT JOIN pg_catalog.pg_namespace AS owner_namespace "
            "ON owner_namespace.oid = owner_relation.relnamespace "
            "WHERE COALESCE(identified.schema, owner_namespace.nspname) "
            "<> 'information_schema' "
            "AND COALESCE(identified.schema, owner_namespace.nspname) "
            "NOT LIKE 'pg\\_%' ESCAPE '\\' "
            "AND NOT COALESCE(table_owned.is_internal, false) "
            "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_type AS type "
            "WHERE dependent.classid = 'pg_catalog.pg_type'::regclass "
            "AND dependent.objid = type.oid AND (type.typrelid <> 0 OR EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_type AS element "
            "WHERE element.typarray = type.oid))) "
            "UNION "
            "SELECT nspname, nspname, 'schema' FROM pg_catalog.pg_namespace "
            "WHERE nspname <> 'information_schema' "
            "AND nspname NOT LIKE 'pg\\_%' ESCAPE '\\'"
        )
    )
    return {
        (str(schema), str(name), str(object_kind)) for schema, name, object_kind in rows
    }


def _outside_owned_schema(
    objects: set[tuple[str, str, str]],
) -> set[tuple[str, str, str]]:
    return {object_ for object_ in objects if object_[0] != _OWNED_SCHEMA}


def _migrate(cluster: ClusterSession, workspace: UUID) -> set[tuple[str, str, str]]:
    """Install the declared extensions, then run the chain; return what it created.

    The extensions go in first and **outside** the measured window, exactly where
    ``core.module.install`` step 6 puts them: they are the core's statement on the
    core's connection, and the types ``CREATE EXTENSION vector`` puts in ``public`` are
    not this module's migration leaking. A ``CREATE EXTENSION`` inside the migration
    would land inside the window and red the assertion below, which is the behaviour
    wanted.
    """
    database_name, engine = _workspace_engine(cluster, workspace)
    with engine.begin() as connection:
        create_required_extensions(connection, MANIFEST)
    with engine.begin() as connection:
        before = _objects(connection)
        run_module_chain(
            connection,
            MANIFEST,
            expected_database=database_name,
            core_version=core_version(),
        )
        return _objects(connection) - before


def test_the_migration_creates_exactly_the_ratified_schema(
    cluster: ClusterSession, workspace: UUID, recallatron_loaded: None
) -> None:
    created = _migrate(cluster, workspace)

    assert created == _EXPECTED_OBJECTS
    assert not _outside_owned_schema(created)


def test_the_migration_is_a_no_op_when_re_run_within_its_own_chain(
    cluster: ClusterSession, workspace: UUID, recallatron_loaded: None
) -> None:
    """Idempotent under re-run, which is what an install retried after a crash does.

    ``create_all(checkfirst=False)`` would raise on a second pass; nothing re-runs
    because Alembic's version table records the head, and the second ``run_chain``
    upgrades from head to head. The catalog is what proves it: not one object more.
    """
    created = _migrate(cluster, workspace)
    assert created == _EXPECTED_OBJECTS

    database_name, engine = _workspace_engine(cluster, workspace)
    with engine.begin() as connection:
        before = _objects(connection)
        run_module_chain(
            connection,
            MANIFEST,
            expected_database=database_name,
            core_version=core_version(),
        )
        assert _objects(connection) == before


def test_memory_embedding_has_no_index_beyond_its_primary_key(
    cluster: ClusterSession, workspace: UUID, recallatron_loaded: None
) -> None:
    """AC 2: no HNSW, no IVFFlat, no expression index, in 1a1.

    Asked of ``pg_catalog`` rather than of the ``Table`` object, because a dense index
    can arrive from raw DDL in a migration that the Python model never mentions — which
    is precisely the shape this claim has to exclude. ``indisprimary`` is read, so the
    one surviving index has to be the primary key rather than merely the only one.
    """
    _migrate(cluster, workspace)

    _, engine = _workspace_engine(cluster, workspace)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT index_class.relname::text, index_def.indisprimary, "
                "access_method.amname::text "
                "FROM pg_catalog.pg_index AS index_def "
                "JOIN pg_catalog.pg_class AS index_class "
                "ON index_class.oid = index_def.indexrelid "
                "JOIN pg_catalog.pg_class AS table_class "
                "ON table_class.oid = index_def.indrelid "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = table_class.relnamespace "
                "JOIN pg_catalog.pg_am AS access_method "
                "ON access_method.oid = index_class.relam "
                "WHERE namespace.nspname = :schema "
                "AND table_class.relname = 'memory_embedding'"
            ),
            {"schema": _OWNED_SCHEMA},
        ).all()

    assert [
        (str(name), bool(primary), str(method)) for name, primary, method in rows
    ] == [("memory_embedding_pkey", True, "btree")]


# --- the read surface -----------------------------------------------------------------
#
# Every test below drives the real operations through ``dispatch`` against a real
# installed-and-enabled Recallatron, with rows written straight through the module's own
# repository: there is no writer yet, and seeding through the repository is what § A3
# says a synthetic fixture does. The refusal assertions read ``outcome.state``, which is
# the refusal code the dispatcher folds an ``OperationRefused`` into.

_RESPOND = "respond"
_INTERNAL = "internal_analysis"
_MEMORY_MODULE = MANIFEST.module_id


def _recent(offset_seconds: int = 0) -> datetime:
    """A ``recorded_at`` comfortably inside any retention window, ordered by offset.

    Relative to the wall clock rather than a literal instant: retention is compared
    against ``now``, so a fixture pinned to a date would start failing on whichever day
    it fell out of the default window.
    """
    return datetime.now(UTC) - timedelta(days=1) + timedelta(seconds=offset_seconds)


def _long_ago() -> datetime:
    """Older than the 365-day default, so every read excludes it as expired."""
    return datetime.now(UTC) - timedelta(days=400)


def _row(
    *,
    title: str = "a memory",
    body: str = "the body of a memory about apples",
    recorded_at: datetime | None = None,
    audience_kind: str = "workspace",
    audience_id: UUID | None = None,
    kind: str = "note",
    invalidation_reason: str | None = None,
    superseded_by_id: UUID | None = None,
) -> MemoryRow:
    recorded = _recent() if recorded_at is None else recorded_at
    return MemoryRow(
        id=uuid7(),
        kind=kind,
        title=title,
        body=body,
        audience_kind=audience_kind,
        audience_id=audience_id,
        confidence=None,
        occurred_at=None,
        recorded_at=recorded,
        recorded_by_kind="account",
        recorded_by_id=None,
        origin="told",
        revision=1,
        corrected_at=None,
        superseded_by_id=superseded_by_id,
        invalidated_at=None if invalidation_reason is None else recorded,
        invalidation_reason=invalidation_reason,
        source_namespace=None,
        external_source_key=None,
    )


Link = tuple[str, str, bool]
"""``(ref, relation, supersession_lineage)``, as the fixtures spell a link."""


def _write(
    conn: Connection,
    row: MemoryRow,
    *,
    purposes: Sequence[str] = (_RESPOND,),
    links: Sequence[Link] = (),
) -> MemoryRow:
    insert_memory(conn, row)
    for purpose in purposes:
        insert_memory_purpose(conn, MemoryPurposeRow(memory_id=row.id, purpose=purpose))
    for ref, relation, lineage in links:
        insert_memory_link(
            conn,
            MemoryLinkRow(
                memory_id=row.id,
                ref=ref,
                relation=relation,
                created_at=row.recorded_at,
                supersession_lineage=lineage,
            ),
        )
    return row


def _write_many(
    conn: Connection,
    rows: Sequence[MemoryRow],
    *,
    container: str,
    purposes: Sequence[str] = (_RESPOND,),
) -> tuple[MemoryRow, ...]:
    """Five hundred members in three round trips rather than fifteen hundred.

    The per-row writer above is the readable one and is what every small fixture uses;
    this one exists because the 500/501 boundary needs a container that large and a
    row-at-a-time loop makes that case slow enough to discourage writing it.
    """
    conn.execute(insert(memory_tables.memory), [asdict(row) for row in rows])
    conn.execute(
        insert(memory_tables.memory_purpose),
        [{"memory_id": row.id, "purpose": p} for row in rows for p in purposes],
    )
    conn.execute(
        insert(memory_tables.memory_link),
        [
            {
                "memory_id": row.id,
                "ref": container,
                "relation": "derived_from",
                "created_at": row.recorded_at,
                "supersession_lineage": False,
            }
            for row in rows
        ],
    )
    return tuple(rows)


@dataclass(frozen=True)
class MemoryWorkspace:
    """A workspace with Recallatron installed, enabled and migrated."""

    cluster: ClusterSession
    workspace: UUID
    owner_account_id: UUID
    surfaces: LoadedSurfaces
    database_name: str
    engine: Engine
    consumers: ConsumerRegistry

    def context(
        self, *, account_id: UUID | None = None, role: Role = Role.OWNER
    ) -> WorkspaceContext:
        ctx = context_for_harness(
            self.workspace,
            self.owner_account_id if account_id is None else account_id,
            role,
        )
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    def bound_context(
        self, purpose: str, *, account_id: UUID | None = None
    ) -> WorkspaceContext:
        """A context whose principal carries a verified purpose binding.

        Rebuilt from stored provenance, which is the one factory a test can drive to a
        bound principal without minting a purpose-carrying token first. The binding it
        produces is the same ``ctx.principal.bound_purpose`` a runtime job carries.
        """
        account = self.owner_account_id if account_id is None else account_id
        ctx = context_from_operation(
            self.workspace,
            actor_kind="account",
            actor_id=account,
            audience_kind="session",
            audience_id=account,
            entry="api",
            purpose=purpose,
        )
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    @contextmanager
    def unit(self) -> Iterator[UnitOfWork]:
        with UnitOfWork(self.engine, self.database_name) as uow:
            yield uow
            uow.commit()

    @contextmanager
    def reading(self) -> Iterator[UnitOfWork]:
        with UnitOfWork(self.engine, self.database_name) as uow:
            yield uow

    def call(
        self, ctx: WorkspaceContext, name: str, payload: dict[str, object]
    ) -> OperationOutcome:
        """One dispatch, carrying the registry the loader built **and** the one
        ``ConsumerRegistry`` this workspace's composition root would have built.

        Both writes publish, and a publishing handler refuses ``consumers_missing``
        without a registry — so a helper that dropped it would make every write test
        a test of the missing-wiring refusal.
        """
        return dispatch(
            ctx,
            name,
            payload,
            registry=self.surfaces.operations,
            consumers=self.consumers,
        )

    def unwired(
        self, ctx: WorkspaceContext, name: str, payload: dict[str, object]
    ) -> OperationOutcome:
        """The same dispatch with no registry wired: what a deployment bug looks
        like."""
        return dispatch(ctx, name, payload, registry=self.surfaces.operations)

    def remember(self, ctx: WorkspaceContext, **payload: object) -> OperationOutcome:
        return self.call(ctx, MEMORY_REMEMBER, payload)

    def derive(self, ctx: WorkspaceContext, **payload: object) -> OperationOutcome:
        return self.call(ctx, MEMORY_DERIVE, payload)

    def counts(self) -> dict[str, int]:
        """Every row this module and the outbox hold, read straight from the tables.

        Direct reads rather than through an operation: what these assertions are
        about is whether anything was **written**, and a read path that correctly
        excludes a row it should not show would report zero for a row that is there.

        ``core.audit_record`` is deliberately **not** counted here. A refused mutate
        writes a ``refused`` audit row on purpose — that is the whole point of the
        audit path — so a refusal assertion built on this mapping would have to
        expect the log to grow, which reads as though the refusal wrote something it
        should not have. Audit rows are asserted by name through
        :meth:`audit_rows` where they matter.
        """
        tables = {
            "memory": memory_tables.memory,
            "purpose": memory_tables.memory_purpose,
            "entity": memory_tables.memory_entity,
            "mention": memory_tables.memory_mention,
            "link": memory_tables.memory_link,
            "receipt": memory_tables.source_receipt,
            "outbox": work_tables.outbox_event,
        }
        with self.reading() as uow:
            return {
                name: int(
                    uow.connection.execute(
                        select(func.count()).select_from(table)
                    ).scalar_one()
                )
                for name, table in tables.items()
            }

    def outbox(self) -> list[Row[Any]]:
        with self.reading() as uow:
            return list(
                uow.connection.execute(
                    select(work_tables.outbox_event).order_by(
                        work_tables.outbox_event.c.position
                    )
                )
            )

    def audit_rows(self, operation: str) -> list[Row[Any]]:
        with self.reading() as uow:
            return list(
                uow.connection.execute(
                    select(work_tables.audit_record).where(
                        work_tables.audit_record.c.operation_name == operation
                    )
                )
            )

    def read(self, ctx: WorkspaceContext, **payload: object) -> OperationOutcome:
        return self.call(ctx, MEMORY_READ, payload)

    def recall(self, ctx: WorkspaceContext, **payload: object) -> OperationOutcome:
        return self.call(ctx, MEMORY_RECALL, payload)

    def set_retention(self, value: str | None) -> None:
        """Rewrite or delete the stored retention row, around the settings write path.

        Deliberately not through the settings operation: that path validates, so it
        cannot produce the corrupt row AC 8 is about. What a reader must survive is a
        row that got there some other way — a bad restore, a hand-edited database, a
        future writer with a bug.
        """
        with self.unit() as uow:
            if value is None:
                uow.connection.execute(
                    text("DELETE FROM " + _core_setting_table() + " WHERE key = :key"),
                    {"key": RETENTION_DAYS_KEY},
                )
            else:
                upsert_workspace_setting(
                    uow.connection,
                    key=RETENTION_DAYS_KEY,
                    value=value,
                    value_type=ValueType.INT,
                    updated_by=None,
                )

    def stored_setting(self, key: str) -> str | None:
        """The raw stored value of one workspace setting, or ``None`` for no row."""
        with self.reading() as uow:
            statement = text(
                "SELECT value FROM " + _core_setting_table() + " WHERE key = :key"
            )
            return uow.connection.execute(statement, {"key": key}).scalar_one_or_none()

    def expire_by_age(self, value: str | None = "true") -> None:
        """Turn § A9's gate on — **nothing in this file ages out until it is called.**

        Recallatron never auto-forgets a memory by age, so the fixture leaves the gate
        where ``core.module.enable`` writes it, at ``false``. Every case below that
        expects an age exclusion, or a ``retention_unavailable`` for a broken window,
        says so here first: with the gate off there is no window, so the window row is
        never read and nothing can refuse for it.

        ``value`` takes the same escape hatch as :meth:`set_retention` — ``None``
        deletes the row, a string stores it verbatim — so the pre-correction workspace
        shape (no gate row at all) and an unparseable one are both reachable.
        """
        with self.unit() as uow:
            if value is None:
                uow.connection.execute(
                    text("DELETE FROM " + _core_setting_table() + " WHERE key = :key"),
                    {"key": RETENTION_EXPIRE_BY_AGE_KEY},
                )
            else:
                upsert_workspace_setting(
                    uow.connection,
                    key=RETENTION_EXPIRE_BY_AGE_KEY,
                    value=value,
                    value_type=ValueType.BOOL,
                    updated_by=None,
                )

    def set_strategy(self, value: str | None) -> None:
        """Store or delete this workspace's retrieval-strategy row, verbatim.

        The same escape hatch as :meth:`set_retention`: around the validating settings
        operation, so an absent row and an unparseable one are both reachable.
        """
        with self.unit() as uow:
            if value is None:
                uow.connection.execute(
                    text("DELETE FROM " + _core_setting_table() + " WHERE key = :key"),
                    {"key": RETRIEVAL_STRATEGY_KEY},
                )
            else:
                upsert_workspace_setting(
                    uow.connection,
                    key=RETRIEVAL_STRATEGY_KEY,
                    value=value,
                    value_type=ValueType.STR,
                    updated_by=None,
                )


def _core_setting_table() -> str:
    """The workspace-setting table's qualified name, assembled rather than written.

    Spelled in pieces so this file reads the same way the module's own source has to:
    the schema-ownership scan treats a ``<core-schema>.<name>`` literal as a foreign
    table reference, and while that scan does not walk ``tests/``, writing the literal
    here would make this file a template for the next one that does.
    """
    return "core" + ".workspace_setting"


@pytest.fixture
def memory(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[MemoryWorkspace]:
    register_core_operations()
    # The memory resolver goes on the **process-wide** table too, which is where
    # ``load_modules`` puts it in a real deployment. ``loaded_probe_modules`` loads
    # into local registries on purpose, and everything that dispatches or resolves by
    # hand passes them explicitly — but the two places a module resolves a reference
    # *itself* (an entity's backing ref, and a memory's non-memory link) call
    # ``resolve_in`` with its default, exactly as they will in production. Without
    # this the positive half of those paths is unreachable from a test and only their
    # denials could be exercised. Re-registering the same function object is a no-op,
    # so this is safe to run per test, and a workspace that has not enabled a module
    # still resolves nothing of it.
    register_resolver(
        _MEMORY_MODULE,
        MEMORY_RECORD_TYPE,
        resolve_memory,
        origin=_MEMORY_MODULE,
    )
    with loaded_probe_modules(monkeypatch, _MEMORY_MODULE) as surfaces:
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext), bootstrap
        install_and_enable_module(cluster.backend, bootstrap, workspace, _MEMORY_MODULE)
        database_name, engine = _workspace_engine(cluster, workspace)
        yield MemoryWorkspace(
            cluster=cluster,
            workspace=workspace,
            owner_account_id=owner_account_id,
            surfaces=surfaces,
            database_name=database_name,
            engine=engine,
            # One registry for the whole fixture, built where a composition root
            # would build it. Empty, which is the shipped state: no consumer
            # subscribes to a Recallatron event in 1a1, and a publish that reaches
            # none is a success with zero deliveries.
            consumers=ConsumerRegistry(),
        )


def _window(outcome: OperationOutcome) -> ReadWindow:
    assert outcome.ok, outcome
    assert isinstance(outcome.result, ReadWindow)
    return outcome.result


def _refused(outcome: OperationOutcome, state: str) -> None:
    """A refusal, and nothing else: no result, and the state in the error too."""
    assert outcome.state == state, outcome
    assert outcome.result is None, outcome
    assert outcome.error is not None and outcome.error.error_code == state, outcome


# --- eligibility ---------------------------------------------------------------------


def test_eligibility_decides_what_the_record_resolver_heads(
    memory: MemoryWorkspace,
) -> None:
    """Generic resolution is the same function, in ``current`` mode, with no head for
    anything it denies."""
    with memory.unit() as uow:
        live = _write(uow.connection, _row(title="visible"))
        private = _write(
            uow.connection,
            _row(audience_kind="member", audience_id=uuid7()),
        )

    owner = memory.context()
    operator = context_for_operator(memory.workspace)
    assert isinstance(operator, WorkspaceContext)
    with memory.reading() as uow:
        head = resolve_in(
            memory_reference(live.id), owner, uow, registry=memory.surfaces.resolvers
        )
        assert isinstance(head, RecordHead), head
        assert (head.display, head.readable, head.state, head.revision) == (
            "visible",
            True,
            "live",
            1,
        )
        for reference in (memory_reference(private.id), memory_reference(uuid7())):
            denied = resolve_in(
                reference, owner, uow, registry=memory.surfaces.resolvers
            )
            assert isinstance(denied, Unavailable), denied
            assert denied.reason == "not_found"
        # The role gate: an operator is not in § A10's read roles, and a resolver has
        # no declaration to refuse it, so the eligibility function is what does.
        by_operator = resolve_in(
            memory_reference(live.id),
            operator,
            uow,
            registry=memory.surfaces.resolvers,
        )
        assert isinstance(by_operator, Unavailable), by_operator
        assert by_operator.reason == "not_found"


def test_eligibility_denies_a_member_audience_row_for_another_account(
    memory: MemoryWorkspace, cluster: ClusterSession
) -> None:
    member_id = add_member(
        cluster.backend, memory.workspace, Role.MEMBER, display_name="member-two"
    )
    with memory.unit() as uow:
        private = _write(
            uow.connection,
            _row(title="theirs", audience_kind="member", audience_id=member_id),
        )

    with memory.reading() as uow:
        theirs = resolve_in(
            memory_reference(private.id),
            memory.context(),
            uow,
            registry=memory.surfaces.resolvers,
        )
        assert isinstance(theirs, Unavailable) and theirs.reason == "not_found"
        mine = resolve_in(
            memory_reference(private.id),
            memory.context(account_id=member_id, role=Role.MEMBER),
            uow,
            registry=memory.surfaces.resolvers,
        )
        assert isinstance(mine, RecordHead) and mine.display == "theirs"


def test_eligibility_denies_a_row_whose_ordinary_link_does_not_resolve(
    memory: MemoryWorkspace,
) -> None:
    """A linked record that is missing, deleted, unavailable or unreadable denies the
    row — including one owned by a module this workspace has not enabled."""
    foreign = f"harness.note:{uuid7()}"
    with memory.unit() as uow:
        blocked = _write(
            uow.connection, _row(title="blocked"), links=((foreign, "about", False),)
        )
        absent_source = _write(
            uow.connection,
            _row(title="orphaned"),
            links=((memory_reference(uuid7()), "derived_from", False),),
        )

    with memory.reading() as uow:
        for row in (blocked, absent_source):
            denied = resolve_in(
                memory_reference(row.id),
                memory.context(),
                uow,
                registry=memory.surfaces.resolvers,
            )
            assert isinstance(denied, Unavailable), (row.title, denied)
            assert denied.reason == "not_found"


def test_eligibility_admits_a_lineage_link_without_resolving_the_predecessor(
    memory: MemoryWorkspace,
) -> None:
    """Marked ancestry is content-free provenance: it neither resolves nor requires a
    live predecessor, and it never supplies one's head.

    The control is the same row with the marker off, which denies — so the test is
    about the marker rather than about the reference happening to be tolerated.
    """
    gone = memory_reference(uuid7())
    with memory.unit() as uow:
        container = _write(uow.connection, _row(title="container"))
        heir = _write(
            uow.connection,
            _row(title="the replacement"),
            links=(
                (gone, "derived_from", True),
                (memory_reference(container.id), "derived_from", False),
            ),
        )
        control = _write(
            uow.connection,
            _row(title="control"),
            links=((gone, "derived_from", False),),
        )

    owner = memory.context()
    with memory.reading() as uow:
        assert isinstance(
            resolve_in(
                memory_reference(heir.id),
                owner,
                uow,
                registry=memory.surfaces.resolvers,
            ),
            RecordHead,
        )
        denied = resolve_in(
            memory_reference(control.id),
            owner,
            uow,
            registry=memory.surfaces.resolvers,
        )
        assert isinstance(denied, Unavailable) and denied.reason == "not_found"

    window = _window(
        memory.read(
            owner,
            container_ref=memory_reference(container.id),
            target_ref=memory_reference(heir.id),
        )
    )
    lineage = [link for link in window.items[0].links if link.supersession_lineage]
    assert [(link.ref, link.display) for link in lineage] == [(gone, None)]


def test_eligibility_applies_a_bound_purpose_gate(memory: MemoryWorkspace) -> None:
    """A bound context sees only what carries its purpose; an unbound one has no gate.

    AC 5's own sentence, driven end to end: a memory stored with exactly
    ``internal_analysis`` is absent from a read bound to any other purpose, present to
    an unbound authenticated browse, and present to a read bound to it.
    """
    with memory.unit() as uow:
        # The container carries both purposes: it is a linked source of every member,
        # so a container narrower than its members would deny them all — which is the
        # eligibility function working, not a purpose gate this test is about.
        container = _write(
            uow.connection, _row(title="container"), purposes=(_RESPOND, _INTERNAL)
        )
        reference = memory_reference(container.id)
        answered = _write(
            uow.connection,
            _row(title="answered"),
            purposes=(_RESPOND,),
            links=((reference, "derived_from", False),),
        )
        analysed = _write(
            uow.connection,
            _row(title="analysed", recorded_at=_recent(1)),
            purposes=(_INTERNAL,),
            links=((reference, "derived_from", False),),
        )

    unbound = _window(
        memory.read(
            memory.context(),
            container_ref=reference,
            target_ref=memory_reference(answered.id),
            context=10,
        )
    )
    assert {item.title for item in unbound.items} == {"answered", "analysed"}

    bound = _window(
        memory.read(
            memory.bound_context(_RESPOND),
            container_ref=reference,
            target_ref=memory_reference(answered.id),
            context=10,
        )
    )
    assert [item.title for item in bound.items] == ["answered"]
    assert bound.total == 1

    _refused(
        memory.read(
            memory.bound_context(_RESPOND),
            container_ref=reference,
            target_ref=memory_reference(analysed.id),
        ),
        "not_found",
    )
    internal = _window(
        memory.read(
            memory.bound_context(_INTERNAL),
            container_ref=reference,
            target_ref=memory_reference(analysed.id),
            context=10,
        )
    )
    assert [item.title for item in internal.items] == ["analysed"]


def test_eligibility_refuses_when_the_reference_budget_is_exhausted(
    memory: MemoryWorkspace,
) -> None:
    """Both halves of § A13's shared budget, driven against a deliberately tiny one.

    Four thousand and ninety-six references is not a number a fixture can reach in a
    test worth reading, so the budget is constructed small and the *rule* is what is
    under test: an overflow is ``reference_scan_limit``, never a partial answer.
    """
    with memory.unit() as uow:
        source = _write(uow.connection, _row(title="source"))
        derived = _write(
            uow.connection,
            _row(title="derived"),
            links=((memory_reference(source.id), "derived_from", False),),
        )

    owner = memory.context()
    with memory.reading() as uow:
        spent = MemoryRequest(
            owner,
            now=datetime.now(UTC),
            retention=RetentionPolicy.of_days(365),
            budget=ReferenceBudget(limit=1),
        )
        decision = eligible_memory(
            owner, uow, derived.id, mode=ReadMode.CURRENT, request=spent
        )
        assert decision == Denied("reference_scan_limit")

        shallow = MemoryRequest(
            owner,
            now=datetime.now(UTC),
            retention=RetentionPolicy.of_days(365),
            budget=ReferenceBudget(depth_limit=0),
        )
        assert eligible_memory(
            owner, uow, derived.id, mode=ReadMode.CURRENT, request=shallow
        ) == Denied("reference_scan_limit")

        roomy = MemoryRequest(
            owner, now=datetime.now(UTC), retention=RetentionPolicy.of_days(365)
        )
        assert not isinstance(
            eligible_memory(
                owner, uow, derived.id, mode=ReadMode.CURRENT, request=roomy
            ),
            Denied,
        )


class _PartyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    party_ref: str
    purpose: str
    channel: str | None = None


class _PartyAnswer(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: str


def _contact_registry(answer: str | None) -> OperationRegistry:
    """A registry holding one synthetic contact-permission implementation.

    ``answer`` is the state it returns, or ``None`` for one that raises — § A6's
    "error result suppresses content". Local, never the process registry: neither
    kind of answer belongs to the rest of the session.
    """

    def handler(
        ctx: WorkspaceContext, uow: object, model_input: _PartyCheck
    ) -> _PartyAnswer:
        if answer is None:
            raise RuntimeError("the contact service is unreachable")
        return _PartyAnswer(state=answer)

    registry = OperationRegistry()
    registry.register(
        OperationDeclaration(
            name="leads.contact_permission.check",
            safety_class=SafetyClass.READ,
            roles=frozenset({Role.OWNER, Role.MEMBER, Role.SERVICE}),
            input_model=_PartyCheck,
            output=_PartyAnswer,
            idempotency=Idempotency.NONE,
            audit=None,
        ),
        handler,
        origin="leads",
    )
    return registry


def test_eligibility_contact_seam_is_absent_permitted_or_denied(
    memory: MemoryWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§ A6's optional contact seam, in the three states 1a1 can put it in.

    Absent is the state this repository actually ships — there is no such module here
    and none is invented to answer the check. The other two are driven against a
    synthetic implementation registered into a local registry, because the deny half is
    security logic and "unreachable today" is not a reason to leave it unexercised.
    """
    party = RecordRef.parse(f"relationships.party:{uuid7()}")
    with memory.unit() as uow:
        insert_module_state(
            uow.connection,
            module_id="leads",
            package_version="0",
            state="enabled",
            installed_at=datetime.now(UTC),
            enabled_at=datetime.now(UTC),
        )

    bound = memory.bound_context(_RESPOND)
    assert "leads" in bound.enabled_modules
    with memory.reading() as uow:
        request = begin_request(bound, uow)
        assert request is not None

        # Absent: no operation is registered, so the check is not applicable.
        assert contact_permitted(bound, uow, party, "about", request=request)

        for answer, expected in (
            ("permitted", True),
            ("withdrawn", False),
            ("suppressed", False),
            (None, False),
        ):
            monkeypatch.setattr(
                memory_eligibility, "CONTACT_LOOKUP", _contact_registry(answer)
            )
            assert (
                contact_permitted(bound, uow, party, "about", request=request)
                is expected
            ), answer

        # An unbound caller has no purpose gate, so there is nothing to check.
        unbound_request = begin_request(memory.context(), uow)
        assert unbound_request is not None
        assert contact_permitted(
            memory.context(), uow, party, "about", request=unbound_request
        )


# --- recall -------------------------------------------------------------------------


def test_recall_returns_eligible_rows_marked_lexical(memory: MemoryWorkspace) -> None:
    with memory.unit() as uow:
        _write(uow.connection, _row(title="apples", body="a note about apples"))
        _write(
            uow.connection,
            _row(title="pears", body="a note about pears", recorded_at=_recent(1)),
        )
        _write(
            uow.connection,
            _row(
                title="expired apples",
                body="an old note about apples",
                recorded_at=_long_ago(),
            ),
        )
    # The ancient row is excluded because this workspace **asked** for an age bound.
    # Without this line it stays, and that is the ratified default — see
    # ``test_a_workspace_that_configures_nothing_keeps_its_oldest_memories``.
    memory.expire_by_age()

    outcome = memory.recall(memory.context(), query="apples")
    assert outcome.ok, outcome
    result = outcome.result
    assert result is not None
    items = result.items
    assert [item.title for item in items] == ["apples"]
    assert items[0].strategy == "lexical"
    assert items[0].score > 0
    assert items[0].body == "a note about apples"


def test_recall_excludes_a_row_whose_source_the_caller_cannot_read(
    memory: MemoryWorkspace,
) -> None:
    """Per-link checks run before any content leaves the service."""
    with memory.unit() as uow:
        _write(
            uow.connection,
            _row(title="apples one", body="apples, readable"),
        )
        _write(
            uow.connection,
            _row(title="apples two", body="apples, blocked", recorded_at=_recent(1)),
            links=((f"harness.note:{uuid7()}", "about", False),),
        )

    outcome = memory.recall(memory.context(), query="apples")
    assert outcome.ok, outcome
    result = outcome.result
    assert result is not None
    assert [item.title for item in result.items] == ["apples one"]


def test_recall_refuses_a_blank_or_oversized_query_as_input_invalid(
    memory: MemoryWorkspace,
) -> None:
    owner = memory.context()
    for payload in (
        {"query": "   "},
        {"query": ""},
        {"query": "a" * 1001},
        {"query": "apples", "k": 0},
        {"query": "apples", "k": 51},
        {"query": "apples", "unknown": True},
    ):
        _refused(memory.recall(owner, **payload), "input_invalid")


# --- the retrieval seam ---------------------------------------------------------------


def _recalled(outcome: OperationOutcome) -> RecallResult:
    assert outcome.ok, outcome
    assert isinstance(outcome.result, RecallResult), outcome
    return outcome.result


@dataclass
class _SpyStrategy:
    """A strategy whose answer no lexical ranking could produce, so seeing it come
    back through ``recall()`` proves the registry entry — not a constant — ran."""

    answer: SearchResult
    name: str = "spy"
    requests: list[SearchRequest] = dataclass_field(default_factory=list)

    def index(self, ctx: WorkspaceContext, uow: Any, item: IndexItem) -> None:
        raise AssertionError("recall must not index")

    def invalidate(self, ctx: WorkspaceContext, uow: Any, ref: str) -> None:
        raise AssertionError("recall must not invalidate")

    def search(
        self, ctx: WorkspaceContext, uow: Any, request: SearchRequest
    ) -> SearchResult:
        self.requests.append(request)
        return self.answer


def test_recall_runs_the_strategy_the_registry_resolves(
    memory: MemoryWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 1, first half: the resolved registry entry decides what runs.

    This phase's key offers only ``lexical``, so the proof swaps that entry for a spy
    rather than writing a setting. The spy answers ``apples`` with the ``pears`` row,
    which shares no lexeme with the query, and with arm counts lexical never reports.
    """
    with memory.unit() as uow:
        _write(uow.connection, _row(title="apples", body="a note about apples"))
        pears = _write(
            uow.connection,
            _row(title="pears", body="a note about pears", recorded_at=_recent(1)),
        )
    spy = _SpyStrategy(
        answer=SearchResult(
            hits=(Hit(ref=pears.id, score=0.5, strategy="spy"),),
            arms=ArmProvenance(lexical=7, dense=3),
            dense_available=True,
        )
    )
    monkeypatch.setitem(STRATEGY_REGISTRY, STRATEGY_LEXICAL, spy)

    result = _recalled(memory.recall(memory.context(), query="apples"))

    assert [request.query for request in spy.requests] == ["apples"]
    assert spy.requests[0].limit == CANDIDATE_SCAN_LIMIT
    assert [item.title for item in result.items] == ["pears"]
    assert [item.strategy for item in result.items] == ["spy"]
    assert result.items[0].score == 0.5
    assert result.provenance == RecallProvenance(
        strategy="spy",
        arms=ArmCounts(lexical=7, dense=3),
        dense_available=True,
    )


_INLINE_RECALL_CONFIG: Any = literal_column("'english'::regconfig")


def _inline_recall_refs(
    uow: UnitOfWork, request: MemoryRequest, query_text: str
) -> list[str]:
    """The statement ``recall()`` ran inline before the seam, held verbatim.

    Deliberately a copy, not an import: the parity claim is that the seam returns what
    *this* statement returned, and a baseline built from the code under test would
    agree with it whatever it did.
    """
    query = func.plainto_tsquery(_INLINE_RECALL_CONFIG, query_text)
    score = func.ts_rank_cd(memory_tables.memory.c.search_tsv, query)
    statement = (
        select(memory_tables.memory.c.id, score.label("score"))
        .where(
            memory_tables.memory.c.search_tsv.bool_op("@@")(query),
            *row_local_conditions(request, ReadMode.CURRENT),
        )
        .order_by(
            score.desc(),
            memory_tables.memory.c.recorded_at,
            memory_tables.memory.c.id,
        )
        .limit(CANDIDATE_SCAN_LIMIT)
    )
    return [
        memory_reference(candidate)
        for candidate, _ in uow.connection.execute(statement).all()
    ]


def test_lexical_recall_returns_what_the_inline_statement_returned(
    memory: MemoryWorkspace,
) -> None:
    """1A's zero-delta claim: same rows, same order, same label.

    Single-lexeme queries only, so the baseline stays exact once the query builder
    changes; the multi-word divergence is AC 23's to assert. Scores are not compared.
    The workspace pins ``lexical`` itself, so this stays a test of the lexical arm
    whatever the package default becomes.
    """
    other_account = uuid7()
    with memory.unit() as uow:
        # Different densities and ties, so the order is decided by score first and
        # then by ``recorded_at`` and ``id``.
        for offset, (title, body) in enumerate(
            (
                ("apples", "apples apples apples, and a body"),
                ("one apple", "a single apples mention"),
                ("tie a", "apples in the body"),
                ("tie b", "apples in the body"),
                ("pears", "a body about pears"),
                ("long", "apples " + "filler words " * 40 + "apples body"),
            )
        ):
            _write(
                uow.connection,
                _row(title=title, body=body, recorded_at=_recent(offset)),
            )
        # Excluded row-locally by both statements: another member's private memory
        # and an invalidated one.
        _write(
            uow.connection,
            _row(
                title="private apples",
                body="apples body",
                audience_kind="member",
                audience_id=other_account,
            ),
        )
        _write(
            uow.connection,
            _row(
                title="corrected apples",
                body="apples body",
                invalidation_reason="source_corrected",
            ),
        )
    memory.set_strategy(STRATEGY_LEXICAL)
    ctx = memory.context()

    for query_text in ("apples", "body"):
        with memory.reading() as uow:
            request = begin_request(ctx, uow)
            assert request is not None
            baseline = _inline_recall_refs(uow, request, query_text)
        assert len(baseline) >= 4, (query_text, baseline)

        result = _recalled(memory.recall(ctx, query=query_text, k=50))
        assert [item.ref for item in result.items] == baseline, query_text
        assert {item.strategy for item in result.items} == {STRATEGY_LEXICAL}
        assert all(item.score > 0 for item in result.items)


def test_every_recall_carries_lexical_provenance(memory: MemoryWorkspace) -> None:
    """AC 8's ``false`` half: provenance on every response, counting the ranked list
    before the permission walk, with no dense arm to report."""
    assert memory.stored_setting(RETRIEVAL_STRATEGY_KEY) == STRATEGY_LEXICAL
    with memory.unit() as uow:
        _write(uow.connection, _row(title="apples one", body="apples, readable"))
        _write(
            uow.connection,
            _row(title="apples two", body="apples, blocked", recorded_at=_recent(1)),
            links=((f"harness.note:{uuid7()}", "about", False),),
        )

    ranked = _recalled(memory.recall(memory.context(), query="apples"))
    assert [item.title for item in ranked.items] == ["apples one"]
    # Two ranked, one shown: the walk removed the other after the count was taken.
    assert ranked.provenance == RecallProvenance(
        strategy=STRATEGY_LEXICAL,
        arms=ArmCounts(lexical=2, dense=0),
        dense_available=False,
    )
    assert all(item.strategy == ranked.provenance.strategy for item in ranked.items)

    empty = _recalled(memory.recall(memory.context(), query="quinces"))
    assert empty.items == ()
    assert empty.provenance == RecallProvenance(
        strategy=STRATEGY_LEXICAL,
        arms=ArmCounts(lexical=0, dense=0),
        dense_available=False,
    )


@pytest.mark.parametrize("stored", [None, "not-a-strategy", STRATEGY_DENSE])
def test_an_absent_or_unusable_strategy_row_recalls_lexically_without_refusing(
    memory: MemoryWorkspace, stored: str | None
) -> None:
    """An absent row takes the package default; a present row outside the offered
    vocabulary degrades to ``lexical``. Neither refuses a read."""
    with memory.unit() as uow:
        _write(uow.connection, _row(title="apples", body="a note about apples"))
    memory.set_strategy(stored)

    result = _recalled(memory.recall(memory.context(), query="apples"))
    assert [item.title for item in result.items] == ["apples"]
    assert result.provenance.strategy == STRATEGY_LEXICAL


# --- the centered read window ---------------------------------------------------------


def _container_with(
    memory: MemoryWorkspace, count: int, *, hidden_for: UUID | None = None
) -> tuple[str, tuple[MemoryRow, ...]]:
    """A container memory and ``count`` eligible members linked to it."""
    with memory.unit() as uow:
        container = _write(uow.connection, _row(title="container"))
        reference = memory_reference(container.id)
        members = tuple(
            _write(
                uow.connection,
                _row(title=f"member {index:03d}", recorded_at=_recent(index + 1)),
                links=((reference, "derived_from", False),),
            )
            for index in range(count)
        )
        if hidden_for is not None:
            _write(
                uow.connection,
                _row(
                    title="hidden",
                    audience_kind="member",
                    audience_id=hidden_for,
                    recorded_at=_recent(count + 1),
                ),
                links=((reference, "derived_from", False),),
            )
    return reference, members


def test_read_returns_a_centred_window_with_eligible_only_metadata(
    memory: MemoryWorkspace,
) -> None:
    reference, members = _container_with(memory, 7, hidden_for=uuid7())

    window = _window(
        memory.read(
            memory.context(),
            container_ref=reference,
            target_ref=memory_reference(members[3].id),
        )
    )
    assert [item.title for item in window.items] == [
        f"member {index:03d}" for index in (1, 2, 3, 4, 5)
    ]
    assert (window.target_position, window.window_start, window.window_end) == (2, 1, 6)
    # Seven, not eight: the hidden member is a member of the container and of no
    # count this caller is shown.
    assert window.total == 7
    assert window.has_more is True


def test_read_context_zero_and_ten_bound_the_window(memory: MemoryWorkspace) -> None:
    reference, members = _container_with(memory, 7)
    owner = memory.context()
    target = memory_reference(members[3].id)

    narrow = _window(
        memory.read(owner, container_ref=reference, target_ref=target, context=0)
    )
    assert [item.title for item in narrow.items] == ["member 003"]
    assert (narrow.target_position, narrow.window_start, narrow.window_end) == (0, 3, 4)
    assert narrow.has_more is True

    wide = _window(
        memory.read(owner, container_ref=reference, target_ref=target, context=10)
    )
    assert len(wide.items) == 7 <= 21
    assert (wide.target_position, wide.window_start, wide.window_end) == (3, 0, 7)
    assert wide.has_more is False

    for context in (-1, 11):
        _refused(
            memory.read(
                owner, container_ref=reference, target_ref=target, context=context
            ),
            "input_invalid",
        )


def test_read_refusals_do_not_shift_with_container_size(
    memory: MemoryWorkspace,
) -> None:
    """The same target, against a small container and one over the 500 cap.

    Each pair must answer with the identical earlier refusal and no window metadata:
    an invalid target never reaches the scan, an ineligible one never reaches
    membership, and a non-member never reaches the candidate select. If any of the
    three leaked ``window_scan_limit`` from the big container, the caller would have
    learned that the container is saturated through a target it has no claim on.
    """
    small, _ = _container_with(memory, 2)
    with memory.unit() as uow:
        big_container = _write(uow.connection, _row(title="big container"))
        big = memory_reference(big_container.id)
        _write_many(
            uow.connection,
            [
                _row(title=f"bulk {index:04d}", recorded_at=_recent(index + 1))
                for index in range(501)
            ],
            container=big,
        )
        ineligible = _write(
            uow.connection,
            _row(audience_kind="member", audience_id=uuid7()),
            links=((small, "derived_from", False), (big, "derived_from", False)),
        )
        stranger = _write(uow.connection, _row(title="stranger"))
        # A member of *another* container, so it has links — just none naming the
        # container under test. This is the target that tells an exact
        # ``memory_link.ref = container_ref`` check apart from a looser "does this
        # memory link to anything at all", which would let it into the scan.
        elsewhere_container = _write(uow.connection, _row(title="elsewhere"))
        elsewhere = _write(
            uow.connection,
            _row(title="a member of somewhere else"),
            links=((memory_reference(elsewhere_container.id), "derived_from", False),),
        )

    owner = memory.context()
    for container in (small, big):
        _refused(
            memory.read(owner, container_ref=container, target_ref="not-a-reference"),
            "input_invalid",
        )
        _refused(
            memory.read(
                owner,
                container_ref=container,
                target_ref=f"harness.note:{uuid7()}",
            ),
            "input_invalid",
        )
        _refused(
            memory.read(
                owner,
                container_ref=container,
                target_ref=memory_reference(ineligible.id),
            ),
            "not_found",
        )
        _refused(
            memory.read(
                owner,
                container_ref=container,
                target_ref=memory_reference(uuid7()),
            ),
            "not_found",
        )
        for outsider in (stranger, elsewhere):
            _refused(
                memory.read(
                    owner,
                    container_ref=container,
                    target_ref=memory_reference(outsider.id),
                ),
                "container_membership_required",
            )


def test_read_refuses_a_purpose_that_is_not_this_context_s_binding(
    memory: MemoryWorkspace,
) -> None:
    reference, members = _container_with(memory, 2)
    target = memory_reference(members[0].id)

    _refused(
        memory.read(
            memory.bound_context(_RESPOND),
            container_ref=reference,
            target_ref=target,
            purpose=_INTERNAL,
        ),
        "purpose_mismatch",
    )
    _refused(
        memory.read(
            memory.context(),
            container_ref=reference,
            target_ref=target,
            purpose=_RESPOND,
        ),
        "purpose_mismatch",
    )
    _refused(
        memory.read(
            memory.bound_context(_RESPOND),
            container_ref=reference,
            target_ref=target,
            purpose="not_a_purpose",
        ),
        "input_invalid",
    )
    # Stated and agreeing, and omitted entirely, are both the binding.
    for payload in ({"purpose": _RESPOND}, {}):
        assert (
            _window(
                memory.read(
                    memory.bound_context(_RESPOND),
                    container_ref=reference,
                    target_ref=target,
                    **payload,
                )
            ).total
            == 2
        )


def test_read_current_and_history_modes_over_the_lifecycle_states(
    memory: MemoryWorkspace,
) -> None:
    with memory.unit() as uow:
        container = _write(uow.connection, _row(title="container"))
        reference = memory_reference(container.id)
        link: tuple[Link, ...] = ((reference, "derived_from", False),)
        replacement = _write(uow.connection, _row(title="replacement"))
        live = _write(uow.connection, _row(title="live"), links=link)
        corrected = _write(
            uow.connection,
            _row(
                title="corrected",
                recorded_at=_recent(1),
                invalidation_reason="source_corrected",
            ),
            links=link,
        )
        _write(
            uow.connection,
            _row(
                title="superseded",
                recorded_at=_recent(2),
                invalidation_reason="source_superseded",
                superseded_by_id=replacement.id,
            ),
            links=link,
        )
        erased = _write(
            uow.connection,
            _row(
                title="erased",
                recorded_at=_recent(3),
                invalidation_reason="source_deleted",
            ),
            links=link,
        )
        expired = _write(
            uow.connection,
            _row(title="expired", recorded_at=_long_ago()),
            links=link,
        )

    # The ``expired`` member is excluded only because this workspace turned the age
    # gate on; with the gate off — the default — it is an ordinary current member.
    memory.expire_by_age()

    owner = memory.context()
    current = _window(
        memory.read(
            owner,
            container_ref=reference,
            target_ref=memory_reference(live.id),
            context=10,
        )
    )
    assert [item.title for item in current.items] == ["live"]

    history = _window(
        memory.read(
            owner,
            container_ref=reference,
            target_ref=memory_reference(live.id),
            context=10,
            include_invalidated=True,
        )
    )
    assert [item.title for item in history.items] == [
        "live",
        "corrected",
        "superseded",
    ]
    assert history.total == 3
    shown = {item.title: item for item in history.items}
    assert shown["corrected"].invalidation_reason == "source_corrected"
    assert shown["corrected"].invalidated_at is not None
    # The successor ref, and only because the replacement is itself readable now.
    assert shown["superseded"].superseded_by == memory_reference(replacement.id)
    assert shown["live"].superseded_by is None

    for hidden in (erased, expired):
        for mode in (False, True):
            _refused(
                memory.read(
                    owner,
                    container_ref=reference,
                    target_ref=memory_reference(hidden.id),
                    include_invalidated=mode,
                ),
                "not_found",
            )
    _refused(
        memory.read(
            owner, container_ref=reference, target_ref=memory_reference(corrected.id)
        ),
        "not_found",
    )


def test_read_history_mode_is_not_a_permission_override(
    memory: MemoryWorkspace,
) -> None:
    """``include_invalidated`` widens the lifecycle states and nothing else.

    A retained row whose own source is erased, and one belonging to somebody else's
    member audience, both stay refused with the flag on.
    """
    with memory.unit() as uow:
        container = _write(uow.connection, _row(title="container"))
        reference = memory_reference(container.id)
        erased_source = _write(
            uow.connection,
            _row(title="erased source", invalidation_reason="source_deleted"),
        )
        retained = _write(
            uow.connection,
            _row(
                title="retained",
                recorded_at=_recent(1),
                invalidation_reason="source_corrected",
            ),
            links=(
                (reference, "derived_from", False),
                (memory_reference(erased_source.id), "derived_from", False),
            ),
        )
        theirs = _write(
            uow.connection,
            _row(
                title="theirs",
                recorded_at=_recent(2),
                audience_kind="member",
                audience_id=uuid7(),
                invalidation_reason="source_corrected",
            ),
            links=((reference, "derived_from", False),),
        )

    owner = memory.context()
    for target in (retained, theirs):
        _refused(
            memory.read(
                owner,
                container_ref=reference,
                target_ref=memory_reference(target.id),
                include_invalidated=True,
            ),
            "not_found",
        )


def test_read_scan_limit_is_five_hundred_eligible_candidates(
    memory: MemoryWorkspace,
) -> None:
    """The 500/501 boundary, and the proof that hidden members do not move it.

    Five hundred eligible members plus a hidden-audience one, a wrong-purpose one, an
    expired one and a mode-excluded one still succeeds: those four are prefiltered out
    row-locally, before the 501st identifier is counted. The five hundred and first
    *eligible* member is what flips it, and the refusal carries nothing else.
    """
    with memory.unit() as uow:
        container = _write(uow.connection, _row(title="container"))
        reference = memory_reference(container.id)
        members = _write_many(
            uow.connection,
            [
                _row(title=f"bulk {index:04d}", recorded_at=_recent(index + 1))
                for index in range(500)
            ],
            container=reference,
        )
        link: tuple[Link, ...] = ((reference, "derived_from", False),)
        _write(
            uow.connection,
            _row(
                title="hidden audience",
                audience_kind="member",
                audience_id=uuid7(),
                recorded_at=_recent(600),
            ),
            links=link,
        )
        _write(
            uow.connection,
            _row(title="expired", recorded_at=_long_ago()),
            links=link,
        )
        _write(
            uow.connection,
            _row(
                title="mode excluded",
                recorded_at=_recent(601),
                invalidation_reason="source_corrected",
            ),
            links=link,
        )

    # The gate is on so that the ``expired`` member is one of the four row-locally
    # prefiltered rows this case is about; with it off that member would be eligible
    # and the boundary under test would sit at 499 real members plus it.
    memory.expire_by_age()

    owner = memory.context()
    target = memory_reference(members[250].id)
    window = _window(
        memory.read(owner, container_ref=reference, target_ref=target, context=2)
    )
    assert window.total == 500
    assert len(window.items) == 5
    assert window.target_position == 2

    with memory.unit() as uow:
        _write(
            uow.connection,
            _row(title="the five hundred and first", recorded_at=_recent(700)),
            links=((reference, "derived_from", False),),
        )
    _refused(
        memory.read(owner, container_ref=reference, target_ref=target),
        "window_scan_limit",
    )


def test_a_source_denied_candidate_can_still_trigger_the_read_scan_limit(
    memory: MemoryWorkspace,
) -> None:
    """§ A6's one residual case, written down rather than wished away.

    The 501st candidate is row-locally eligible and would be denied on a full check,
    because its own source is unreadable. The sentinel is counted before any of that,
    so the bounded saturation signal still fires — and it still discloses nothing
    beyond itself. This is the sentence § A6 spells out as "not a promise of zero
    information", and the test exists so nobody later reads the absence of one as a
    stronger promise than the design makes.
    """
    with memory.unit() as uow:
        container = _write(uow.connection, _row(title="container"))
        reference = memory_reference(container.id)
        members = _write_many(
            uow.connection,
            [
                _row(title=f"bulk {index:04d}", recorded_at=_recent(index + 1))
                for index in range(500)
            ],
            container=reference,
        )
        _write(
            uow.connection,
            _row(title="source denied", recorded_at=_recent(700)),
            links=(
                (reference, "derived_from", False),
                (f"harness.note:{uuid7()}", "about", False),
            ),
        )

    outcome = memory.read(
        memory.context(),
        container_ref=reference,
        target_ref=memory_reference(members[0].id),
    )
    _refused(outcome, "window_scan_limit")
    assert outcome.error is not None
    # Fixed and content-free: no candidate count, no identity, no window metadata.
    assert "500" not in outcome.error.error_text
    assert "501" not in outcome.error.error_text


# --- AC 8: every read path fails closed on a missing or corrupt retention policy ------
#
# **Only while the gate is on**, which is why every case below turns it on first. With
# ``retention.expire_by_age`` off there is no window, nothing reads the ``days`` row,
# and ``retention_unavailable`` is unreachable — a workspace that declined age-based
# expiry must not be broken by a setting it never uses. The gate-off half of that rule
# has its own case further down.

_UNUSABLE_RETENTION = (None, "0", "4000", "-1", "not-a-number", "")
"""Absent, below the minimum, above the maximum, negative, unparseable, empty.

Every one of them is a row a reader must refuse rather than fall back from, once the
gate is on. ``None`` deletes the row entirely, which is the case the settings resolver
would answer with the 365-day package default.
"""


def test_read_refuses_a_missing_or_corrupt_retention_policy(
    memory: MemoryWorkspace,
) -> None:
    reference, members = _container_with(memory, 3)
    memory.expire_by_age()
    owner = memory.context()
    target = memory_reference(members[1].id)
    assert (
        _window(memory.read(owner, container_ref=reference, target_ref=target)).total
        == 3
    )

    for value in _UNUSABLE_RETENTION:
        memory.set_retention(value)
        _refused(
            memory.read(owner, container_ref=reference, target_ref=target),
            "retention_unavailable",
        )

    memory.set_retention("365")
    assert (
        _window(memory.read(owner, container_ref=reference, target_ref=target)).total
        == 3
    )


def test_recall_refuses_a_missing_or_corrupt_retention_policy(
    memory: MemoryWorkspace,
) -> None:
    with memory.unit() as uow:
        _write(uow.connection, _row(title="apples", body="a note about apples"))
    memory.expire_by_age()

    owner = memory.context()
    assert memory.recall(owner, query="apples").ok

    for value in _UNUSABLE_RETENTION:
        memory.set_retention(value)
        _refused(memory.recall(owner, query="apples"), "retention_unavailable")

    memory.set_retention("365")
    assert memory.recall(owner, query="apples").ok


def test_the_resolver_refuses_a_read_on_a_missing_or_corrupt_retention_policy(
    memory: MemoryWorkspace,
) -> None:
    with memory.unit() as uow:
        live = _write(uow.connection, _row(title="visible"))
    memory.expire_by_age()
    reference = memory_reference(live.id)
    owner = memory.context()

    for value in _UNUSABLE_RETENTION:
        memory.set_retention(value)
        with memory.reading() as uow:
            answer = resolve_in(
                reference, owner, uow, registry=memory.surfaces.resolvers
            )
        assert isinstance(answer, Unavailable), (value, answer)
        # Not the 365-day default quietly applied, and no head or label either.
        assert answer.reason == "retention_unavailable", value

    memory.set_retention("365")
    with memory.reading() as uow:
        restored = resolve_in(reference, owner, uow, registry=memory.surfaces.resolvers)
    assert isinstance(restored, RecordHead) and restored.display == "visible"


# --- AC 8: the default. Nothing is hidden by age in a workspace that never asked ------
#
# Recallatron never auto-forgets a memory by age. These are the cases that prove the
# *absence* of a mechanism, which is why each one drives a real read rather than
# asserting a resolved policy value: a policy object that said "unbounded" while a
# predicate somewhere still compared a timestamp would satisfy the second and fail the
# first, and the first is the promise.


def test_a_workspace_that_configures_nothing_keeps_its_oldest_memories(
    memory: MemoryWorkspace,
) -> None:
    """The ratified default, on every read surface, for both audiences.

    The workspace is exactly as ``core.module.enable`` left it: an
    ``expire_by_age`` row reading ``false`` and a 365-day window nothing reads. Two
    memories recorded well beyond that window — one workspace-audience, one
    member-private — are still returned by recall, by the centered read, and by
    generic resolution.

    Both audiences on purpose. The member row would survive even with the gate on
    (§ A9's exemption), so a case that used only a member row would pass against a
    build whose gate did nothing; the workspace row is the one that distinguishes
    "no horizon" from "a horizon that happens not to reach this row".
    """
    account = memory.owner_account_id
    with memory.unit() as uow:
        # A body with no shared term, so the recall below scores the two aged rows
        # and not the container they hang off.
        container = _write(
            uow.connection, _row(title="container", body="a holder for two notes")
        )
        reference = memory_reference(container.id)
        link: tuple[Link, ...] = ((reference, "derived_from", False),)
        shared = _write(
            uow.connection,
            _row(
                title="ancient apples",
                body="an old note about apples",
                recorded_at=_long_ago(),
            ),
            links=link,
        )
        private = _write(
            uow.connection,
            _row(
                title="ancient private apples",
                body="an old private note about apples",
                recorded_at=_long_ago(),
                audience_kind="member",
                audience_id=account,
            ),
            links=link,
        )
    # Nothing is configured: the rows are the ones enable wrote, untouched.
    assert str(memory.stored_setting(RETENTION_EXPIRE_BY_AGE_KEY)).lower() == "false"

    owner = memory.context()
    recalled = memory.recall(owner, query="apples")
    assert recalled.ok, recalled
    assert recalled.result is not None
    assert {item.title for item in recalled.result.items} == {
        "ancient apples",
        "ancient private apples",
    }

    window = _window(
        memory.read(
            owner,
            container_ref=reference,
            target_ref=memory_reference(shared.id),
            context=10,
        )
    )
    assert {item.title for item in window.items} == {
        "ancient apples",
        "ancient private apples",
    }
    assert window.total == 2

    with memory.reading() as uow:
        for row in (shared, private):
            head = resolve_in(
                memory_reference(row.id),
                owner,
                uow,
                registry=memory.surfaces.resolvers,
            )
            assert isinstance(head, RecordHead), (row.title, head)
            assert head.readable and head.display == row.title


def test_with_the_gate_off_a_broken_window_row_refuses_nothing(
    memory: MemoryWorkspace,
) -> None:
    """``retention_unavailable`` is unreachable while the gate is off (§ A13).

    The one stated exception to fail-closed, and it is not a softening: a missing or
    corrupt ``days`` row is not a *mandatory* setting in a workspace that reads it
    nowhere. Failing closed on it would break every read and write of a workspace
    over a policy it explicitly declined.

    The same six rows refuse once the gate goes on, which is the case above; the
    pairing is what makes this an exception rather than a hole.
    """
    with memory.unit() as uow:
        live = _write(uow.connection, _row(title="visible"))
        old = _write(uow.connection, _row(title="ancient", recorded_at=_long_ago()))
    owner = memory.context()

    for value in _UNUSABLE_RETENTION:
        memory.set_retention(value)
        assert memory.recall(owner, query="memory").ok, value
        assert memory.remember(
            owner, kind="note", title="t", body="b", purposes=[_RESPOND]
        ).ok, value
        with memory.reading() as uow:
            for row in (live, old):
                head = resolve_in(
                    memory_reference(row.id),
                    owner,
                    uow,
                    registry=memory.surfaces.resolvers,
                )
                assert isinstance(head, RecordHead), (value, row.title, head)


def test_a_workspace_with_no_gate_row_at_all_resolves_false_without_a_backfill(
    memory: MemoryWorkspace,
) -> None:
    """The pre-correction shape: a ``days`` row, and no gate row anywhere.

    ``_write_enable_rows`` runs only at enable and there is no backfill path, so a
    workspace enabled before this key existed holds no row for it — and must resolve
    the package default ``false`` rather than refusing, rather than inheriting a
    horizon it never chose, and **without a reader quietly writing the row**. The
    last clause is asserted directly: a reader that backfilled would turn every read
    into a write and would do it on a connection that may be read-only.
    """
    memory.expire_by_age(None)
    with memory.unit() as uow:
        old = _write(uow.connection, _row(title="ancient", recorded_at=_long_ago()))
    owner = memory.context()

    with memory.reading() as uow:
        assert expire_by_age_enabled(owner, uow) is False
        assert effective_retention(owner, uow) == RetentionPolicy.unbounded()
        head = resolve_in(
            memory_reference(old.id), owner, uow, registry=memory.surfaces.resolvers
        )
    assert isinstance(head, RecordHead) and head.readable

    assert memory.stored_setting(RETENTION_EXPIRE_BY_AGE_KEY) is None, (
        "a reader backfilled the gate row; resolving a default must not write one"
    )


# --- the write surface ----------------------------------------------------------------
#
# Everything below drives ``remember``/``derive``/``entity.*`` through the real
# dispatcher against a real installed Recallatron. Where an assertion is about whether
# something was *written*, it reads the tables directly through ``memory.counts()``:
# a read path that correctly hides a row it should not show would report zero for a row
# that is nevertheless there, which is exactly the defect these cases exist to catch.

_FOLLOW_UP = "follow_up"
_FOREIGN = "harness.note"
"""A record type another (fixture) module owns. Nothing in this workspace enables that
module, so every reference to it is unreadable here — which is what makes it the probe
for "a linked record this caller cannot read denies the write"."""


def _written(outcome: OperationOutcome) -> MemoryWritten:
    assert outcome.ok, outcome
    assert isinstance(outcome.result, MemoryWritten), outcome
    return outcome.result


def _failed(outcome: OperationOutcome) -> None:
    """A handler that broke, as the dispatcher reports it: ``failed`` with no result."""
    assert outcome.state == "failed", outcome
    assert outcome.result is None, outcome


def _links_of(memory: MemoryWorkspace, reference: str) -> set[tuple[str, str]]:
    with memory.reading() as uow:
        rows = uow.connection.execute(
            select(
                memory_tables.memory_link.c.ref, memory_tables.memory_link.c.relation
            ).where(
                memory_tables.memory_link.c.memory_id == RecordRef.parse(reference).id
            )
        ).all()
    return {(str(ref), str(relation)) for ref, relation in rows}


def _stored(memory: MemoryWorkspace, reference: str) -> MemoryRow:
    with memory.reading() as uow:
        row = (
            uow.connection.execute(
                select(memory_tables.memory).where(
                    memory_tables.memory.c.id == RecordRef.parse(reference).id
                )
            )
            .mappings()
            .one()
        )
    return MemoryRow(**{key: row[key] for key in MemoryRow.__slots__})


def test_remember_persists_each_kind_with_its_audience_purposes_and_timestamps(
    memory: MemoryWorkspace,
) -> None:
    """AC 3's first sentence, driven for all four ratified kinds.

    The stored row is read back from its own table rather than from the operation's
    answer, so a handler that returned a well-formed result and wrote something else
    reds here.
    """
    owner = memory.context()
    for kind in memory_tables.MEMORY_KINDS:
        occurred = datetime.now(UTC) - timedelta(hours=3)
        written = _written(
            memory.remember(
                owner,
                kind=kind,
                title=f"  a {kind}  ",
                body=f"the body of a {kind}",
                purposes=[_RESPOND, _INTERNAL],
                confidence=0.5,
                occurred_at=occurred.isoformat(),
            )
        )
        assert written.kind == kind
        assert written.audience == "workspace"
        assert written.purposes == (_INTERNAL, _RESPOND)
        assert written.revision == 1

        row = _stored(memory, written.ref)
        # Trimmed on the way in, and the trimmed form is what is stored.
        assert row.title == f"a {kind}"
        assert row.origin == "told"
        assert row.audience_kind == "workspace" and row.audience_id is None
        assert row.occurred_at == occurred
        assert row.confidence == 0.5
        # Server-owned, and inside this workspace's retention window by construction.
        assert row.recorded_at > datetime.now(UTC) - timedelta(minutes=5)
        assert row.recorded_by_kind == "account"
        assert row.recorded_by_id == memory.owner_account_id
        with memory.reading() as uow:
            assert list_memory_purposes(uow.connection, row.id) == (
                _INTERNAL,
                _RESPOND,
            )

    # And the rows are readable through the ordinary read surface.
    found = memory.recall(owner, query="body")
    assert found.ok and found.result is not None
    assert len(found.result.items) == len(memory_tables.MEMORY_KINDS)


def test_remember_refuses_a_memory_reference_in_either_relation(
    memory: MemoryWorkspace,
) -> None:
    """AC 3: ``use_derive``, raised **before** a row or an entity is created.

    Both relations, and the counts either side prove the refusal is not a rollback of
    work that was already half done — ``remember`` never reaches an insert at all.
    """
    owner = memory.context()
    source = _written(
        memory.remember(
            owner, kind="note", title="a source", body="a body", purposes=[_RESPOND]
        )
    )
    before = memory.counts()
    for field in ("provenance_refs", "about_refs"):
        outcome = memory.remember(
            owner,
            kind="note",
            title="derived by hand",
            body="a body",
            purposes=[_RESPOND],
            **{field: [source.ref]},
        )
        _refused(outcome, "use_derive")
    assert memory.counts() == before


def test_remember_refuses_an_absent_or_empty_purpose_set(
    memory: MemoryWorkspace,
) -> None:
    """An unbound caller states its purposes; there is no guessed default.

    A memory with no purpose would be a memory no bound read could ever return, so
    the absence is refused rather than filled in.
    """
    owner = memory.context()
    before = memory.counts()
    for payload in ({}, {"purposes": []}):
        _refused(
            memory.remember(
                owner, kind="note", title="unstated", body="a body", **payload
            ),
            "purposes_empty",
        )
    assert memory.counts() == before


def test_a_bound_caller_writes_exactly_its_binding_and_its_own_member_audience(
    memory: MemoryWorkspace,
) -> None:
    """§ A4's two write rules for delegated work, both directions.

    The purpose is the binding whether it is stated or omitted, and any other purpose
    in an explicit set is ``purpose_mismatch``. The audience ceiling is that account's
    member audience: omitting it takes the ceiling, and asking for the wider workspace
    audience is ``audience_unavailable``.
    """
    bound = memory.bound_context(_RESPOND)
    written = _written(
        memory.remember(bound, kind="fact", title="delegated", body="a body")
    )
    assert written.purposes == (_RESPOND,)
    assert written.audience == "member"
    row = _stored(memory, written.ref)
    assert row.audience_kind == "member"
    assert row.audience_id == memory.owner_account_id

    assert _written(
        memory.remember(
            bound,
            kind="fact",
            title="stated",
            body="a body",
            purposes=[_RESPOND],
            audience="member",
        )
    ).purposes == (_RESPOND,)

    before = memory.counts()
    _refused(
        memory.remember(
            bound,
            kind="fact",
            title="widened",
            body="a body",
            purposes=[_RESPOND, _INTERNAL],
        ),
        "purpose_mismatch",
    )
    _refused(
        memory.remember(
            bound, kind="fact", title="shared", body="a body", audience="workspace"
        ),
        "audience_unavailable",
    )
    assert memory.counts() == before


def test_remember_refuses_a_linked_record_this_caller_cannot_read(
    memory: MemoryWorkspace,
) -> None:
    """A non-memory provenance or about ref must resolve live and readable."""
    owner = memory.context()
    before = memory.counts()
    for field in ("provenance_refs", "about_refs"):
        _refused(
            memory.remember(
                owner,
                kind="note",
                title="blocked",
                body="a body",
                purposes=[_RESPOND],
                **{field: [f"{_FOREIGN}:{uuid7()}"]},
            ),
            "not_found",
        )
    _refused(
        memory.remember(
            owner,
            kind="note",
            title="malformed",
            body="a body",
            purposes=[_RESPOND],
            about_refs=["not-a-reference"],
        ),
        "input_invalid",
    )
    assert memory.counts() == before


_WRITE_PAYLOADS: tuple[tuple[str, dict[str, object]], ...] = (
    (
        MEMORY_REMEMBER,
        {"kind": "note", "title": "t", "body": "b", "purposes": [_RESPOND]},
    ),
    (
        MEMORY_DERIVE,
        {"kind": "summary", "title": "t", "body": "b", "purposes": [_RESPOND]},
    ),
)
"""The two write operations, with the smallest payload each accepts. ``derive``'s
``sources`` is filled in per test, because it needs a reference that exists."""


def test_every_write_path_fails_closed_on_a_missing_or_corrupt_retention_policy(
    memory: MemoryWorkspace,
) -> None:
    """AC 8's write half: ``retention_unavailable``, and **nothing written**.

    The stored setting is removed and then corrupted in each of the ways a bad
    restore or a hand-edited row could leave it, and the proof that no row landed is
    a direct table read rather than a later read that would have hidden one anyway.
    A writer that fell back to the 365-day package default would pass every read test
    in this file and fail here.
    """
    owner = memory.context()
    source = _written(
        memory.remember(
            owner, kind="note", title="a source", body="a body", purposes=[_RESPOND]
        )
    )
    memory.expire_by_age()
    before = memory.counts()
    for value in _UNUSABLE_RETENTION:
        memory.set_retention(value)
        _refused(
            memory.remember(
                owner, kind="note", title="t", body="b", purposes=[_RESPOND]
            ),
            "retention_unavailable",
        )
        _refused(
            memory.derive(
                owner,
                kind="summary",
                title="t",
                body="b",
                purposes=[_RESPOND],
                sources=[source.ref],
            ),
            "retention_unavailable",
        )
    assert memory.counts() == before

    memory.set_retention("365")
    assert memory.remember(
        owner, kind="note", title="t", body="b", purposes=[_RESPOND]
    ).ok


def test_derive_meets_the_source_audiences_and_intersects_their_purposes(
    memory: MemoryWorkspace,
) -> None:
    """AC 4's first sentence: workspace + member-private gives member-private, with
    only the purposes both sources carry."""
    owner = memory.context()
    shared = _written(
        memory.remember(
            owner,
            kind="fact",
            title="shared",
            body="visible to the workspace",
            purposes=[_RESPOND, _INTERNAL, _FOLLOW_UP],
        )
    )
    private = _written(
        memory.remember(
            owner,
            kind="fact",
            title="private",
            body="visible to one member",
            audience="member",
            purposes=[_RESPOND, _INTERNAL],
        )
    )

    written = _written(
        memory.derive(
            owner,
            kind="summary",
            title="both",
            body="a summary of both",
            sources=[shared.ref, private.ref, shared.ref],
        )
    )
    assert written.audience == "member"
    assert written.purposes == (_INTERNAL, _RESPOND)
    row = _stored(memory, written.ref)
    assert row.origin == "derived"
    assert row.audience_id == memory.owner_account_id
    # Every direct source is stored as a ``derived_from`` link, and the duplicate
    # spelling collapsed rather than becoming a second row.
    assert _links_of(memory, written.ref) == {
        (shared.ref, "derived_from"),
        (private.ref, "derived_from"),
    }


def test_derive_refuses_an_empty_meet_or_an_empty_intersection_without_writing(
    memory: MemoryWorkspace, cluster: ClusterSession
) -> None:
    """AC 4's second sentence, and AC 3's "refuses without a resulting record".

    Two member audiences meet nowhere, and two disjoint purpose sets intersect
    nowhere. Both are decided before any write, which the unchanged counts prove.
    """
    other = add_member(
        cluster.backend, memory.workspace, Role.MEMBER, display_name="member-two"
    )
    owner = memory.context()
    mine = _written(
        memory.remember(
            owner,
            kind="fact",
            title="mine",
            body="a body",
            audience="member",
            purposes=[_RESPOND],
        )
    )
    theirs = _written(
        memory.remember(
            memory.context(account_id=other, role=Role.MEMBER),
            kind="fact",
            title="theirs",
            body="a body",
            audience="member",
            purposes=[_RESPOND],
        )
    )
    disjoint = _written(
        memory.remember(
            owner,
            kind="fact",
            title="disjoint",
            body="a body",
            audience="member",
            purposes=[_INTERNAL],
        )
    )

    before = memory.counts()
    # An operator context is the only way to hold both member rows eligible at once,
    # and it has no write role — so the empty meet is driven through the member who
    # can read both: neither can. What is reachable, and is the case A4 names, is a
    # caller asking for an audience its sources do not admit.
    _refused(
        memory.derive(
            owner,
            kind="summary",
            title="widened",
            body="a body",
            sources=[mine.ref],
            audience="workspace",
        ),
        "audience_unavailable",
    )
    _refused(
        memory.derive(
            owner,
            kind="summary",
            title="disjoint purposes",
            body="a body",
            sources=[mine.ref, disjoint.ref],
        ),
        "purposes_empty",
    )
    # A source belonging to another member is not eligible at all, which is the
    # earlier refusal the empty meet never gets to.
    _refused(
        memory.derive(
            owner,
            kind="summary",
            title="not mine",
            body="a body",
            sources=[theirs.ref],
        ),
        "not_found",
    )
    _refused(
        memory.derive(owner, kind="summary", title="none", body="a body", sources=[]),
        "input_invalid",
    )
    assert memory.counts() == before


def test_derive_inherits_its_sources_actual_restrictions(
    memory: MemoryWorkspace,
) -> None:
    """§ A5: "store all direct derived_from refs and inherited actual source/about
    restrictions".

    The inherited ``about`` link is what keeps the derivation as restricted as the
    thing it came from: drop it and a memory derived from a row that names a record
    the caller may not read becomes readable the moment the source is deleted.
    """
    owner = memory.context()
    with memory.unit() as uow:
        readable = _write(uow.connection, _row(title="a readable source"))
    source = _written(
        memory.remember(
            owner,
            kind="fact",
            title="with an about link",
            body="a body",
            purposes=[_RESPOND],
        )
    )
    # Give the source an actual permission-bearing link through the repository: the
    # write path refuses a memory ref, and what is under test is the *copy*, not how
    # the source acquired it.
    with memory.unit() as uow:
        insert_memory_link(
            uow.connection,
            MemoryLinkRow(
                memory_id=RecordRef.parse(source.ref).id,
                ref=memory_reference(readable.id),
                relation="about",
                created_at=datetime.now(UTC),
                supersession_lineage=False,
            ),
        )
    written = _written(
        memory.derive(
            owner,
            kind="summary",
            title="inheriting",
            body="a body",
            sources=[source.ref],
        )
    )
    assert _links_of(memory, written.ref) == {
        (source.ref, "derived_from"),
        (memory_reference(readable.id), "about"),
    }


# --- entities -------------------------------------------------------------------------


def _create(kind: str, name: str, **extra: object) -> dict[str, object]:
    return {"variant": "create", "kind": kind, "name": name, **extra}


def _select(entity_ref: str, **extra: object) -> dict[str, object]:
    return {"variant": "select", "entity_ref": entity_ref, **extra}


def _entities(outcome: OperationOutcome) -> EntityList:
    assert outcome.ok, outcome
    assert isinstance(outcome.result, EntityList), outcome
    return outcome.result


def test_inline_creation_always_mints_a_fresh_entity_even_for_the_same_name(
    memory: MemoryWorkspace,
) -> None:
    """AC 3: "typed inline creation always creates a fresh entity UUID including for
    duplicate normalized names".

    Two writes naming ``Ada Lovelace`` and ``ada   LOVELACE`` normalize to one string
    and are still two entities. A create that matched on name would answer "does an
    entity called this already exist here" to anyone who can write a memory.
    """
    owner = memory.context()
    first = _written(
        memory.remember(
            owner,
            kind="note",
            title="one",
            body="a body",
            purposes=[_RESPOND],
            mentions=[_create("person", "Ada Lovelace", role="author")],
        )
    )
    second = _written(
        memory.remember(
            owner,
            kind="note",
            title="two",
            body="a body",
            purposes=[_RESPOND],
            mentions=[_create("person", "ada   LOVELACE")],
        )
    )
    assert first.mentions[0].ref != second.mentions[0].ref
    assert first.mentions[0].role == "author"
    assert second.mentions[0].role is None
    with memory.reading() as uow:
        normalized = (
            uow.connection.execute(
                select(memory_tables.memory_entity.c.normalized_name)
            )
            .scalars()
            .all()
        )
    assert set(normalized) == {"ada lovelace"}


def test_selection_needs_an_authorized_entity_and_collapses_a_duplicate(
    memory: MemoryWorkspace, cluster: ClusterSession
) -> None:
    """Selection reaches an entity only through an eligible mention; a second
    selection of the same entity is the same mention, not a second one.

    The entity is created on a **member-private** memory, which is what makes the
    negative half real: another member of the same workspace has no eligible mention
    and no backing ref, so it has no route at all. Created on a workspace-audience
    memory the entity would be reachable by everybody and the refusal below would be
    testing nothing.
    """
    owner = memory.context()
    first = _written(
        memory.remember(
            owner,
            kind="note",
            title="one",
            body="a body",
            audience="member",
            purposes=[_RESPOND],
            mentions=[_create("project", "Rheo")],
        )
    )
    entity_ref = first.mentions[0].ref

    again = _written(
        memory.remember(
            owner,
            kind="note",
            title="two",
            body="a body",
            audience="member",
            purposes=[_RESPOND],
            mentions=[
                _select(entity_ref, role="subject"),
                _select(entity_ref, role="subject"),
            ],
        )
    )
    assert [mention.ref for mention in again.mentions] == [entity_ref]

    # Another member has no route to it: no eligible mention, no backing ref. The
    # answer is the same ``not_found`` an unknown id gets — no label, no collision.
    stranger = add_member(
        cluster.backend, memory.workspace, Role.MEMBER, display_name="member-two"
    )
    before = memory.counts()
    _refused(
        memory.remember(
            memory.context(account_id=stranger, role=Role.MEMBER),
            kind="note",
            title="theirs",
            body="a body",
            audience="member",
            purposes=[_RESPOND],
            mentions=[_select(entity_ref)],
        ),
        "not_found",
    )
    _refused(
        memory.remember(
            owner,
            kind="note",
            title="unknown",
            body="a body",
            purposes=[_RESPOND],
            mentions=[_select(entity_reference(uuid7()))],
        ),
        "not_found",
    )
    assert memory.counts() == before


def test_a_contradictory_mention_variant_refuses_input_invalid(
    memory: MemoryWorkspace,
) -> None:
    """§ A13: duplicates collapse by composite key, contradictions refuse.

    One ``(memory_id, entity_id)`` row cannot carry two roles, and silently keeping
    either would be a coin flip the caller cannot see.
    """
    owner = memory.context()
    first = _written(
        memory.remember(
            owner,
            kind="note",
            title="one",
            body="a body",
            purposes=[_RESPOND],
            mentions=[_create("topic", "retention")],
        )
    )
    before = memory.counts()
    _refused(
        memory.remember(
            owner,
            kind="note",
            title="contradictory",
            body="a body",
            purposes=[_RESPOND],
            mentions=[
                _select(first.mentions[0].ref, role="author"),
                _select(first.mentions[0].ref, role="subject"),
            ],
        ),
        "input_invalid",
    )
    assert memory.counts() == before


def test_a_backing_ref_must_resolve_and_becomes_an_about_link(
    memory: MemoryWorkspace,
) -> None:
    """§ A3: "a backing entity ref always adds an ``about`` link in the same
    transaction", and § A5: it must resolve before its label is returned."""
    owner = memory.context()
    with memory.unit() as uow:
        backing = _write(uow.connection, _row(title="a backing record"))
    backing_ref = memory_reference(backing.id)

    written = _written(
        memory.remember(
            owner,
            kind="note",
            title="backed",
            body="a body",
            purposes=[_RESPOND],
            mentions=[_create("thing", "the thing", backing_ref=backing_ref)],
        )
    )
    assert written.mentions[0].backing_ref == backing_ref
    assert (backing_ref, "about") in _links_of(memory, written.ref)

    before = memory.counts()
    _refused(
        memory.remember(
            owner,
            kind="note",
            title="unbacked",
            body="a body",
            purposes=[_RESPOND],
            mentions=[_create("thing", "no such", backing_ref=f"{_FOREIGN}:{uuid7()}")],
        ),
        "not_found",
    )
    assert memory.counts() == before


def test_entity_reads_show_only_eligible_results_and_eligible_counts(
    memory: MemoryWorkspace, cluster: ClusterSession
) -> None:
    """AC 3's entity half: no hidden label, ref, collision or count.

    The owner's own count is two — the two memories it can read — while the member
    who can read only one of them is told one. A count taken over every mention would
    report the existence of a memory that caller may not see.
    """
    owner = memory.context()
    other = add_member(
        cluster.backend, memory.workspace, Role.MEMBER, display_name="member-two"
    )
    shared = _written(
        memory.remember(
            owner,
            kind="note",
            title="shared",
            body="a body",
            purposes=[_RESPOND],
            mentions=[_create("organization", "Novadiem")],
        )
    )
    entity_ref = shared.mentions[0].ref
    _written(
        memory.remember(
            owner,
            kind="note",
            title="private",
            body="a body",
            audience="member",
            purposes=[_RESPOND],
            mentions=[_select(entity_ref)],
        )
    )

    listed = _entities(memory.call(owner, ENTITY_LIST, {}))
    assert [item.ref for item in listed.items] == [entity_ref]
    assert listed.items[0].mention_count == 2
    assert listed.items[0].name == "Novadiem"

    theirs = memory.context(account_id=other, role=Role.MEMBER)
    assert _entities(memory.call(theirs, ENTITY_LIST, {})).items[0].mention_count == 1

    got = memory.call(owner, ENTITY_GET, {"entity_ref": entity_ref})
    assert got.ok and isinstance(got.result, EntityItem)
    assert got.result.mention_count == 2
    _refused(
        memory.call(owner, ENTITY_GET, {"entity_ref": entity_reference(uuid7())}),
        "not_found",
    )
    _refused(
        memory.call(owner, ENTITY_GET, {"entity_ref": shared.ref}), "input_invalid"
    )
    # The kind filter narrows the caller's own eligible set and nothing else.
    assert _entities(memory.call(owner, ENTITY_LIST, {"kind": "person"})).items == ()
    for limit in (0, 51):
        _refused(memory.call(owner, ENTITY_LIST, {"limit": limit}), "input_invalid")


def test_removing_the_last_mention_prunes_an_unbacked_entity(
    memory: MemoryWorkspace,
) -> None:
    """AC 3: "deleting the last mention prunes an unbacked entity" — in the deleting
    transaction, and only for one with no independent route.

    Driven at the service seam rather than through an operation because no operation
    in this run removes a mention: correction and deletion are the next prompt's, and
    a shallow test through a path that does not exist would prove nothing. What is
    pinned here is the rule those paths will call.
    """
    owner = memory.context()
    with memory.unit() as uow:
        backing = _write(uow.connection, _row(title="a backing record"))
    written = _written(
        memory.remember(
            owner,
            kind="note",
            title="two mentions",
            body="a body",
            purposes=[_RESPOND],
            mentions=[
                _create("person", "unbacked"),
                _create("person", "backed", backing_ref=memory_reference(backing.id)),
            ],
        )
    )
    unbacked, backed = (
        next(m for m in written.mentions if m.name == "unbacked"),
        next(m for m in written.mentions if m.name == "backed"),
    )
    memory_id = RecordRef.parse(written.ref).id

    with memory.unit() as uow:
        assert remove_mention(
            uow.connection, memory_id, RecordRef.parse(unbacked.ref).id
        )
        # The backed one survives its last mention: the ref is an independent route,
        # and pruning a row somebody else's record points at would leave it dangling.
        assert not remove_mention(
            uow.connection, memory_id, RecordRef.parse(backed.ref).id
        )
    with memory.reading() as uow:
        surviving = (
            uow.connection.execute(select(memory_tables.memory_entity.c.name))
            .scalars()
            .all()
        )
    assert list(surviving) == ["backed"]


# --- events, and the three-way atomic commit (AC 1) -----------------------------------


class _FailingSink:
    """An audit sink whose ``record`` raises, installed for one test.

    Sink management follows ``tests/postgres/test_audit_dispatch.py``'s own recipe —
    ``reset_sinks()`` then ``install_sink`` per module, restored afterwards — rather
    than a new mechanism. What differs is only *which* sink is installed: that file
    injects a commit failure because its subject is the row surviving a rollback;
    this one injects the sink itself, because AC 1 words the failure as the audit
    sink's.
    """

    __slots__ = ()

    def record(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("the audit sink is unavailable")


@pytest.fixture
def failing_audit_sink() -> Iterator[None]:
    reset_sinks()
    install_sink(CORE_MODULE_ID, CORE_AUDIT_SINK)
    install_sink(MANIFEST.module_id, _FailingSink())
    yield
    reset_sinks()
    install_sink(CORE_MODULE_ID, CORE_AUDIT_SINK)
    install_sink(HARNESS_MODULE_ID, CORE_AUDIT_SINK)


def test_the_manifest_declares_both_ratified_events_and_no_subscription() -> None:
    """AC 1: exactly two ``EventDeclaration``s, schema version 1, three fields each."""
    assert MANIFEST.subscriptions == ()
    declared = {event.type: event for event in MANIFEST.events}
    assert set(declared) == {MEMORY_RECORDED, MEMORY_INVALIDATED}
    for event in declared.values():
        assert event.schema_version == 1
        assert set(event.data.model_fields) == {"memory_ref", "kind", "reason"}


def test_a_write_commits_its_rows_its_event_and_its_audit_row_together(
    memory: MemoryWorkspace,
) -> None:
    """AC 1's positive half, for both writes, read back from all three tables.

    The publish reaching no subscriber is the shipped state and is a success: one
    outbox row, zero delivery rows.
    """
    owner = memory.context()
    source = _written(
        memory.remember(
            owner,
            kind="note",
            title="a source",
            body="a body",
            purposes=[_RESPOND],
            mentions=[_create("topic", "events")],
        )
    )
    derived = _written(
        memory.derive(
            owner,
            kind="summary",
            title="a summary",
            body="a body",
            sources=[source.ref],
        )
    )

    events = memory.outbox()
    assert [row.type for row in events] == [MEMORY_RECORDED, MEMORY_RECORDED]
    assert [row.subject_ref for row in events] == [source.ref, derived.ref]
    assert [row.subject_revision for row in events] == [1, 1]
    assert [row.data for row in events] == [
        {"memory_ref": source.ref, "kind": "note", "reason": None},
        {"memory_ref": derived.ref, "kind": "summary", "reason": None},
    ]
    assert [row.source for row in events] == [
        f"urn:rheo:module:{MANIFEST.module_id}"
    ] * 2
    with memory.reading() as uow:
        deliveries = uow.connection.execute(
            select(func.count()).select_from(work_tables.event_delivery)
        ).scalar_one()
    assert deliveries == 0

    for operation in (MEMORY_REMEMBER, MEMORY_DERIVE):
        rows = memory.audit_rows(operation)
        assert [row.outcome for row in rows] == ["succeeded"], operation
        assert rows[0].safety_class == "mutate"
        assert rows[0].subject_ref is None


def test_an_audit_sink_failure_commits_none_of_the_three(
    memory: MemoryWorkspace, failing_audit_sink: None
) -> None:
    """AC 1's negative half, and the outbox assertion is the point of it.

    A publish that had already written its outbox row and then rolled back is exactly
    the failure this test exists to catch: the event would be delivered for a memory
    that does not exist. So the absence of the outbox row is asserted explicitly,
    beside the absence of the memory, its children and the audit row.
    """
    owner = memory.context()
    before = memory.counts()

    outcome = memory.remember(
        owner,
        kind="note",
        title="never written",
        body="a body",
        purposes=[_RESPOND],
        mentions=[_create("person", "nobody")],
    )

    _failed(outcome)
    assert memory.counts() == before
    assert memory.audit_rows(MEMORY_REMEMBER) == []
    assert memory.outbox() == []


def test_a_write_with_no_consumer_registry_refuses_rather_than_skipping_the_publish(
    memory: MemoryWorkspace,
) -> None:
    """A composition root that wired no registry has a deployment bug.

    The handler refuses rather than building a throwaway registry, whose fan-out
    would depend on which call built it — a silent wrong answer instead of a visible
    refusal. Nothing is written, which the counts prove.
    """
    before = memory.counts()
    outcome = memory.unwired(
        memory.context(),
        MEMORY_REMEMBER,
        {"kind": "note", "title": "t", "body": "b", "purposes": [_RESPOND]},
    )
    _refused(outcome, CONSUMERS_MISSING)
    assert memory.counts() == before


def test_the_kind_check_constraint_admits_exactly_the_four_ratified_kinds(
    cluster: ClusterSession, workspace: UUID, recallatron_loaded: None
) -> None:
    """AC 14's second, independent proof, read off the live database.

    ``modules/recallatron/tests/test_memory_contracts.py`` scans the source tree for
    the two deferred names; this reads the constraint the migration actually created.
    A migration that widened the vocabulary with raw DDL would pass that scan and fail
    here.
    """
    _migrate(cluster, workspace)
    _, engine = _workspace_engine(cluster, workspace)
    with engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT pg_get_constraintdef(constraint_class.oid) "
                "FROM pg_catalog.pg_constraint AS constraint_class "
                "JOIN pg_catalog.pg_class AS table_class "
                "ON table_class.oid = constraint_class.conrelid "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = table_class.relnamespace "
                "WHERE namespace.nspname = :schema "
                "AND constraint_class.conname = 'memory_kind'"
            ),
            {"schema": _OWNED_SCHEMA},
        ).scalar_one()
    for kind in memory_tables.MEMORY_KINDS:
        assert f"'{kind}'" in definition
    assert definition.count("::text") == len(memory_tables.MEMORY_KINDS)


# --- the trusted source-unit seam -----------------------------------------------------
#
# The two ``SourceAuthority`` implementations in this run, and both live here rather
# than in the module: a real producer adapter belongs to the later automatic-memory
# carrier and to the migration run, and shipping one in production code would be
# shipping the producer this run is not authorized to build.
#
# Three rules that only a later producer will ever exercise in anger are built and
# tested here against these fixtures rather than deferred with that producer:
#
#   1. acceptance requires ``source_recorded_at >= now - recallatron.retention.days``
#      and terminalizes an out-of-window unit ``noop`` rather than writing a row no
#      read could return;
#   2. automatic acceptance binds exactly ``internal_analysis``, with the consequence
#      that such a memory is invisible to a bound read requiring another purpose;
#   3. one source unit yields at most one destination representation — a message
#      carrying two *separable* explicit facts is still one unit.


@dataclass(frozen=True)
class SyntheticAuthority:
    """The synthetic runtime adapter: it answers with a grant somebody chose.

    A ``SourceAuthority`` that returned a bare boolean would be unfalsifiable — the
    seam would have nothing to hold the unit against — so the protocol answers with
    the *facts it verified* and the seam enforces them. Making the grant a fixture
    parameter is what lets a test vary one verified fact at a time: a ceiling
    narrower than the unit's audience, a purpose the unit does not carry, a required
    link it omits.
    """

    grant: AuthorityGrant | None

    def verify(
        self, unit: TrustedSourceUnit, *, workspace_id: UUID, now: datetime
    ) -> AuthorityGrant | AuthorityRefused:
        if self.grant is None:
            return AuthorityRefused(reason="not_enrolled")
        return self.grant


@dataclass(frozen=True)
class ValidatedImporterAuthority:
    """The validated importer's adapter: the batch was validated before it got here.

    The importer is the other thing § A5 permits to exercise this seam in 1a1, and it
    differs from the runtime adapter in exactly one way that matters: it has already
    validated the whole batch, so it grants what the row itself claims. It is a
    separate class rather than a flag because "trust the row" is precisely the
    property a *runtime* adapter must never have.
    """

    workspace_id: UUID

    def verify(
        self, unit: TrustedSourceUnit, *, workspace_id: UUID, now: datetime
    ) -> AuthorityGrant | AuthorityRefused:
        if workspace_id != self.workspace_id:
            return AuthorityRefused(reason="wrong_workspace")
        return AuthorityGrant(
            workspace_id=workspace_id,
            producer_kind=unit.producer_kind,
            authority_id=unit.authority_id,
            principal_account_id=unit.principal_account_id,
            audience_kind=unit.audience_kind,
            audience_id=unit.audience_id,
            bound_purpose=unit.bound_purpose,
            source_revision=1,
            required_links=unit.evidence.links,
        )


_AUTHORITY_ID = uuid7()


def _unit(**overrides: object) -> TrustedSourceUnit:
    """One trusted unit from the synthetic runtime producer, inside its window."""
    now = datetime.now(UTC)
    evidence: dict[str, object] = {
        "kind": "note",
        "title": "what the message said",
        "body": "an explicitly stated fact",
    }
    evidence.update(overrides.pop("evidence", {}))  # type: ignore[arg-type]
    fields: dict[str, object] = {
        "producer_kind": "rheo_runtime",
        "authority_id": _AUTHORITY_ID,
        "principal_account_id": None,
        "audience_kind": "workspace",
        "audience_id": None,
        "bound_purpose": _INTERNAL,
        "source_recorded_at": now - timedelta(minutes=5),
        "source_expires_at": now + timedelta(days=1),
        "external_source_key": f"message-{uuid7()}",
        "evidence": SanitizedEvidence(**evidence),  # type: ignore[arg-type]
    }
    fields.update(overrides)
    return TrustedSourceUnit(**fields)  # type: ignore[arg-type]


def _granting(
    memory: MemoryWorkspace, unit: TrustedSourceUnit, **overrides: object
) -> SyntheticAuthority:
    fields: dict[str, object] = {
        "workspace_id": memory.workspace,
        "producer_kind": unit.producer_kind,
        "authority_id": unit.authority_id,
        "principal_account_id": unit.principal_account_id,
        "audience_kind": unit.audience_kind,
        "audience_id": unit.audience_id,
        "bound_purpose": unit.bound_purpose,
        "source_revision": 1,
        "required_links": unit.evidence.links,
    }
    fields.update(overrides)
    return SyntheticAuthority(AuthorityGrant(**fields))  # type: ignore[arg-type]


def _accept(
    memory: MemoryWorkspace,
    unit: TrustedSourceUnit,
    *,
    authority: object | None = None,
    now: datetime | None = None,
) -> AcceptOutcome:
    adapter = _granting(memory, unit) if authority is None else authority
    assert hasattr(adapter, "verify")
    with memory.unit() as uow:
        return accept_source_unit(
            memory.context(),
            uow,
            unit,
            authority=adapter,  # type: ignore[arg-type]
            consumers=memory.consumers,
            now=now,
        )


def _receipts(memory: MemoryWorkspace) -> list[tuple[str, str, bool]]:
    """``(external key, state, has a record id)`` for every receipt, key-ordered."""
    with memory.reading() as uow:
        rows = uow.connection.execute(
            select(
                memory_tables.source_receipt.c.external_source_key,
                memory_tables.source_receipt.c.state,
                memory_tables.source_receipt.c.record_id,
            ).order_by(memory_tables.source_receipt.c.external_source_key)
        ).all()
    return [(str(key), str(state), record is not None) for key, state, record in rows]


def test_acceptance_binds_internal_analysis_and_hides_it_from_another_purpose(
    memory: MemoryWorkspace,
) -> None:
    """Ratified rule 2, and the product consequence § A5 states out loud.

    A memory recorded automatically carries exactly ``internal_analysis``. It is
    therefore invisible to a read bound to ``respond`` — no override — visible to an
    unbound browse, and visible to a read bound to ``internal_analysis``.
    """
    unit = _unit()
    outcome = _accept(memory, unit)
    assert outcome.state == "active" and outcome.memory_ref is not None

    row = _stored(memory, outcome.memory_ref)
    assert row.origin == "derived"
    assert row.recorded_at == unit.source_recorded_at
    assert row.recorded_by_kind == "service" and row.recorded_by_id is None
    assert row.source_namespace is not None
    assert row.external_source_key == unit.external_source_key
    with memory.reading() as uow:
        assert list_memory_purposes(uow.connection, row.id) == (_INTERNAL,)
    assert _receipts(memory) == [(unit.external_source_key, "active", True)]

    found = memory.recall(memory.context(), query="explicitly")
    assert found.ok and found.result is not None
    assert [item.title for item in found.result.items] == [unit.evidence.title]

    bound_elsewhere = memory.recall(memory.bound_context(_RESPOND), query="explicitly")
    assert bound_elsewhere.ok and bound_elsewhere.result is not None
    assert bound_elsewhere.result.items == ()

    bound_here = memory.recall(memory.bound_context(_INTERNAL), query="explicitly")
    assert bound_here.ok and bound_here.result is not None
    assert len(bound_here.result.items) == 1


def test_acceptance_terminalizes_a_born_expired_unit_as_noop_with_no_row_written(
    memory: MemoryWorkspace,
) -> None:
    """Ratified rule 1, proved by a direct table read rather than by a later read.

    The unit's authority, window and evidence are all fine; its ``source_recorded_at``
    simply sits outside the workspace's *current* retention window, **and this
    workspace has turned age-based expiry on**. A memory written from it would be
    excluded from every read and export the instant it committed, so acceptance
    writes a ``noop`` receipt and no memory at all.

    The proof is the memory table, not ``recall``: a read excludes an expired row
    anyway, so a read-path assertion would pass just as happily if the row were there.

    The two cases where the *same* out-of-window clock is accepted instead — the gate
    off, and a member-audience ceiling — are the two cases below.
    """
    memory.expire_by_age()
    memory.set_retention("30")
    now = datetime.now(UTC)
    unit = _unit(
        source_recorded_at=now - timedelta(days=45),
        source_expires_at=now + timedelta(days=1),
    )
    before = memory.counts()

    outcome = _accept(memory, unit, now=now)

    assert outcome == AcceptOutcome("noop", None)
    after = memory.counts()
    assert after["memory"] == before["memory"]
    assert after["purpose"] == before["purpose"]
    assert after["link"] == before["link"]
    assert after["outbox"] == before["outbox"]
    assert _receipts(memory) == [(unit.external_source_key, "noop", False)]

    # The boundary itself is retained, which is § A9's rule for every other
    # comparison against the horizon: exactly at the horizon is still inside it.
    inside = _unit(
        source_recorded_at=now - timedelta(days=30) + timedelta(seconds=1),
        source_expires_at=now + timedelta(days=1),
    )
    assert _accept(memory, inside, now=now).state == "active"


def test_acceptance_keeps_an_ancient_unit_when_the_workspace_has_no_age_gate(
    memory: MemoryWorkspace,
) -> None:
    """The same out-of-window clock, in the default workspace: accepted, live.

    There is no window to be outside of, so the recheck is **skipped** rather than
    run against a substituted default — a default would invent a horizon the
    workspace explicitly declined. The row is written, it is current, and it is
    returned by an ordinary read.
    """
    memory.set_retention("30")
    now = datetime.now(UTC)
    unit = _unit(
        source_recorded_at=now - timedelta(days=45),
        source_expires_at=now + timedelta(days=1),
    )

    outcome = _accept(memory, unit, now=now)

    assert outcome.state == "active"
    assert outcome.memory_ref is not None
    assert _receipts(memory) == [(unit.external_source_key, "active", True)]
    with memory.reading() as uow:
        head = resolve_in(
            outcome.memory_ref,
            memory.context(),
            uow,
            registry=memory.surfaces.resolvers,
        )
    assert isinstance(head, RecordHead) and head.readable, head


def test_acceptance_writes_no_terminal_noop_for_an_old_member_audience_unit(
    memory: MemoryWorkspace,
) -> None:
    """The gate is **on** and the clock is out of window, and it is still accepted.

    § A9 puts member-audience rows outside the retention mechanism entirely — never
    swept, never age-filtered, never counted by export — so the resulting row is not
    born expired at all: it is permanently readable by its own member. Terminalizing
    it ``noop`` would be worse than merely writing an invisible row, which is why
    this case is asserted on the **receipt** and not only on the memory: a terminal
    receipt is replay-proof, so the unit could never be re-offered and that member's
    evidence would be destroyed for a reason the age mechanism does not apply to it.
    """
    memory.expire_by_age()
    memory.set_retention("30")
    account = memory.owner_account_id
    now = datetime.now(UTC)
    unit = _unit(
        principal_account_id=account,
        audience_kind="member",
        audience_id=account,
        source_recorded_at=now - timedelta(days=45),
        source_expires_at=now + timedelta(days=1),
    )

    outcome = _accept(memory, unit, now=now)

    assert outcome.state == "active", outcome
    assert _receipts(memory) == [(unit.external_source_key, "active", True)]
    # And it is readable by the account whose memory it is, which is the other half
    # of the exemption. (That account is the workspace owner here because the fixture
    # provisions one membership; the audience test is on the account id, not a role.)
    assert outcome.memory_ref is not None
    with memory.reading() as uow:
        head = resolve_in(
            outcome.memory_ref,
            memory.context(account_id=account),
            uow,
            registry=memory.surfaces.resolvers,
        )
    assert isinstance(head, RecordHead) and head.readable, head


def test_one_unit_with_two_separable_facts_yields_one_representation(
    memory: MemoryWorkspace,
) -> None:
    """Ratified rule 3, and the grain the receipt's primary key fixes.

    The evidence carries two explicit facts that could have been split. It is one
    unit, so it is one memory, under that message's one kind, audience, purpose set
    and confidence — and a replay of the same identity returns that same
    representation rather than creating a second one.
    """
    unit = _unit(
        evidence={
            "kind": "fact",
            "title": "two things at once",
            "body": "Ada joined on Tuesday. The project ships in March.",
            "confidence": 0.9,
        }
    )
    first = _accept(memory, unit)
    assert first.state == "active" and first.memory_ref is not None
    after_first = memory.counts()

    replay = _accept(memory, unit)

    assert replay == first, "an exact-digest replay returns the same representation"
    assert memory.counts() == after_first, "and performs no write"
    with memory.reading() as uow:
        titles = (
            uow.connection.execute(select(memory_tables.memory.c.title)).scalars().all()
        )
    assert list(titles) == ["two things at once"]
    assert _stored(memory, first.memory_ref).confidence == 0.9


def test_a_changed_digest_under_one_identity_is_source_unavailable(
    memory: MemoryWorkspace,
) -> None:
    """The digest detects changed sanitized evidence under the same identity.

    It is the *envelope* that is digested, not the text alone — so a replay that kept
    the body and moved the audience ceiling is a changed digest too, and is suppressed
    rather than quietly accepted under the new ceiling.
    """
    unit = _unit()
    assert _accept(memory, unit).state == "active"
    after = memory.counts()

    edited = _unit(
        external_source_key=unit.external_source_key,
        source_recorded_at=unit.source_recorded_at,
        source_expires_at=unit.source_expires_at,
        evidence={
            "kind": "note",
            "title": "what the message said",
            "body": "a different body",
        },
    )
    assert _accept(memory, edited) == AcceptOutcome("source_unavailable", None)
    assert memory.counts() == after


_TERMINAL_STATES = ("noop", "denied", "erased", "expired", "orphaned")
"""Every receipt state that is not ``active``. All five suppress a later replay with
the same single word: telling them apart would report whether a source was once
accepted and then erased, which is the fact an erasure removes."""


@pytest.mark.parametrize("state", _TERMINAL_STATES)
def test_every_terminal_receipt_state_suppresses_a_replay(
    memory: MemoryWorkspace, state: str
) -> None:
    unit = _unit()
    accepted = _accept(memory, unit)
    assert accepted.state == "active"
    with memory.unit() as uow:
        receipt = uow.connection.execute(
            select(memory_tables.source_receipt.c.source_namespace)
        ).scalar_one()
        assert set_source_receipt_state(
            uow.connection,
            "memory",
            str(receipt),
            unit.external_source_key,
            state=state,
        )
    after = memory.counts()

    assert _accept(memory, unit) == AcceptOutcome("source_unavailable", None)
    assert memory.counts() == after


def test_an_active_receipt_whose_representation_is_gone_is_source_unavailable(
    memory: MemoryWorkspace,
) -> None:
    """ "Noncurrent" is a state of the *row*, not of the receipt.

    The receipt still says ``active``; the memory it names has been physically
    removed. Replay must not recreate it — that is the resurrection the whole receipt
    design exists to prevent.
    """
    unit = _unit()
    accepted = _accept(memory, unit)
    assert accepted.memory_ref is not None
    with memory.unit() as uow:
        uow.connection.execute(
            memory_tables.memory.delete().where(
                memory_tables.memory.c.id == RecordRef.parse(accepted.memory_ref).id
            )
        )
    after = memory.counts()

    assert _accept(memory, unit) == AcceptOutcome("source_unavailable", None)
    assert memory.counts() == after


def test_unverifiable_authority_writes_no_receipt_at_all(
    memory: MemoryWorkspace,
) -> None:
    """A unit whose authority cannot be established cannot poison a partition.

    Both shapes: an adapter that refuses outright, and one whose grant disagrees with
    the unit about who the producer is. Neither writes a receipt, because recording an
    outcome against a key this caller has not proved it holds is how one producer
    terminalizes another's identity.
    """
    unit = _unit()
    before = memory.counts()
    assert _accept(memory, unit, authority=SyntheticAuthority(None)) == AcceptOutcome(
        "authority_unverified", None
    )
    assert _accept(
        memory, unit, authority=_granting(memory, unit, authority_id=uuid7())
    ) == AcceptOutcome("authority_unverified", None)
    assert _accept(
        memory, unit, authority=_granting(memory, unit, workspace_id=uuid7())
    ) == AcceptOutcome("authority_unverified", None)
    assert memory.counts() == before


def test_a_verified_unit_that_fails_source_policy_earns_a_content_free_receipt(
    memory: MemoryWorkspace,
) -> None:
    """A verified caller failing policy is ``denied`` — with a receipt, no memory.

    Four failures, each a different sentence of § A5: a purpose that is not the
    ratified automatic binding, an audience above the verified ceiling, a reversed
    source window, and a required canonical link the unit omits.
    """
    now = datetime.now(UTC)
    account = memory.owner_account_id
    cases: tuple[tuple[str, TrustedSourceUnit, object], ...] = (
        (
            "another purpose",
            _unit(bound_purpose=ContextPurpose.RESPOND),
            None,
        ),
        (
            "an audience above the ceiling",
            _unit(audience_kind="workspace", audience_id=None),
            "ceiling",
        ),
        (
            "a reversed window",
            _unit(
                source_recorded_at=now + timedelta(days=1),
                source_expires_at=now + timedelta(days=2),
            ),
            None,
        ),
        (
            "a missing required link",
            _unit(),
            "links",
        ),
    )
    for label, unit, variant in cases:
        before = memory.counts()
        if variant == "ceiling":
            authority: object = _granting(
                memory, unit, audience_kind="member", audience_id=account
            )
        elif variant == "links":
            authority = _granting(
                memory,
                unit,
                required_links=(
                    SourceLink(ref=f"{_FOREIGN}:{uuid7()}", relation="about"),
                ),
            )
        else:
            authority = _granting(memory, unit)
        outcome = _accept(memory, unit, authority=authority, now=now)
        assert outcome == AcceptOutcome("denied", None), label
        after = memory.counts()
        assert after["memory"] == before["memory"], label
        assert after["receipt"] == before["receipt"] + 1, label


def test_a_closed_source_window_is_expired_rather_than_denied(
    memory: MemoryWorkspace,
) -> None:
    """A window that simply ran out is a different fact from a malformed one, and the
    receipt records which."""
    now = datetime.now(UTC)
    unit = _unit(
        source_recorded_at=now - timedelta(hours=2),
        source_expires_at=now - timedelta(hours=1),
    )
    assert _accept(memory, unit, now=now) == AcceptOutcome("expired", None)
    assert _receipts(memory) == [(unit.external_source_key, "expired", False)]


def test_evidence_with_nothing_explicit_in_it_is_a_noop(
    memory: MemoryWorkspace,
) -> None:
    """Empty or insufficiently explicit evidence is a conservative no-op, not a
    denial: nothing was wrong with the caller, there was simply nothing to record."""
    unit = _unit(evidence={"title": "   ", "body": "   "})
    assert _accept(memory, unit) == AcceptOutcome("noop", None)
    assert _receipts(memory) == [(unit.external_source_key, "noop", False)]


def test_acceptance_fails_closed_on_a_missing_or_corrupt_retention_policy(
    memory: MemoryWorkspace,
) -> None:
    """AC 8 binds the acceptance recheck exactly as it binds ``remember``/``derive``.

    Not even a receipt is written: the refusal lands before anything this seam could
    record, which is what "before any write" has to mean for a path whose *purpose*
    is writing an outcome row.

    **While the gate is on**, like every other ``retention_unavailable``: with it off
    the window row is not read at all, so acceptance carries on — the case below.
    """
    memory.expire_by_age()
    before = memory.counts()
    for value in _UNUSABLE_RETENTION:
        memory.set_retention(value)
        with pytest.raises(OperationRefused) as raised:
            _accept(memory, _unit())
        assert raised.value.state == "retention_unavailable"
    assert memory.counts() == before


def test_acceptance_copies_the_units_canonical_links_and_its_mentions(
    memory: MemoryWorkspace,
) -> None:
    """ "Copies all actual canonical source/about restrictions", re-authorized here.

    A link the accepting caller cannot resolve is not a restriction anybody can check,
    so a unit carrying one is denied rather than creating a memory nothing could ever
    read. Mentions are always creations: a trusted producer that could *select* an
    entity would have had to probe for one.
    """
    with memory.unit() as uow:
        readable = _write(uow.connection, _row(title="a readable source"))
    reference = memory_reference(readable.id)
    unit = _unit(
        evidence={
            "title": "linked",
            "body": "an explicitly stated fact",
            "links": (SourceLink(ref=reference, relation="about"),),
            "mentions": (
                SourceMention(kind="person", name="Ada", role="author"),
                SourceMention(kind="person", name="Ada"),
            ),
        }
    )
    outcome = _accept(memory, unit)
    assert outcome.state == "active" and outcome.memory_ref is not None
    assert _links_of(memory, outcome.memory_ref) == {(reference, "about")}
    with memory.reading() as uow:
        names = (
            uow.connection.execute(select(memory_tables.memory_entity.c.name))
            .scalars()
            .all()
        )
    assert list(names) == ["Ada", "Ada"], "one unit, two mentions, two fresh entities"

    unreadable = _unit(
        evidence={
            "title": "blocked",
            "body": "an explicitly stated fact",
            "links": (SourceLink(ref=f"{_FOREIGN}:{uuid7()}", relation="about"),),
        }
    )
    assert _accept(memory, unreadable).state == "denied"


def test_the_validated_importer_adapter_preserves_its_rows_purposes(
    memory: MemoryWorkspace,
) -> None:
    """The second of this run's two adapters, and the one asymmetry that matters.

    A ``migration`` unit is not an automatic producer: it may be unbound and it
    preserves each imported memory's own ratified purposes, under the import
    contract that validated the whole batch before this seam saw it.
    """
    unit = _unit(
        producer_kind="migration",
        bound_purpose=None,
        evidence={
            "title": "an imported memory",
            "body": "an explicitly stated fact",
            "purposes": (ContextPurpose.RESPOND, ContextPurpose.FOLLOW_UP),
        },
    )
    outcome = _accept(
        memory, unit, authority=ValidatedImporterAuthority(memory.workspace)
    )
    assert outcome.state == "active" and outcome.memory_ref is not None
    with memory.reading() as uow:
        stored = list_memory_purposes(
            uow.connection, RecordRef.parse(outcome.memory_ref).id
        )
    assert stored == (_FOLLOW_UP, _RESPOND)

    # And an automatic producer presenting the same purposes is denied before a write.
    automatic = _unit(
        evidence={
            "title": "an automatic memory",
            "body": "an explicitly stated fact",
            "purposes": (ContextPurpose.RESPOND,),
        }
    )
    assert _accept(memory, automatic).state == "denied"


def test_retirement_writes_a_terminal_receipt_and_never_touches_a_live_row(
    memory: MemoryWorkspace,
) -> None:
    """§ A5's retirement rule, both halves.

    For a live representation the seam writes **nothing** and says so: erasing a live
    memory is a per-action-approved deletion under the original authorized caller, and
    an acceptance lease is not erasure authority. For a unit with no live
    representation it writes a content-free terminal receipt, so a later replay of the
    same key cannot resurrect what the source withdrew.
    """
    unit = _unit()
    accepted = _accept(memory, unit)
    assert accepted.memory_ref is not None
    authority = _granting(memory, unit)
    before = memory.counts()

    with memory.unit() as uow:
        live = retire_source_unit(memory.context(), uow, unit, authority=authority)
    assert live.state == LIVE_REPRESENTATION
    assert live.memory_ref == accepted.memory_ref
    assert memory.counts() == before
    assert _receipts(memory) == [(unit.external_source_key, "active", True)]

    with memory.unit() as uow:
        uow.connection.execute(
            memory_tables.memory.delete().where(
                memory_tables.memory.c.id == RecordRef.parse(accepted.memory_ref).id
            )
        )
    with memory.unit() as uow:
        retired = retire_source_unit(memory.context(), uow, unit, authority=authority)
    assert retired.state == "erased"
    # The original id is retained as a pre-creation tombstone, which is the only
    # evidence left that the identity ever had a representation.
    assert _receipts(memory) == [(unit.external_source_key, "erased", True)]
    assert _accept(memory, unit) == AcceptOutcome("source_unavailable", None)

    unknown = _unit()
    with memory.unit() as uow:
        assert (
            retire_source_unit(
                memory.context(), uow, unknown, authority=_granting(memory, unknown)
            ).state
            == "erased"
        )
    assert (unknown.external_source_key, "erased", False) in _receipts(memory)


def test_a_source_receipt_carries_only_identity_authority_and_outcome(
    memory: MemoryWorkspace,
) -> None:
    """AC 2: opaque partitioned identity, and no source content anywhere on the row.

    The namespace is minted at the boundary and is not the caller's key, not the
    workspace id, and not anything a caller could have chosen — a producer that could
    pick it could write into another producer's partition.
    """
    unit = _unit(evidence={"title": "a secret title", "body": "a secret body"})
    assert _accept(memory, unit).state == "active"
    with memory.reading() as uow:
        row = (
            uow.connection.execute(select(memory_tables.source_receipt))
            .mappings()
            .one()
        )
    namespace = str(row["source_namespace"])
    assert namespace != unit.external_source_key
    assert str(memory.workspace) not in namespace
    assert len(namespace) <= 128
    stored = " ".join(str(value) for value in row.values())
    for secret in ("a secret title", "a secret body", "Ada"):
        assert secret not in stored
    assert len(bytes(row["payload_digest"])) == 32
    assert row["bound_purpose"] == _INTERNAL
    assert row["producer_kind"] == "rheo_runtime"
