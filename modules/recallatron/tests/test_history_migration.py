"""Revision 0004 owns frozen DDL, independent of runtime table evolution."""

import runpy
from pathlib import Path

import pytest
from rheo_recallatron.storage import tables
from rheo_recallatron.storage.tables import history_record
from sqlalchemy import Column, MetaData, Text, create_mock_engine
from sqlalchemy.schema import CreateIndex, CreateTable

_REVISION = (
    Path(__file__).resolve().parents[1]
    / "src/rheo_recallatron/migrations/versions"
    / "0004_history_records.py"
)


def test_history_revision_has_the_current_shape_but_independent_metadata() -> None:
    frozen = runpy.run_path(str(_REVISION))["history_metadata"].tables[
        "recallatron.history_record"
    ]
    assert frozen is not history_record
    dialect = create_mock_engine("postgresql://", lambda *args, **kwargs: None).dialect
    assert frozen.schema == history_record.schema
    assert frozen.name == history_record.name
    assert list(frozen.c.keys()) == list(history_record.c.keys())
    # SQLAlchemy stores constraints in a set; their render order is not stable.
    # Compare all DDL lines independently of that ordering and trailing commas,
    # while checking column order separately above.
    assert sorted(
        line.strip().rstrip(",")
        for line in str(CreateTable(frozen).compile(dialect=dialect)).splitlines()
    ) == sorted(
        line.strip().rstrip(",")
        for line in str(
            CreateTable(history_record).compile(dialect=dialect)
        ).splitlines()
    )
    assert sorted(
        str(CreateIndex(i).compile(dialect=dialect)) for i in frozen.indexes
    ) == (
        sorted(
            str(CreateIndex(i).compile(dialect=dialect)) for i in history_record.indexes
        )
    )


def test_later_runtime_columns_cannot_rewrite_revision_0004(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Substitute evolved runtime metadata, so the test catches a runtime import
    # without mutating the table used by other tests.
    evolved = history_record.to_metadata(MetaData())
    evolved.append_column(Column("future_synthetic_column", Text))
    monkeypatch.setattr(tables, "history_metadata", evolved.metadata)
    frozen = runpy.run_path(str(_REVISION))["history_metadata"].tables[
        "recallatron.history_record"
    ]
    assert "future_synthetic_column" not in frozen.c
    assert "future_synthetic_column" in evolved.c
