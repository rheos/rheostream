"""The predecessor's SQLite snapshot, opened read-only and immutable (spec § System
Components 1).

:func:`open_snapshot` is the only way in. It builds the
``file:<path>?mode=ro&immutable=1`` URI itself and refuses every other open mode,
and refuses a caller-supplied ``file:`` URI outright, since a URI's own query string
could ask for ``mode=rw``. ``immutable=1`` also stops SQLite creating ``-wal`` or
``-shm`` files beside the snapshot.

Row reads go through :func:`select_column`, which selects only from the tables the
mapping names in :data:`SELECTABLE_TABLES` (later phases add to it) and refuses
``app_secret`` by name before anything else, so no code path here can select from it.
Schema introspection (``sqlite_master``, ``pragma_table_info``,
``pragma_foreign_key_list``) reads structure only and covers every table,
``app_secret`` included, because the inventory counts it.
"""

import os
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

READ_ONLY_MODE: Final = "ro"

#: The one table no reader ever selects rows from (spec § Data Models, "counted,
#: never selected").
NEVER_SELECTED_TABLE: Final = "app_secret"

#: The tables a row reader may select from. Only what a mapping or inventory step
#: needs today: ``conversation`` and ``session_digest`` for their ``session_id``
#: (sessions with turns and no digest), ``procedural_notes`` for the owner-only
#: historical extract, and ``entity_vec_rowids`` (the ``entity_vec`` vec0 table's
#: own shadow) for the entity ids behind its ghost rows.
SELECTABLE_TABLES: Final = frozenset(
    {
        "memory_items",
        "conversation",
        "session_digest",
        "procedural_notes",
        "entity_vec_rowids",
    }
)

SNAPSHOT_MODE_REFUSED = "snapshot_mode_refused"
SNAPSHOT_URI_REFUSED = "snapshot_uri_refused"
SNAPSHOT_MISSING = "snapshot_missing"
SNAPSHOT_UNREADABLE = "snapshot_unreadable"
TABLE_NEVER_SELECTED = "table_never_selected"
TABLE_NOT_SELECTABLE = "table_not_selectable"
COLUMN_UNKNOWN = "column_unknown"

_VIRTUAL_TABLE = re.compile(
    r"\s*CREATE\s+VIRTUAL\s+TABLE\s+.*?\bUSING\s+(\w+)", re.IGNORECASE | re.DOTALL
)


class SnapshotRefusal(Exception):
    """Raised when an open or a read of the snapshot falls outside the rules."""

    def __init__(self, state: str, detail: str) -> None:
        super().__init__(f"{state}: {detail}")
        self.state = state
        self.detail = detail


@dataclass(frozen=True)
class SchemaObject:
    type: str
    name: str
    table_name: str
    #: The module a ``CREATE VIRTUAL TABLE ... USING <module>`` names (``vec0``,
    #: ``fts5``), else ``None``. A virtual table is never queried directly: its
    #: module (sqlite-vec) is not loaded, so it is counted through its shadows.
    virtual_module: str | None = None


@dataclass(frozen=True)
class Column:
    position: int
    name: str
    declared_type: str
    not_null: bool
    primary_key: bool


@dataclass(frozen=True)
class ForeignKey:
    from_column: str
    target_table: str
    target_column: str | None


def open_snapshot(
    path: os.PathLike[str] | str, *, mode: str = READ_ONLY_MODE
) -> sqlite3.Connection:
    """Open ``path`` as ``file:<path>?mode=ro&immutable=1``, or refuse.

    ``mode`` exists so that asking for anything else is an explicit refusal rather
    than a silently ignored argument.
    """
    if mode != READ_ONLY_MODE:
        raise SnapshotRefusal(
            SNAPSHOT_MODE_REFUSED,
            f"the snapshot opens read-only and immutable only, not mode={mode!r}",
        )
    if str(path).startswith("file:"):
        raise SnapshotRefusal(
            SNAPSHOT_URI_REFUSED,
            "pass a filesystem path; the read-only URI is built here, never supplied",
        )
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as error:
        raise SnapshotRefusal(
            SNAPSHOT_MISSING, f"no snapshot file at {path}"
        ) from error
    if not resolved.is_file():
        raise SnapshotRefusal(SNAPSHOT_MISSING, f"not a regular file: {resolved}")
    # immutable=1 cannot see committed pages left in the WAL. A caller must
    # checkpoint/copy the source first; opening it here must never lose those rows.
    wal = resolved.with_name(resolved.name + "-wal")
    try:
        wal_size = wal.stat().st_size
    except FileNotFoundError:
        wal_size = 0
    except OSError as error:
        raise SnapshotRefusal(
            SNAPSHOT_UNREADABLE, "cannot verify the snapshot's WAL sidecar"
        ) from error
    if wal_size:
        raise SnapshotRefusal(
            SNAPSHOT_UNREADABLE,
            "non-empty WAL sidecar; provide a checkpointed snapshot copy first",
        )
    # as_uri() percent-encodes '?' and '#', so the path cannot extend the query.
    uri = f"{resolved.as_uri()}?mode=ro&immutable=1"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise SnapshotRefusal(
            SNAPSHOT_UNREADABLE, f"cannot open the snapshot {resolved}"
        ) from error


