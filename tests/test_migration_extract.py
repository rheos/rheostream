"""Phase 2a: the timestamp rule, the versioned extract model, and the inventory (FR 1,
FR 3).

Seams: ``timestamps.parse_source_time`` (a pure function, every branch), the extract
model's validation (naive ``recorded_at``, a repeated ``entity_key``, round-trip
fidelity), and ``recallatron-migrate inventory`` as a CLI contract (flags in, both
private output files and a counts-only terminal line out). Every input is synthetic:
the snapshot is built here from DDL (no committed ``.db``), the ontology is Prompt 2's
committed ``synthetic_graph.jsonl`` plus files written into ``tmp_path``, and the
private root is a temp directory an unrelated temp repo ignores.
"""

import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from rheo_contracts.source_units import SOURCE_KEY_MAX_LENGTH
from rheo_recallatron.migration import cli, extract, inventory, private_paths
from rheo_recallatron.migration.extract import ExtractMention, ExtractUnit
from rheo_recallatron.migration.predecessor import graph_source, sqlite_source
from rheo_recallatron.migration.timestamps import (
    EPOCH_MILLISECONDS_FLOOR,
    TIME_OF_DAY_UNKNOWN,
    TIMESTAMP_IN_FUTURE,
    TIMESTAMP_UNPARSEABLE,
    SourceTime,
    UnresolvedTime,
    parse_source_time,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GRAPH_FIXTURE = (
    _REPO_ROOT
    / "modules"
    / "recallatron"
    / "tests"
    / "fixtures"
    / "migration"
    / "synthetic_graph.jsonl"
)

_EXTRACTED_AT = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _accepted(raw: object, extracted_at: datetime = _EXTRACTED_AT) -> SourceTime:
    result = parse_source_time(raw, extracted_at=extracted_at)
    assert isinstance(result, SourceTime), result
    assert result.value.tzinfo is UTC
    return result


# --- parse_source_time ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-02T03:04:05Z", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("2026-01-02T03:04:05.000Z", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("2026-01-02T03:04:05+02:00", datetime(2026, 1, 2, 1, 4, 5, tzinfo=UTC)),
        ("2026-01-01T23:30:00-01:00", datetime(2026, 1, 2, 0, 30, tzinfo=UTC)),
    ],
)
def test_an_offset_or_z_timestamp_is_normalized_to_utc(
    raw: str, expected: datetime
) -> None:
    result = _accepted(raw)
    assert result.value == expected
    assert result.losses == ()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-02T03:04:05", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("2026-01-02 03:04:05", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        (
            "2026-01-02 03:04:05.123",
            datetime(2026, 1, 2, 3, 4, 5, 123000, tzinfo=UTC),
        ),
    ],
)
def test_a_naive_iso_or_sqlite_timestamp_is_taken_as_utc(
    raw: str, expected: datetime
) -> None:
    result = _accepted(raw)
    assert result.value == expected
    assert result.losses == ()


def test_a_date_alone_is_midnight_utc_with_the_time_of_day_loss() -> None:
    result = _accepted("2026-01-02")
    assert result.value == datetime(2026, 1, 2, tzinfo=UTC)
    assert result.losses == (TIME_OF_DAY_UNKNOWN,)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1_700_000_000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)),
        (1_700_000_000.5, datetime(2023, 11, 14, 22, 13, 20, 500000, tzinfo=UTC)),
        (1_700_000_000_000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)),
        (0, datetime(1970, 1, 1, tzinfo=UTC)),
    ],
)
def test_an_epoch_number_is_seconds_or_milliseconds(
    raw: float, expected: datetime
) -> None:
    assert _accepted(raw).value == expected


def test_the_seconds_milliseconds_boundary_sits_exactly_at_ten_to_the_eleventh() -> (
    None
):
    """``10**11 - 1`` is seconds (the year 5138), ``10**11`` is milliseconds
    (1973-03-03). The extraction instant is pushed past 5138 so neither side is
    refused as the future."""
    far = datetime(6000, 1, 1, tzinfo=UTC)
    assert EPOCH_MILLISECONDS_FLOOR == 10**11
    below = _accepted(10**11 - 1, far).value
    at = _accepted(10**11, far).value
    assert below == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=10**11 - 1)
    assert below.year == 5138
    assert at == datetime(1973, 3, 3, 9, 46, 40, tzinfo=UTC)


