"""State export and import: the one way ledger state moves between databases.

The state tables (``derive.run.STATE``: decisions, their items and targets, rates and
their coverage, notices, sink tickets and job runs; ADR-0003's small state store) are
the ledger's inputs besides the document set. Everything else is derived and comes back
with ``finance rebuild``. Core's workspace export carries none of it: finance declares
no exportable record type.

Two operations of the module's own registry, never core's:

- ``finance.state.export`` (READ, owner): per state table its canonical lines, their
  count and the sha256 of the file they make, plus the schema head and the derived
  digest.
- ``finance.state.import`` (MUTATE, owner only): into a database at the same head
  whose state tables are all empty; rows go in in foreign-key order.

A line is one row: every column in table order as ``[name, tagged]``, each value
tagged by ``derive.digest.canonical_value`` (numbers normalized, times in UTC), and a
table's lines are sorted, so one state gives one byte sequence. ``decode_value``
inverts the tags by the column's type.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, Table, select, text

from rheo_finance import db
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.derive.digest import canonical_value, ledger_digest
from rheo_finance.derive.run import STATE
from rheo_finance.operations import OperationError, OperationSpec, register
from rheo_finance.storage.schema import metadata
from rheo_finance.tier import Tier

# A column's Python type and the one scalar tag it is written with.
_TAGS: Final[dict[type, str]] = {
    uuid.UUID: "uuid",
    str: "str",
    Decimal: "num",
    int: "num",
    dt.date: "date",
    dt.datetime: "ts",
    bool: "bool",
}
# The widest integer a state column holds (bigint). A larger one is refused as a bad
# line rather than built in full by int() and refused by the database later.
_INT_MIN: Final = Decimal(-(2**63))
_INT_MAX: Final = Decimal(2**63 - 1)


def state_tables() -> list[Table]:
    """The state tables in foreign-key order (parents first)."""
    tables = [table for table in metadata.sorted_tables if table.name in STATE]
    if {table.name for table in tables} != STATE:
        raise OperationError("state_tables_missing")
    return tables


def _kind(column: Column[object]) -> type:
    """The column's Python type when the export can tag it; else a refusal."""
    try:
        kind = column.type.python_type
    except NotImplementedError:
        kind = object
    timezone = getattr(column.type, "timezone", True)
    if kind not in _TAGS or (kind is dt.datetime and not timezone):
        raise OperationError(f"state_column_untaggable: {column.table.name}")
    return kind


