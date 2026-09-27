"""The source-class inventory (spec § System Components 2, FR 1, AC 1).

:func:`build_inventory` reads the snapshot's schema through ``sqlite_source`` (every
table and view in ``sqlite_master``: row count, columns, types, foreign keys), counts
each virtual table through its shadow tables without querying the virtual table
itself (sqlite-vec is never loaded), and adds the figures the ledger and the cutover
lean on: the ``entity_vec`` ghost-row count, graph op and type counts, the ontology
directory's files and sizes, the ``memory_items`` / graph normalized-label overlap,
and the sessions holding conversation turns with no ``session_digest`` row. Each
class carries its ledger scope and a content-free reason.

The inventory holds counts, names and schema only, never a row's content. It still
describes the real snapshot, so it is written only under the private root.
"""

import json
import os
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from rheo_recallatron.entities import normalized_name
from rheo_recallatron.migration.predecessor import graph_source, sqlite_source

INVENTORY_FORMAT: Final = "rheo.migration.inventory"
INVENTORY_VERSION: Final = 1
INVENTORY_JSON_NAME: Final = "inventory.json"
INVENTORY_MARKDOWN_NAME: Final = "inventory.md"

IN_SCOPE: Final = "in_scope"
INVENTORY_ONLY: Final = "inventory_only"
#: A class neither list names: surfaced so a table the spec never saw is decided by a
#: person, not silently dropped into a bucket.
UNCLASSIFIED: Final = "unclassified"

#: Tables whose every row gets a ledger row (spec § Data Models, mapping rules).
_LEDGER_TABLES: Final = frozenset(
    {
        "memory_items",
        "conversation",
        "session_digest",
        "procedural_notes",
        "surfaced_ledger",
        "topic_thread",
        "topic_thread_session",
    }
)
#: Inventory-only tables and their reasons (spec § Data Models, inventory-only).
_INVENTORY_ONLY_TABLES: Final = {
    "tool_call_log": "telemetry_harvested_separately",
    "ticket": "out_of_scope",
    "comment": "out_of_scope",
    "classification_audit": "out_of_scope",
    "app_secret": "counted_never_selected",
    "vec_meta": "derived_index",
    "sqlite_sequence": "schema_metadata",
    # The predecessor's ORM migration journal. Its second bookkeeping table carries
    # the predecessor's own name, which stays out of public code (spec § Data Models,
    # inventory-only): it lands ``unclassified`` and is named only in private output.
    "__drizzle_migrations": "schema_metadata",
}
_OUT_OF_SCOPE_PREFIX: Final = "ticket_fts"
#: The virtual-table modules whose tables (and shadow tables) are derived indexes:
#: sqlite-vec and SQLite full-text search. Any other module is ``unclassified``.
_DERIVED_INDEX_MODULES: Final = frozenset({"vec0", "fts5"})

_PROFILE_PREFIX: Final = "profile.synth."
_MAINTAINER_STATUS: Final = "maintainer-status.json"

_ENTITY_VEC_ROWIDS: Final = "entity_vec_rowids"
_VEC0: Final = "vec0"


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    declared_type: str
    not_null: bool
    primary_key: bool


@dataclass(frozen=True)
class ForeignKeyInfo:
    from_column: str
    target_table: str
    target_column: str | None


@dataclass(frozen=True)
class SchemaEntry:
    name: str
    type: str
    virtual_module: str | None
    #: The virtual table this is a shadow of, when it is one.
    shadow_of: str | None
    #: ``None`` for a virtual table (see ``shadow_row_counts``) or an uncountable view.
    row_count: int | None
    #: ``None`` for a virtual table, whose module is not loaded to describe it.
    columns: tuple[ColumnInfo, ...] | None
    foreign_keys: tuple[ForeignKeyInfo, ...]
    #: A virtual table's shadow tables and their row counts.
    #: A shadow whose count failed stays ``None`` (unknown), never 0.
    shadow_row_counts: dict[str, int | None]
    ledger_scope: str
    scope_reason: str