@pytest.mark.parametrize("raw", ["1969-12-31T23:59:59Z", "1969-12-31", -1, -1_000.0])
def test_a_value_before_1970_is_unparseable(raw: object) -> None:
    assert parse_source_time(raw, extracted_at=_EXTRACTED_AT) == UnresolvedTime(
        TIMESTAMP_UNPARSEABLE
    )


def test_a_value_past_the_extraction_instant_plus_a_day_is_in_the_future() -> None:
    edge = _EXTRACTED_AT + timedelta(days=1)
    assert _accepted(edge.isoformat()).value == edge
    beyond = (edge + timedelta(seconds=1)).isoformat()
    assert parse_source_time(beyond, extracted_at=_EXTRACTED_AT) == UnresolvedTime(
        TIMESTAMP_IN_FUTURE
    )
    assert parse_source_time(
        (edge + timedelta(seconds=1)).timestamp() * 1000, extracted_at=_EXTRACTED_AT
    ) == UnresolvedTime(TIMESTAMP_IN_FUTURE)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "not a timestamp",
        "2026-13-01",
        "2026-02-30T00:00:00",
        True,
        False,
        float("nan"),
        float("inf"),
        10**30,
        {"ts": "2026-01-01"},
        ["2026-01-01"],
    ],
)
def test_garbage_is_unparseable(raw: object) -> None:
    assert parse_source_time(raw, extracted_at=_EXTRACTED_AT) == UnresolvedTime(
        TIMESTAMP_UNPARSEABLE
    )


def test_a_naive_extraction_instant_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_source_time("2026-01-01", extracted_at=datetime(2026, 9, 27))


# --- the extract model ---------------------------------------------------------------


def _mention(entity_key: str = "graph.entity:ent-002", **overrides: object) -> dict:
    return {
        "kind": "person",
        "name": "Sample Gardener North",
        "role": "subject",
        "entity_key": entity_key,
        **overrides,
    }


def _unit(**overrides: object) -> dict:
    return {
        "external_source_key": "graph.entity:ent-004",
        "kind": "fact",
        "title": "New Greenhouse Rota",
        "body": "New Greenhouse Rota\nrole: volunteer",
        "confidence": 0.7,
        "recorded_at": datetime(2026, 1, 4, tzinfo=UTC),
        "mentions": [_mention()],
        **overrides,
    }


def _header(unit_count: int) -> str:
    return extract.ExtractHeader(
        format=extract.EXTRACT_FORMAT,
        version=extract.EXTRACT_VERSION,
        unit_count=unit_count,
        unresolved_count=0,
    ).model_dump_json()


def _locations(error: pytest.ExceptionInfo[ValidationError]) -> list[tuple]:
    return [item["loc"] for item in error.value.errors()]


@pytest.mark.parametrize(
    "recorded_at", [datetime(2026, 1, 4, 3, 0), "2026-01-04T03:00:00"]
)
def test_the_extract_model_rejects_a_naive_recorded_at(recorded_at: object) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ExtractUnit.model_validate(_unit(recorded_at=recorded_at))
    assert _locations(excinfo) == [("recorded_at",)]
    assert excinfo.value.errors()[0]["type"] == "timezone_aware"


def test_the_extract_model_rejects_a_naive_recorded_at_read_from_json() -> None:
    line = ExtractUnit.model_validate(_unit()).model_dump_json()
    naive = line.replace("2026-01-04T00:00:00Z", "2026-01-04T00:00:00")
    assert naive != line
    with pytest.raises(extract.ExtractRefusal) as excinfo:
        extract.parse_extract([_header(unit_count=1), naive])
    assert excinfo.value.state == extract.EXTRACT_UNIT_INVALID
    assert excinfo.value.detail == "line 2: recorded_at=timezone_aware"


def test_an_aware_non_utc_recorded_at_is_kept_as_the_same_instant() -> None:
    plus_two = datetime(2026, 1, 4, 2, 0, tzinfo=timezone(timedelta(hours=2)))
    unit = ExtractUnit.model_validate(_unit(recorded_at=plus_two))
    assert unit.recorded_at == datetime(2026, 1, 4, tzinfo=UTC)


def test_a_unit_repeating_one_entity_key_across_its_mentions_is_rejected() -> None:
    with pytest.raises(ValidationError, match="one mention per entity_key"):
        ExtractUnit.model_validate(
            _unit(mentions=[_mention(), _mention(role="works_on")])
        )
    two_keys = ExtractUnit.model_validate(
        _unit(mentions=[_mention(), _mention("graph.entity:ent-001", kind="project")])
    )
    assert len(two_keys.mentions) == 2


