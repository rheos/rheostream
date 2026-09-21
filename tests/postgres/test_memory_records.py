"""AC 2's schema half: the migration creates the ratified schema, and only it.

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

from collections.abc import Iterator
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import create_required_extensions, loaded_probe_modules
from rheo_core.migrations.module_chain import run_module_chain
from rheo_core.storage.provisioning import core_version
from rheo_recallatron import MANIFEST
from sqlalchemy import Connection, Engine, text

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
