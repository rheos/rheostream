"""The state line codec (L11): every tag round-trips, by the column's type.

No database: rows are synthetic values on the real state tables' columns.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import Column, MetaData, Table
from sqlalchemy.dialects.postgresql import JSONB

from rheo_finance import catalogue
from rheo_finance.derive.digest import canonical_value
from rheo_finance.derive.run import STATE
from rheo_finance.operations import OperationError
from rheo_finance.state import transfer
from rheo_finance.storage.schema import metadata

T0 = dt.datetime(2063, 1, 18, 12, 0, 0, 123456, tzinfo=dt.UTC)
# An offset with no real zone behind it: only the instant matters.
OFFSET = dt.timezone(dt.timedelta(minutes=90))


def table(name: str) -> Table:
    catalogue.load_all()
    return metadata.tables[f"finance.{name}"]


def roundtrip(name: str, values: dict[str, object]) -> dict[str, object]:
    found = table(name)
    row = tuple(values.get(column.name) for column in found.columns)
    return transfer.decode_line(found, transfer.encode_row(found, row))


def test_state_tables_are_the_state_set_parents_first() -> None:
    catalogue.load_all()
    names = [each.name for each in transfer.state_tables()]
    assert set(names) == STATE
    assert names.index("decision") < names.index("decision_item")
    assert names.index("decision") < names.index("decision_target")


def test_a_decision_row_round_trips_with_scale_and_zone() -> None:
    values: dict[str, object] = {
        "id": uuid.UUID("01a1b2c3-0000-7000-8000-000000000001"),
        "kind": "include",
        "target_ref": "txn:synthetic-1",
        "business_pct": Decimal("18.50"),
        "amount": Decimal("-0.000000001"),
        "effective_on": dt.date(2063, 2, 1),
        "decided_at": T0.astimezone(OFFSET),
    }
    back = roundtrip("decision", values)
    for name, value in values.items():
        assert back[name] == value, name
    # The scale is normalized (1 value, 1 text) and the instant is kept, in UTC.
    assert str(back["business_pct"]) == "18.5"
    decided = back["decided_at"]
    assert isinstance(decided, dt.datetime) and decided.utcoffset() == dt.timedelta(0)
    assert back["note"] is None


def test_integers_stay_integers_and_bools_stay_bools() -> None:
    back = roundtrip(
        "job_run",
        {
            "id": uuid.UUID("01a1b2c3-0000-7000-8000-000000000002"),
            "seq": 9007199254740993,
            "job": "finance.tick",
            "started_at": T0,
            "finished_at": T0,
            "ok": True,
            "detail_code": "completed",
        },
    )
    assert back["seq"] == 9007199254740993 and type(back["seq"]) is int
    assert back["ok"] is True
    item = roundtrip(
        "decision_item",
        {
            "decision_id": uuid.UUID(int=1),
            "seq": 1,
            "role": "part",
            "label": "synthetic",
            "amount": Decimal("12.30"),
        },
    )
    assert type(item["seq"]) is int and item["amount"] == Decimal("12.3")


def test_a_tag_that_does_not_match_its_column_is_refused() -> None:
    seq = table("job_run").c.seq
    ok = table("job_run").c.ok
    with pytest.raises(transfer.LineInvalid):
        transfer.decode_value(seq, ["bool", True])
    with pytest.raises(transfer.LineInvalid):
        transfer.decode_value(ok, ["num", "1"])
    with pytest.raises(transfer.LineInvalid):
        transfer.decode_value(seq, ["num", "1.5"])
    with pytest.raises(transfer.LineInvalid):
        transfer.decode_value(
            table("decision").c.decided_at, ["ts", "2063-01-18T12:00"]
        )
    assert transfer.decode_value(ok, ["none", None]) is None


@pytest.mark.parametrize("raw", ["Infinity", "-Infinity", "NaN", "sNaN"])
def test_a_number_that_is_not_finite_is_a_bad_line(raw: str) -> None:
    for column in (table("job_run").c.seq, table("decision").c.amount, None):
        with pytest.raises(transfer.LineInvalid, match="not a finite number"):
            transfer.decode_value(column, ["num", raw])


def test_an_integer_outside_bigint_is_a_bad_line() -> None:
    seq = table("job_run").c.seq
    assert transfer.decode_value(seq, ["num", str(2**63 - 1)]) == 2**63 - 1
    assert transfer.decode_value(seq, ["num", str(-(2**63))]) == -(2**63)
    for raw in (str(2**63), str(-(2**63) - 1), "1E+400"):
        with pytest.raises(transfer.LineInvalid, match="integer out of range"):
            transfer.decode_value(seq, ["num", raw])


def test_nested_lists_and_maps_round_trip() -> None:
    value = {
        "b": [Decimal("1.5"), "text", None, True, [dt.date(2063, 1, 1)]],
        "a": {"inner": T0, "id": uuid.UUID(int=7)},
    }
    assert transfer.decode_value(None, canonical_value(value)) == value
    # No state column holds JSON, so a float is never state.
    with pytest.raises(transfer.LineInvalid):
        transfer.decode_value(None, canonical_value({"f": 0.25}))


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[]",
        '[["key",["str","k"]]]',
    ],
)
def test_a_malformed_line_is_refused(line: str) -> None:
    with pytest.raises(transfer.LineInvalid):
        transfer.decode_line(table("notice"), line)


def test_columns_out_of_order_are_refused() -> None:
    notice = table("notice")
    row = ("k", "alert", dt.date(2063, 1, 1), T0, "pending", None)
    line = transfer.encode_row(notice, row)
    first, second, *rest = json.loads(line)
    swapped = json.dumps([second, first, *rest])
    with pytest.raises(transfer.LineInvalid):
        transfer.decode_line(notice, swapped)


def test_an_untaggable_column_is_refused_at_export() -> None:
    planted = Table(
        "planted",
        MetaData(),
        Column("data", JSONB),
    )
    with pytest.raises(OperationError, match="state_column_untaggable"):
        transfer.encode_row(planted, ({"a": 1},))


def test_lines_sort_and_hash_as_their_file() -> None:
    lines = ["b", "a"]
    assert transfer.file_bytes(lines) == b"b\na\n"
    assert transfer.file_bytes([]) == b""
    assert transfer.file_digest(["a"]) != transfer.file_digest(["a", ""])