def test_the_entity_key_is_required_and_bounded() -> None:
    no_key = _mention()
    del no_key["entity_key"]
    with pytest.raises(ValidationError) as missing:
        ExtractMention.model_validate(no_key)
    assert _locations(missing) == [("entity_key",)]
    longest = "graph.entity:" + "k" * (SOURCE_KEY_MAX_LENGTH - len("graph.entity:"))
    assert ExtractMention.model_validate(_mention(longest)).entity_key == longest
    with pytest.raises(ValidationError):
        ExtractMention.model_validate(_mention(longest + "k"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("external_source_key", "graph.entity:" + "k" * SOURCE_KEY_MAX_LENGTH),
        ("external_source_key", "no-separator"),
        ("external_source_key", ":ent-004"),
        ("external_source_key", "graph.entity:"),
        ("kind", "topic_thread"),
        ("kind", "procedural_notes"),
        ("title", ""),
        ("title", "t" * 201),
        ("body", "é" * 32769),  # 65538 UTF-8 bytes in 32769 characters
        ("confidence", 1.5),
        ("confidence", -0.1),
    ],
)
def test_the_extract_model_enforces_its_bounds(field: str, value: object) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ExtractUnit.model_validate(_unit(**{field: value}))
    assert _locations(excinfo) == [(field,)]


def test_the_extract_model_admits_its_own_bounds_exactly() -> None:
    unit = ExtractUnit.model_validate(
        _unit(title="t" * 200, body="é" * 32768, confidence=None, mentions=[])
    )
    assert len(unit.body.encode("utf-8")) == 65536


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "household"),
        ("role", ""),
        ("role", "r" * 101),
        ("name", ""),
    ],
)
def test_a_mention_enforces_its_vocabulary_and_bounds(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ExtractMention.model_validate(_mention(**{field: value}))
    assert _locations(excinfo) == [(field,)]


def test_the_extract_models_are_frozen_and_forbid_extra_fields() -> None:
    unit = ExtractUnit.model_validate(_unit())
    with pytest.raises(ValidationError):
        unit.title = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError) as excinfo:
        ExtractUnit.model_validate(_unit(source_ref="gmail:synthetic-1"))
    assert _locations(excinfo) == [("source_ref",)]
    with pytest.raises(ValidationError):
        ExtractMention.model_validate(_mention(confirmed=True))


def test_the_extract_round_trips_through_its_lines() -> None:
    units = [
        ExtractUnit.model_validate(_unit()),
        ExtractUnit.model_validate(
            _unit(
                external_source_key="store.memory_item:7",
                kind="note",
                title="Sample Gardener North",
                body="role: volunteer\nsnowman ☃ and a tab\tinside",
                confidence=None,
                recorded_at=datetime(2026, 1, 2, 3, 4, 5, 123000, tzinfo=UTC),
                mentions=[
                    _mention("store.memory_item:7", role="subject"),
                    _mention(
                        "graph.entity:ent-001",
                        kind="project",
                        name="Example Widget Co",
                        role="works_on,subject",
                    ),
                ],
            )
        ),
        ExtractUnit.model_validate(
            _unit(
                external_source_key="store.session_digest:3",
                kind="summary",
                title="Session summary 2026-01-02",
                mentions=[],
            )
        ),
    ]

    lines = extract.dump_extract(units, unresolved_count=4)
    parsed = extract.parse_extract(lines)

    assert parsed.units == tuple(units)
    assert parsed.header == extract.ExtractHeader(
        format="rheo.migration.extract", version=1, unit_count=3, unresolved_count=4
    )
    assert json.loads(lines[0]) == {
        "format": "rheo.migration.extract",
        "version": 1,
        "unit_count": 3,
        "unresolved_count": 4,
    }
    assert set(json.loads(lines[1])) == {
        "external_source_key",
        "kind",
        "title",
        "body",
        "confidence",
        "recorded_at",
        "mentions",
    }
    assert extract.dump_extract(parsed.units, unresolved_count=4) == lines


def test_a_header_that_disagrees_with_its_units_is_refused() -> None:
    lines = extract.dump_extract(
        [ExtractUnit.model_validate(_unit())], unresolved_count=0
    )
    with pytest.raises(extract.ExtractRefusal) as excinfo:
        extract.parse_extract(lines[:1])
    assert excinfo.value.state == extract.EXTRACT_COUNT_MISMATCH
    with pytest.raises(extract.ExtractRefusal) as empty:
        extract.parse_extract([])
    assert empty.value.state == extract.EXTRACT_EMPTY