def schema_objects(connection: sqlite3.Connection) -> list[SchemaObject]:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    objects = []
    for kind, name, table, sql in rows:
        match = _VIRTUAL_TABLE.match(sql) if isinstance(sql, str) else None
        module = match.group(1).lower() if match else None
        objects.append(SchemaObject(str(kind), str(name), str(table), module))
    return objects


def row_count(connection: sqlite3.Connection, table: str) -> int:
    """``count(*)`` of a table or view: a count, never a value, so it covers
    ``app_secret`` too. Refuses a virtual table, whose module may not be loaded."""
    matches = [
        obj
        for obj in schema_objects(connection)
        if obj.name == table and obj.type in ("table", "view")
    ]
    if not matches:
        raise SnapshotRefusal(TABLE_NOT_SELECTABLE, f"no table named {table!r}")
    if matches[0].virtual_module is not None:
        raise SnapshotRefusal(
            TABLE_NOT_SELECTABLE, f"{table!r} is virtual; count its shadow tables"
        )
    (count,) = connection.execute(f"SELECT count(*) FROM {_quoted(table)}").fetchone()
    return int(count)


def _require_table(connection: sqlite3.Connection, table: str) -> None:
    names = {
        obj.name for obj in schema_objects(connection) if obj.type in ("table", "view")
    }
    if table not in names:
        raise SnapshotRefusal(TABLE_NOT_SELECTABLE, f"no table named {table!r}")


def table_columns(connection: sqlite3.Connection, table: str) -> list[Column]:
    _require_table(connection, table)
    rows = connection.execute(
        'SELECT cid, name, type, "notnull", pk FROM pragma_table_info(?)', (table,)
    ).fetchall()
    return [
        Column(int(cid), str(name), str(kind), bool(not_null), bool(pk))
        for cid, name, kind, not_null, pk in rows
    ]


def table_foreign_keys(connection: sqlite3.Connection, table: str) -> list[ForeignKey]:
    _require_table(connection, table)
    rows = connection.execute(
        'SELECT "from", "table", "to" FROM pragma_foreign_key_list(?) ORDER BY id, seq',
        (table,),
    ).fetchall()
    return [
        ForeignKey(str(source), str(target), None if column is None else str(column))
        for source, target, column in rows
    ]


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def select_columns(
    connection: sqlite3.Connection, table: str, columns: Sequence[str]
) -> list[tuple[object, ...]]:
    """Every row's ``columns``, in rowid order, from a selectable table."""
    if table == NEVER_SELECTED_TABLE:
        raise SnapshotRefusal(
            TABLE_NEVER_SELECTED, f"{NEVER_SELECTED_TABLE} is counted, never selected"
        )
    if table not in SELECTABLE_TABLES:
        raise SnapshotRefusal(
            TABLE_NOT_SELECTABLE, f"{table!r} is not a table any mapping step reads"
        )
    if not columns:
        raise SnapshotRefusal(COLUMN_UNKNOWN, f"no column named for {table}")
    known = {col.name for col in table_columns(connection, table)}
    for column in columns:
        if column not in known:
            raise SnapshotRefusal(COLUMN_UNKNOWN, f"{table}.{column} does not exist")
    selected = ", ".join(_quoted(column) for column in columns)
    rows = connection.execute(
        f"SELECT {selected} FROM {_quoted(table)} ORDER BY rowid"
    ).fetchall()
    return [tuple(row) for row in rows]


def select_column(
    connection: sqlite3.Connection, table: str, column: str
) -> list[object]:
    """Every value of ``table.column``, in rowid order, from a selectable table."""
    return [value for (value,) in select_columns(connection, table, (column,))]


def memory_item_labels(connection: sqlite3.Connection) -> list[str]:
    """Every ``memory_items`` label, live or superseded (the denylist wants all)."""
    return [
        value
        for value in select_column(connection, "memory_items", "label")
        if isinstance(value, str)
    ]
