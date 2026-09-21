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
from datetime import UTC, datetime, timedelta
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
    Idempotency,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.factories import context_from_operation
from rheo_core.migrations.module_chain import run_module_chain
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.operations.registry import OperationRegistry
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import RecordHead, Unavailable, resolve_in
from rheo_core.settings import ValueType
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.provisioning import core_version
from rheo_core.storage.repositories import (
    insert_module_state,
    upsert_workspace_setting,
)
from rheo_recallatron import MANIFEST
from rheo_recallatron import eligibility as memory_eligibility
from rheo_recallatron.configuration import RETENTION_DAYS_KEY
from rheo_recallatron.eligibility import (
    Denied,
    MemoryRequest,
    ReadMode,
    ReferenceBudget,
    begin_request,
    contact_permitted,
    eligible_memory,
    memory_reference,
)
from rheo_recallatron.operations import MEMORY_READ, MEMORY_RECALL, ReadWindow
from rheo_recallatron.storage import tables as memory_tables
from rheo_recallatron.storage.repository import (
    MemoryLinkRow,
    MemoryPurposeRow,
    MemoryRow,
    insert_memory,
    insert_memory_link,
    insert_memory_purpose,
)
from sqlalchemy import Connection, Engine, insert, text

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
        return dispatch(ctx, name, payload, registry=self.surfaces.operations)

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
            retention_days=365,
            budget=ReferenceBudget(limit=1),
        )
        decision = eligible_memory(
            owner, uow, derived.id, mode=ReadMode.CURRENT, request=spent
        )
        assert decision == Denied("reference_scan_limit")

        shallow = MemoryRequest(
            owner,
            now=datetime.now(UTC),
            retention_days=365,
            budget=ReferenceBudget(depth_limit=0),
        )
        assert eligible_memory(
            owner, uow, derived.id, mode=ReadMode.CURRENT, request=shallow
        ) == Denied("reference_scan_limit")

        roomy = MemoryRequest(owner, now=datetime.now(UTC), retention_days=365)
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

_UNUSABLE_RETENTION = (None, "0", "4000", "-1", "not-a-number", "")
"""Absent, below the minimum, above the maximum, negative, unparseable, empty.

Every one of them is a row a reader must refuse rather than fall back from. ``None``
deletes the row entirely, which is the case the settings resolver would answer with the
365-day package default.
"""


def test_read_refuses_a_missing_or_corrupt_retention_policy(
    memory: MemoryWorkspace,
) -> None:
    reference, members = _container_with(memory, 3)
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