@pytest.mark.parametrize(
    "header",
    [
        '{"format": "rheo.migration.other", "version": 1, "unit_count": 0, '
        '"unresolved_count": 0}',
        '{"format": "rheo.migration.extract", "version": 2, "unit_count": 0, '
        '"unresolved_count": 0}',
        '{"format": "rheo.migration.extract", "version": 1, "unit_count": -1, '
        '"unresolved_count": 0}',
        '{"format": "rheo.migration.extract", "version": 1, "unit_count": 0}',
        "not json",
    ],
)
def test_a_wrong_header_is_refused(header: str) -> None:
    with pytest.raises(extract.ExtractRefusal) as excinfo:
        extract.parse_extract([header])
    assert excinfo.value.state == extract.EXTRACT_HEADER_INVALID


def test_a_refused_line_never_quotes_its_content() -> None:
    header = _header(unit_count=1)
    unit = json.loads(ExtractUnit.model_validate(_unit()).model_dump_json())
    unit["title"] = "synthetic private phrase " * 10
    with pytest.raises(extract.ExtractRefusal) as excinfo:
        extract.parse_extract([header, json.dumps(unit)])
    assert excinfo.value.detail == "line 2: title=string_too_long"
    assert "synthetic private phrase" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


# --- the inventory -------------------------------------------------------------------

_DDL = """
CREATE TABLE memory_items (
    id integer PRIMARY KEY AUTOINCREMENT NOT NULL,
    type text NOT NULL,
    label text NOT NULL,
    ts text NOT NULL,
    superseded_by integer,
    conflict_flag integer DEFAULT false NOT NULL,
    FOREIGN KEY (superseded_by) REFERENCES memory_items(id)
);
CREATE TABLE conversation (
    id integer PRIMARY KEY AUTOINCREMENT NOT NULL,
    session_id text NOT NULL,
    content text NOT NULL
);
CREATE TABLE session_digest (
    id integer PRIMARY KEY AUTOINCREMENT NOT NULL,
    session_id text NOT NULL UNIQUE,
    summary text NOT NULL
);
CREATE TABLE app_secret (name text PRIMARY KEY, value text NOT NULL);
CREATE TABLE tool_call_log (id integer PRIMARY KEY, tool text NOT NULL);
CREATE TABLE __synthetic_migrations (id integer PRIMARY KEY, hash text);
CREATE TABLE mystery_table (id integer PRIMARY KEY);
CREATE VIEW live_items AS
    SELECT id, label FROM memory_items WHERE superseded_by IS NULL;
CREATE TABLE entity_vec_rowids (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT, id, chunk_id INTEGER, chunk_offset INTEGER
);
CREATE TABLE entity_vec_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT, size INTEGER NOT NULL, validity BLOB
);
"""
# sqlite-vec is never loaded, so the vec0 table's own definition is planted in the
# schema directly, exactly as a snapshot opened without the extension presents it.
_VEC0_DEFINITION = (
    "CREATE VIRTUAL TABLE entity_vec USING vec0("
    "entity_id TEXT PRIMARY KEY, embedding FLOAT[4])"
)
_SECRET_VALUE = "synthetic-secret-value-0000"

# Live graph entities in the fixture: ent-001, ent-002, ent-004, ent-005, ent-006.
_VEC_IDS = ["ent-001", "ent-002", "ent-003", "ent-099", "ent-004"]  # 2 ghosts
_MEMORY_ITEMS = [
    # (label, superseded_by)
    ("example widget co", None),  # overlaps ent-001
    ("Tea", 3),  # superseded: not counted
    ("  TEA ", None),  # overlaps ent-005
    ("Sample Gardener North", None),  # overlaps ent-002 after whitespace collapse
    ("Seed Library Hours", None),  # no graph twin
]
_TURN_SESSIONS = ["s1", "s1", "s2", "s3", "s3"]
_DIGEST_SESSIONS = ["s1", "s4"]  # s2 and s3 have turns and no digest