def encode_row(table: Table, row: Sequence[object]) -> str:
    """One row as its canonical line."""
    for column in table.columns:
        _kind(column)
    return json.dumps(
        [
            [column.name, canonical_value(value)]
            for column, value in zip(table.columns, row, strict=True)
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def table_lines(uow: UnitOfWork, table: Table) -> list[str]:
    """Every row of ``table`` as a canonical line, sorted."""
    for column in table.columns:
        _kind(column)
    rows = uow.execute(select(*table.columns))
    return sorted(encode_row(table, tuple(row)) for row in rows)


def file_bytes(lines: Sequence[str]) -> bytes:
    """The bytes of a table's file: each line ends in a newline."""
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def file_digest(lines: Sequence[str]) -> str:
    return hashlib.sha256(file_bytes(lines)).hexdigest()


class LineInvalid(ValueError):
    """A state line or a tagged value does not decode. The message holds no value."""


def decode_value(column: Column[object] | None, tagged: object) -> object:
    """The value ``canonical_value`` tagged, typed by ``column`` (``None`` inside a
    list or a map)."""
    if not (isinstance(tagged, list) and len(tagged) == 2):
        raise LineInvalid("not a tagged value")
    tag, raw = tagged
    kind = None if column is None else _kind(column)
    if kind is not None and tag not in {"none", _TAGS[kind]}:
        raise LineInvalid("tag does not match the column")
    try:
        match tag:
            case "none" if raw is None:
                return None
            case "bool" if isinstance(raw, bool):
                return raw
            case "num" if isinstance(raw, str):
                number = Decimal(raw)
                # canonical_value never writes Infinity or NaN; int() would raise
                # OverflowError or ValueError on them, and Postgres numeric takes both.
                if not number.is_finite():
                    raise LineInvalid("not a finite number")
                if kind is int:
                    if number != number.to_integral_value():
                        raise LineInvalid("not an integer")
                    if not _INT_MIN <= number <= _INT_MAX:
                        raise LineInvalid("integer out of range")
                    return int(number)
                return number
            case "str" if isinstance(raw, str):
                return raw
            case "ts" if isinstance(raw, str):
                instant = dt.datetime.fromisoformat(raw)
                if instant.tzinfo is None:
                    raise LineInvalid("a time without its zone")
                return instant
            case "date" if isinstance(raw, str):
                return dt.date.fromisoformat(raw)
            case "uuid" if isinstance(raw, str):
                return uuid.UUID(raw)
            # No state column holds JSON, so no state line holds a float: refused, as
            # money is never a float here.
            case "list" if isinstance(raw, list):
                return [decode_value(None, each) for each in raw]
            case "map" if isinstance(raw, list):
                return {_key(pair): decode_value(None, pair[1]) for pair in raw}
    except (ValueError, InvalidOperation) as exc:
        if isinstance(exc, LineInvalid):
            raise
        raise LineInvalid("value does not parse") from None
    raise LineInvalid("unknown tag")


def _key(pair: object) -> str:
    if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str):
        return pair[0]
    raise LineInvalid("not a map entry")


def decode_line(table: Table, line: str) -> dict[str, object]:
    """One canonical line of ``table`` as its column values."""
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        raise LineInvalid("not JSON") from None
    columns = list(table.columns)
    if not (isinstance(parsed, list) and len(parsed) == len(columns)):
        raise LineInvalid("wrong column count")
    row: dict[str, object] = {}
    for column, pair in zip(columns, parsed, strict=True):
        if not (isinstance(pair, list) and len(pair) == 2 and pair[0] == column.name):
            raise LineInvalid("columns out of order")
        row[column.name] = decode_value(column, pair[1])
    return row


def _schema_version(uow: UnitOfWork) -> str | None:
    version = uow.scalar(
        text(f"SELECT version_num FROM {db.SCHEMA}.{db.VERSION_TABLE}")
    )
    return None if version is None else str(version)


# --- the export -------------------------------------------------------------------

P, R = Tier.PUBLIC, Tier.RESTRICTED


class ExportIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TableState(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: Annotated[str, Tiered(P)]
    rows: Annotated[int, Tiered(P)]
    sha256: Annotated[str, Tiered(P)]
    lines: Annotated[tuple[str, ...], Tiered(R)]


class ExportOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_head: Annotated[str, Tiered(P)]
    derived_digest: Annotated[str, Tiered(P)]
    tables: Annotated[tuple[TableState, ...], Tiered(P)]


def export_state(_ctx: FinanceContext, uow: UnitOfWork, _data: ExportIn) -> ExportOut:
    head = _schema_version(uow)
    if head is None:
        raise OperationError("schema_unknown: the database records no revision")
    tables = []
    for table in state_tables():
        lines = table_lines(uow, table)
        tables.append(
            TableState(
                name=table.name,
                rows=len(lines),
                sha256=file_digest(lines),
                lines=tuple(lines),
            )
        )
    return ExportOut(
        schema_head=head,
        derived_digest=ledger_digest(uow).digest,
        tables=tuple(tables),
    )


# --- the import -------------------------------------------------------------------


class TableLines(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    lines: tuple[str, ...]


class ImportIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_head: str
    tables: tuple[TableLines, ...]


class TableCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: Annotated[str, Tiered(P)]
    rows: Annotated[int, Tiered(P)]


class ImportOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    tables: Annotated[tuple[TableCount, ...], Tiered(P)]


def _identity_columns(uow: UnitOfWork) -> set[tuple[str, str]]:
    rows = uow.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = :schema AND is_identity = 'YES'"
        ),
        {"schema": db.SCHEMA},
    )
    return {(str(name), str(column)) for name, column in rows}


def _insert(
    uow: UnitOfWork, table: Table, rows: list[dict[str, object]], *, identity: bool
) -> None:
    names = [column.name for column in table.columns]
    columns = ", ".join(f'"{name}"' for name in names)
    values = ", ".join(f":{name}" for name in names)
    overriding = " OVERRIDING SYSTEM VALUE" if identity else ""
    uow.execute(
        text(
            f'INSERT INTO {db.SCHEMA}."{table.name}" ({columns}){overriding}'
            f" VALUES ({values})"
        ),
        rows,
    )


def import_state(_ctx: FinanceContext, uow: UnitOfWork, data: ImportIn) -> ImportOut:
    head = db.head_revision()
    if data.schema_head != head or _schema_version(uow) != head:
        raise OperationError("schema_mismatch: export and import at one head")
    tables = state_tables()
    given = {each.name: each.lines for each in data.tables}
    if len(given) != len(data.tables) or set(given) != {t.name for t in tables}:
        raise OperationError("state_tables_mismatch: every state table, once")
    for table in tables:
        if uow.scalar(select(select(table).exists())):
            raise OperationError("state_not_empty: import into an empty ledger")
    identity = _identity_columns(uow)
    counts = []
    for table in tables:
        rows = []
        for number, line in enumerate(given[table.name], start=1):
            try:
                rows.append(decode_line(table, line))
            except LineInvalid:
                raise OperationError(
                    f"state_line_invalid: {table.name} line {number}"
                ) from None
        generated = sorted(c for t, c in identity if t == table.name)
        if rows:
            _insert(uow, table, rows, identity=bool(generated))
            for column in generated:
                uow.execute(
                    text(
                        "SELECT setval(pg_get_serial_sequence(:table, :column),"
                        f' (SELECT max("{column}") FROM {db.SCHEMA}."{table.name}"))'
                    ),
                    {"table": f"{db.SCHEMA}.{table.name}", "column": column},
                )
        counts.append(TableCount(name=table.name, rows=len(rows)))
    return ImportOut(tables=tuple(counts))


register(
    OperationSpec(
        name="finance.state.export",
        input_model=ExportIn,
        output_model=ExportOut,
        handler=export_state,
        safety_class=SafetyClass.READ,
        roles=frozenset({Role.OWNER}),
        connection="owner",
        description="Export every state table as canonical lines, counts and hashes.",
    )
)
register(
    OperationSpec(
        name="finance.state.import",
        input_model=ImportIn,
        output_model=ImportOut,
        handler=import_state,
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER}),
        connection="owner",
        owner_only=True,
        description="Import exported state into an empty ledger at the same head.",
    )
)