@dataclass(frozen=True)
class GraphCounts:
    entity_lines: int
    live_entities: int
    entity_types: dict[str, int]
    live_entity_types: dict[str, int]
    ops: dict[str, int]
    malformed_lines: int
    unknown_op_lines: int


@dataclass(frozen=True)
class OntologyFile:
    name: str
    size_bytes: int
    ledger_scope: str
    scope_reason: str


@dataclass(frozen=True)
class Inventory:
    format: str
    version: int
    objects: tuple[SchemaEntry, ...]
    graph: GraphCounts
    ontology_files: tuple[OntologyFile, ...]
    #: Rowids of ``entity_vec`` whose entity id is no live graph entity; ``None``
    #: when the snapshot has no ``entity_vec_rowids`` shadow.
    entity_vec_ghost_rows: int | None
    #: Live (unsuperseded) ``memory_items`` rows whose normalized label equals a live
    #: graph entity's; ``None`` when the snapshot has no ``memory_items``.
    memory_items_graph_label_overlap: int | None
    #: Distinct sessions with conversation turns and no ``session_digest`` row;
    #: ``None`` when either table is absent.
    sessions_without_digest: int | None


def _table_scope(name: str, index_module: str | None) -> tuple[str, str]:
    """``index_module`` is the virtual module of the table itself, or of the virtual
    table it is a shadow of; ``None`` for an ordinary table or view."""
    if name.startswith(_OUT_OF_SCOPE_PREFIX):
        return INVENTORY_ONLY, "out_of_scope"
    if index_module is not None:
        if index_module in _DERIVED_INDEX_MODULES:
            return INVENTORY_ONLY, "derived_index"
        return UNCLASSIFIED, "unknown_virtual_module"
    if name in _LEDGER_TABLES:
        return IN_SCOPE, "ledger_rows"
    if name in _INVENTORY_ONLY_TABLES:
        return INVENTORY_ONLY, _INVENTORY_ONLY_TABLES[name]
    return UNCLASSIFIED, "not_in_spec"


def _shadow_owner(name: str, virtual_names: list[str]) -> str | None:
    """The longest virtual-table name ``name`` extends with ``_<suffix>``."""
    owners = [v for v in virtual_names if name != v and name.startswith(f"{v}_")]
    return max(owners, key=len) if owners else None


def _schema_entries(connection: sqlite3.Connection) -> tuple[SchemaEntry, ...]:
    objects = [
        obj
        for obj in sqlite_source.schema_objects(connection)
        if obj.type in ("table", "view")
    ]
    modules = {
        obj.name: obj.virtual_module
        for obj in objects
        if obj.virtual_module is not None
    }
    virtual_names = list(modules)
    shadows = {
        obj.name: owner
        for obj in objects
        if obj.virtual_module is None
        and (owner := _shadow_owner(obj.name, virtual_names)) is not None
    }
    counts: dict[str, int | None] = {}
    for obj in objects:
        if obj.virtual_module is not None:
            continue
        try:
            counts[obj.name] = sqlite_source.row_count(connection, obj.name)
        except sqlite3.OperationalError:
            counts[obj.name] = None  # a view over a table whose module is not loaded
    entries = []
    for obj in sorted(objects, key=lambda o: o.name):
        virtual = obj.virtual_module is not None
        shadow_counts = {
            shadow: counts[shadow]
            for shadow, owner in sorted(shadows.items())
            if owner == obj.name
        }
        owner = shadows.get(obj.name)
        scope = _table_scope(
            obj.name, obj.virtual_module or (modules[owner] if owner else None)
        )
        if virtual:
            columns: tuple[ColumnInfo, ...] | None = None
            keys: tuple[ForeignKeyInfo, ...] = ()
            rowids = f"{obj.name}_rowids"
            row_count = (
                shadow_counts.get(rowids) if obj.virtual_module == _VEC0 else None
            )
        else:
            try:
                columns = tuple(
                    ColumnInfo(c.name, c.declared_type, c.not_null, c.primary_key)
                    for c in sqlite_source.table_columns(connection, obj.name)
                )
                keys = tuple(
                    ForeignKeyInfo(k.from_column, k.target_table, k.target_column)
                    for k in sqlite_source.table_foreign_keys(connection, obj.name)
                )
            except sqlite3.OperationalError:
                columns, keys = None, ()  # the same view whose count failed
            row_count = counts[obj.name]
        entries.append(
            SchemaEntry(
                name=obj.name,
                type=obj.type,
                virtual_module=obj.virtual_module,
                shadow_of=shadows.get(obj.name),
                row_count=row_count,
                columns=columns,
                foreign_keys=keys,
                shadow_row_counts=shadow_counts,
                ledger_scope=scope[0],
                scope_reason=scope[1],
            )
        )
    return tuple(entries)