def _snapshot(tmp_path: Path) -> Path:
    path = tmp_path / "inputs" / "synthetic-snapshot.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(_DDL)
    connection.executemany(
        "INSERT INTO memory_items (type, label, ts, superseded_by) "
        "VALUES ('fact', ?, '2026-01-01 00:00:00', ?)",
        _MEMORY_ITEMS,
    )
    connection.executemany(
        "INSERT INTO conversation (session_id, content) VALUES (?, 'synthetic turn')",
        [(s,) for s in _TURN_SESSIONS],
    )
    connection.executemany(
        "INSERT INTO session_digest (session_id, summary) VALUES (?, 'synthetic')",
        [(s,) for s in _DIGEST_SESSIONS],
    )
    connection.execute(
        "INSERT INTO app_secret VALUES ('synthetic-key', ?)", (_SECRET_VALUE,)
    )
    connection.executemany(
        "INSERT INTO entity_vec_rowids (id, chunk_id, chunk_offset) VALUES (?, 1, 0)",
        [(i,) for i in _VEC_IDS],
    )
    connection.execute("INSERT INTO entity_vec_chunks (size) VALUES (1024)")
    connection.execute("PRAGMA writable_schema=ON")
    connection.execute(
        "INSERT INTO sqlite_master (type, name, tbl_name, rootpage, sql) "
        "VALUES ('table', 'entity_vec', 'entity_vec', 0, ?)",
        (_VEC0_DEFINITION,),
    )
    connection.execute("PRAGMA writable_schema=OFF")
    connection.commit()
    connection.close()
    return path


_ONTOLOGY_EXTRAS = {
    "profile.synth.person.json": '{"synthetic": true}\n',
    "maintainer-status.json": "{}\n",
    "notes/readme.txt": "synthetic\n",
}


def _ontology(tmp_path: Path) -> Path:
    directory = tmp_path / "inputs" / "ontology"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_GRAPH_FIXTURE, directory / graph_source.GRAPH_FILE_NAME)
    for name, text in _ONTOLOGY_EXTRAS.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text)
    return directory


@pytest.fixture
def private_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    repo = tmp_path / "private-home"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("private/\n")
    root = repo / "private"
    root.mkdir()
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    return root


def _run_inventory(tmp_path: Path, out: Path) -> int:
    return cli.main(
        [
            "inventory",
            "--snapshot",
            str(_snapshot(tmp_path)),
            "--ontology",
            str(_ontology(tmp_path)),
            "--out",
            str(out),
        ]
    )


def test_inventory_writes_both_files_into_a_new_out_directory(
    private_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = private_root / "reports" / "inventory-run"
    assert not out.exists()

    assert _run_inventory(tmp_path, out) == 0

    assert sorted(p.name for p in out.iterdir()) == ["inventory.json", "inventory.md"]
    for path in out.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        text = path.read_text()
        assert _SECRET_VALUE not in text
        assert "synthetic turn" not in text
    captured = capsys.readouterr()
    assert captured.out == "inventory: objects=12 ontology_files=4\n"
    assert captured.err == ""


def test_inventory_counts_the_synthetic_fixtures(
    private_root: Path, tmp_path: Path
) -> None:
    out = private_root / "inventory-run"
    assert _run_inventory(tmp_path, out) == 0
    report = json.loads((out / "inventory.json").read_text())
    objects = {entry["name"]: entry for entry in report["objects"]}

    assert (report["format"], report["version"]) == ("rheo.migration.inventory", 1)
    assert report["entity_vec_ghost_rows"] == 2
    assert report["memory_items_graph_label_overlap"] == 3
    assert report["sessions_without_digest"] == 2

    assert {name: entry["row_count"] for name, entry in objects.items()} == {
        "__synthetic_migrations": 0,
        "app_secret": 1,
        "conversation": 5,
        "entity_vec": 5,
        "entity_vec_chunks": 1,
        "entity_vec_rowids": 5,
        "live_items": 4,
        "memory_items": 5,
        "mystery_table": 0,
        "session_digest": 2,
        "sqlite_sequence": 5,
        "tool_call_log": 0,
    }
    assert {
        name: (entry["ledger_scope"], entry["scope_reason"])
        for name, entry in objects.items()
    } == {
        "__synthetic_migrations": ("inventory_only", "schema_metadata"),
        "app_secret": ("inventory_only", "counted_never_selected"),
        "conversation": ("in_scope", "ledger_rows"),
        "entity_vec": ("inventory_only", "derived_index"),
        "entity_vec_chunks": ("inventory_only", "derived_index"),
        "entity_vec_rowids": ("inventory_only", "derived_index"),
        "live_items": ("unclassified", "not_in_spec"),
        "memory_items": ("in_scope", "ledger_rows"),
        "mystery_table": ("unclassified", "not_in_spec"),
        "session_digest": ("in_scope", "ledger_rows"),
        "sqlite_sequence": ("inventory_only", "schema_metadata"),
        "tool_call_log": ("inventory_only", "telemetry_harvested_separately"),
    }

    vec = objects["entity_vec"]
    assert (vec["type"], vec["virtual_module"], vec["columns"]) == (
        "table",
        "vec0",
        None,
    )
    assert vec["shadow_row_counts"] == {"entity_vec_chunks": 1, "entity_vec_rowids": 5}
    assert objects["entity_vec_rowids"]["shadow_of"] == "entity_vec"

    items = objects["memory_items"]
    assert [c["name"] for c in items["columns"]] == [
        "id",
        "type",
        "label",
        "ts",
        "superseded_by",
        "conflict_flag",
    ]
    assert items["columns"][0]["primary_key"] and items["columns"][2]["not_null"]
    assert items["foreign_keys"] == [
        {
            "from_column": "superseded_by",
            "target_table": "memory_items",
            "target_column": "id",
        }
    ]

    assert report["graph"] == {
        "entity_lines": 7,
        "live_entities": 5,
        "entity_types": {
            "Deadline": 1,
            "Fact": 2,
            "Person": 2,
            "Preference": 1,
            "Project": 1,
        },
        "live_entity_types": {
            "Deadline": 1,
            "Fact": 1,
            "Person": 1,
            "Preference": 1,
            "Project": 1,
        },
        "ops": {
            "confirm": 1,
            "confirm_relate": 1,
            "relate": 1,
            "supersede": 1,
            "unrelate": 1,
        },
        "malformed_lines": 1,
        "unknown_op_lines": 1,
    }
    assert report["ontology_files"] == [
        {
            "name": "graph.jsonl",
            "size_bytes": _GRAPH_FIXTURE.stat().st_size,
            "ledger_scope": "in_scope",
            "scope_reason": "ledger_rows",
        },
        {
            "name": "maintainer-status.json",
            "size_bytes": 3,
            "ledger_scope": "inventory_only",
            "scope_reason": "operational_state",
        },
        {
            "name": "notes/readme.txt",
            "size_bytes": 10,
            "ledger_scope": "unclassified",
            "scope_reason": "not_in_spec",
        },
        {
            "name": "profile.synth.person.json",
            "size_bytes": 20,
            "ledger_scope": "in_scope",
            "scope_reason": "one_class_row",
        },
    ]
    markdown = (out / "inventory.md").read_text()
    assert "| entity_vec | virtual (vec0) | 5 |" in markdown
    assert "ghost rows (no live graph entity): 2" in markdown


def test_the_inventory_counts_app_secret_and_never_selects_a_value(
    tmp_path: Path,
) -> None:
    connection = sqlite_source.open_snapshot(_snapshot(tmp_path))
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        result = inventory.build_inventory(
            connection,
            graph_source.read_graph(_ontology(tmp_path)),
            _ontology(tmp_path),
        )
    finally:
        connection.close()
    secret = [s for s in statements if "app_secret" in s]
    counts = [s for s in secret if s == 'SELECT count(*) FROM "app_secret"']
    # pragma_table_info(...) also traces as its expanded "-- PRAGMA ..." form.
    introspection = [s for s in secret if "pragma_" in s or s.startswith("-- PRAGMA ")]
    assert len(counts) == 1
    assert sorted(counts + introspection) == sorted(secret)
    assert not [s for s in statements if '"entity_vec"' in s or "FROM entity_vec " in s]
    assert result.entity_vec_ghost_rows == 2


def test_inventory_refuses_an_out_dir_outside_the_private_root(
    private_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "outside"

    assert _run_inventory(tmp_path, outside) == 1

    assert not outside.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{private_paths.OUTPUT_ESCAPES_ROOT}: ")


def test_inventory_refuses_a_missing_ontology_without_writing(
    private_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = private_root / "inventory-run"
    code = cli.main(
        [
            "inventory",
            "--snapshot",
            str(_snapshot(tmp_path)),
            "--ontology",
            str(tmp_path / "absent-ontology"),
            "--out",
            str(out),
        ]
    )
    assert code == 1
    assert list(out.iterdir()) == []
    assert capsys.readouterr().err.startswith(f"{cli.INPUT_UNREADABLE}: ")