def _graph_counts(parsed: graph_source.ParsedGraph) -> GraphCounts:
    live = graph_source.live_entities(parsed)

    def types(lines: list[graph_source.EntityLine]) -> dict[str, int]:
        tally = Counter(str(line.record.get("type")) for line in lines)
        return dict(sorted(tally.items()))

    return GraphCounts(
        entity_lines=len(parsed.entity_lines),
        live_entities=len(live),
        entity_types=types(list(parsed.entity_lines)),
        live_entity_types=types(live),
        ops=dict(sorted(Counter(line.op for line in parsed.op_lines).items())),
        malformed_lines=len(parsed.malformed_line_numbers),
        unknown_op_lines=len(parsed.unknown_op_line_numbers),
    )


def _ontology_scope(name: str) -> tuple[str, str]:
    if name == graph_source.GRAPH_FILE_NAME:
        return IN_SCOPE, "ledger_rows"
    if name.startswith(_PROFILE_PREFIX):
        return IN_SCOPE, "one_class_row"
    if name == _MAINTAINER_STATUS:
        return INVENTORY_ONLY, "operational_state"
    return UNCLASSIFIED, "not_in_spec"


def _raise_listing_error(error: OSError) -> None:
    raise error


def _ontology_files(ontology_dir: Path) -> tuple[OntologyFile, ...]:
    """Every file under ``ontology_dir``. A directory that cannot be listed raises
    (``os.walk`` would otherwise skip it and the inventory would be silently short)."""
    files = []
    for root, _dirs, names in os.walk(ontology_dir, onerror=_raise_listing_error):
        for name in names:
            path = Path(root) / name
            relative = path.relative_to(ontology_dir).as_posix()
            scope = _ontology_scope(relative)
            files.append(OntologyFile(relative, path.stat().st_size, *scope))
    return tuple(sorted(files, key=lambda f: f.name))


def _table_names(entries: tuple[SchemaEntry, ...]) -> set[str]:
    return {entry.name for entry in entries if entry.virtual_module is None}


def _ghost_rows(
    connection: sqlite3.Connection, tables: set[str], live_ids: set[str]
) -> int | None:
    if _ENTITY_VEC_ROWIDS not in tables:
        return None
    ids = sqlite_source.select_column(connection, _ENTITY_VEC_ROWIDS, "id")
    return sum(1 for entity_id in ids if entity_id not in live_ids)


def _label_overlap(
    connection: sqlite3.Connection, tables: set[str], live_labels: set[str]
) -> int | None:
    if "memory_items" not in tables:
        return None
    rows = sqlite_source.select_columns(
        connection, "memory_items", ("label", "superseded_by")
    )
    return sum(
        1
        for label, superseded_by in rows
        if superseded_by is None
        and isinstance(label, str)
        and normalized_name(label) in live_labels
    )


def _sessions_without_digest(
    connection: sqlite3.Connection, tables: set[str]
) -> int | None:
    if not {"conversation", "session_digest"} <= tables:
        return None
    turns = set(sqlite_source.select_column(connection, "conversation", "session_id"))
    digests = set(
        sqlite_source.select_column(connection, "session_digest", "session_id")
    )
    return len(turns - digests)


def build_inventory(
    connection: sqlite3.Connection,
    graph: graph_source.ParsedGraph,
    ontology_dir: os.PathLike[str] | str,
) -> Inventory:
    entries = _schema_entries(connection)
    tables = _table_names(entries)
    live = graph_source.live_entities(graph)
    return Inventory(
        format=INVENTORY_FORMAT,
        version=INVENTORY_VERSION,
        objects=entries,
        graph=_graph_counts(graph),
        ontology_files=_ontology_files(Path(ontology_dir)),
        entity_vec_ghost_rows=_ghost_rows(
            connection, tables, {head.entity_id for head in live}
        ),
        memory_items_graph_label_overlap=_label_overlap(
            connection,
            tables,
            {
                normalized_name(label)
                for label in graph_source.live_entity_labels(graph)
            },
        ),
        sessions_without_digest=_sessions_without_digest(connection, tables),
    )


def render_json(inventory: Inventory) -> str:
    return json.dumps(asdict(inventory), indent=2, sort_keys=False) + "\n"


def _figure(value: int | None) -> str:
    return "n/a (source absent)" if value is None else str(value)


def render_markdown(inventory: Inventory) -> str:
    lines = [
        "# Predecessor source inventory",
        "",
        "## Snapshot tables and views",
        "",
        "| Name | Type | Rows | Columns | Foreign keys | Scope | Reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for entry in inventory.objects:
        kind = (
            entry.type
            if entry.virtual_module is None
            else (f"virtual ({entry.virtual_module})")
        )
        if entry.shadow_of is not None:
            kind = f"shadow of {entry.shadow_of}"
        rows = "n/a" if entry.row_count is None else str(entry.row_count)
        columns = (
            "n/a"
            if entry.columns is None
            else ", ".join(f"{c.name} {c.declared_type}".strip() for c in entry.columns)
        )
        keys = ", ".join(
            f"{k.from_column} -> {k.target_table}.{k.target_column or '?'}"
            for k in entry.foreign_keys
        )
        lines.append(
            f"| {entry.name} | {kind} | {rows} | {columns} | {keys} | "
            f"{entry.ledger_scope} | {entry.scope_reason} |"
        )
    graph = inventory.graph
    lines += [
        "",
        "## Ontology graph",
        "",
        f"- Entity lines: {graph.entity_lines}; live entities: {graph.live_entities}",
        "- Entity types (all lines): "
        + (", ".join(f"{k} {v}" for k, v in graph.entity_types.items()) or "none"),
        "- Entity types (live heads): "
        + (", ".join(f"{k} {v}" for k, v in graph.live_entity_types.items()) or "none"),
        "- Ops: " + (", ".join(f"{k} {v}" for k, v in graph.ops.items()) or "none"),
        f"- Malformed lines: {graph.malformed_lines}; unknown-op lines: "
        f"{graph.unknown_op_lines}",
        "",
        "## Ontology files",
        "",
        "| File | Bytes | Scope | Reason |",
        "|---|---|---|---|",
        *(
            f"| {f.name} | {f.size_bytes} | {f.ledger_scope} | {f.scope_reason} |"
            for f in inventory.ontology_files
        ),
        "",
        "## Cross-source figures",
        "",
        "- `entity_vec` ghost rows (no live graph entity): "
        + _figure(inventory.entity_vec_ghost_rows),
        "- Live `memory_items` rows whose normalized label equals a live graph "
        "entity's: " + _figure(inventory.memory_items_graph_label_overlap),
        "- Sessions with conversation turns and no `session_digest` row: "
        + _figure(inventory.sessions_without_digest),
        "",
    ]
    return "\n".join(lines)
