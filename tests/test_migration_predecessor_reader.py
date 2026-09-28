"""The predecessor reader and the ``recallatron-migrate denylist`` generator (FR 1
scaffolding, FR 18 / AC 17).

Seams: ``sqlite_source.open_snapshot``'s read-only-open refusal, and the ``denylist``
subcommand as a CLI contract (flags in, ``denylist.txt`` content and a counts-only
terminal line out). Every input is synthetic: the SQLite snapshot is built here from
DDL strings (no committed ``.db``), the ontology is the committed
``synthetic_graph.jsonl`` fixture, and the private root is a temp directory an
unrelated temp repo ignores, the same pattern as ``test_migration_private_paths.py``.
"""

import hashlib
import importlib.util
import json
import shutil
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import pytest
from harness.migration_inputs import SECRET_VALUE as _SECRET_VALUE
from harness.migration_inputs import new_snapshot, use_private_root
from rheo_recallatron.migration import cli, private_paths
from rheo_recallatron.migration.predecessor import graph_source, sqlite_source
from rheo_recallatron.migration.predecessor.sqlite_source import SnapshotRefusal

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
_LEAK_SCAN_SCRIPT = _REPO_ROOT / "scripts" / "check_migration_outputs.py"

# A subset of the predecessor's real `memory_items` columns, plus an `app_secret`
# table whose one synthetic value must never be selected.
_DDL = """
CREATE TABLE memory_items (
    id integer PRIMARY KEY AUTOINCREMENT NOT NULL,
    type text NOT NULL,
    label text NOT NULL,
    properties text NOT NULL,
    ts text NOT NULL,
    superseded_by integer,
    conflict_flag integer DEFAULT false NOT NULL,
    FOREIGN KEY (superseded_by) REFERENCES memory_items(id)
);
CREATE TABLE app_secret (name text PRIMARY KEY, value text NOT NULL);
"""
# "Kale" (4 characters) and "Beets" (5) pin the floor from both sides.
_MEMORY_LABELS = [
    "Seed Library Hours",
    "tea",
    "EXAMPLE widget   co",
    "  Compost   Rota  ",
    "Kale",
    "Beets",
]


def _snapshot(tmp_path: Path, *, wal: bool = False) -> Path:
    path, connection = new_snapshot(tmp_path, _DDL, wal=wal)
    connection.executemany(
        "INSERT INTO memory_items (type, label, properties, ts, superseded_by) "
        "VALUES ('fact', ?, '{}', '2026-01-01T00:00:00.000Z', NULL)",
        [(label,) for label in _MEMORY_LABELS],
    )
    connection.execute("UPDATE memory_items SET superseded_by = 1 WHERE id = 2")
    connection.commit()
    connection.close()
    return path


def _ontology(tmp_path: Path, extra_records: Sequence[object] = ()) -> Path:
    """The committed fixture, with ``extra_records`` appended as JSON lines."""
    directory = tmp_path / "inputs" / "ontology"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / graph_source.GRAPH_FILE_NAME
    shutil.copyfile(_GRAPH_FIXTURE, target)
    with target.open("a", encoding="utf-8") as handle:
        for record in extra_records:
            handle.write(json.dumps(record) + "\n")
    return directory


def _fact(entity_id: str, label: str, superseded_by: str | None) -> dict[str, object]:
    return {
        "id": entity_id,
        "type": "Fact",
        "label": label,
        "properties": {},
        "valid_from": "2026-01-14T00:00:00.000Z",
        "valid_until": None,
        "confidence": 0.7,
        "source": "manual",
        "superseded_by": superseded_by,
        "confirmed": False,
    }


# The last record for ent-007 carries ``superseded_by`` itself and no supersede op
# names it: only the record's own field says it is not live.
_SELF_SUPERSEDED = [
    _fact("ent-007", "Retired Watering Schedule", None),
    _fact("ent-007", "Retired Watering Schedule", "ent-004"),
]


_HARVEST_QUERIES: list[str | None] = [
    "greenhouse",
    "Greenhouse ",
    "pH",
    None,
    "",
    "seed library hours",
]


def _harvest(tmp_path: Path, suffix: str) -> Path:
    path = tmp_path / "inputs" / f"synthetic-harvest{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"tool": "entity_search", "query": query} for query in _HARVEST_QUERIES]
    if suffix == ".json":
        path.write_text(json.dumps(rows))
    else:
        lines = ["tool,query"] + [f"entity_search,{q or ''}" for q in _HARVEST_QUERIES]
        path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def private_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return use_private_root(monkeypatch, tmp_path, private_paths.PRIVATE_ROOT_VARIABLE)


def _run(
    tmp_path: Path,
    out: Path,
    suffix: str = ".json",
    harvest: Path | None = None,
    snapshot: Path | None = None,
    extra_records: Sequence[object] = (),
) -> int:
    return cli.main(
        [
            "denylist",
            "--snapshot",
            str(snapshot if snapshot is not None else _snapshot(tmp_path)),
            "--ontology",
            str(_ontology(tmp_path, extra_records)),
            "--harvest",
            str(harvest if harvest is not None else _harvest(tmp_path, suffix)),
            "--out",
            str(out),
        ]
    )


def _written(out: Path) -> list[str]:
    return (out / cli.DENYLIST_FILE_NAME).read_text().splitlines()


# --- sqlite_source: read-only immutable open -----------------------------------------


@pytest.mark.parametrize("mode", ["rw", "rwc", "memory", ""])
def test_a_writable_or_other_mode_open_is_refused(tmp_path: Path, mode: str) -> None:
    snapshot = _snapshot(tmp_path)
    with pytest.raises(SnapshotRefusal) as excinfo:
        sqlite_source.open_snapshot(snapshot, mode=mode)
    assert excinfo.value.state == sqlite_source.SNAPSHOT_MODE_REFUSED


def test_a_caller_supplied_uri_is_refused(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with pytest.raises(SnapshotRefusal) as excinfo:
        sqlite_source.open_snapshot(f"file:{snapshot}?mode=rw")
    assert excinfo.value.state == sqlite_source.SNAPSHOT_URI_REFUSED


def test_a_missing_snapshot_is_refused_and_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "absent.db"
    with pytest.raises(SnapshotRefusal) as excinfo:
        sqlite_source.open_snapshot(missing)
    assert excinfo.value.state == sqlite_source.SNAPSHOT_MISSING
    assert not missing.exists()


def test_the_exact_read_only_immutable_uri_reaches_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = _snapshot(tmp_path)
    calls: list[tuple[str, bool]] = []
    real_connect = sqlite3.connect

    def spy(database: str, *, uri: bool = False) -> sqlite3.Connection:
        calls.append((database, uri))
        return real_connect(database, uri=uri)

    monkeypatch.setattr(sqlite_source.sqlite3, "connect", spy)
    sqlite_source.open_snapshot(snapshot).close()

    ((database, uri),) = calls
    assert uri is True
    assert database == f"{snapshot.resolve().as_uri()}?mode=ro&immutable=1"
    parts = urlsplit(database)
    assert (parts.scheme, parts.path) == ("file", str(snapshot.resolve()))
    assert parse_qs(parts.query, strict_parsing=True) == {
        "mode": ["ro"],
        "immutable": ["1"],
    }


def test_the_open_connection_cannot_write_and_leaves_no_side_files(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    before = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    connection = sqlite_source.open_snapshot(snapshot)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM memory_items")
        assert sqlite_source.memory_item_labels(connection) == _MEMORY_LABELS
    finally:
        connection.close()
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in snapshot.parent.iterdir()) == [snapshot.name]


def test_a_wal_mode_snapshot_is_read_without_wal_or_shm_side_files(
    tmp_path: Path,
) -> None:
    """A plain ``mode=ro`` open of a WAL database creates ``-wal`` and ``-shm``
    beside it; ``immutable=1`` must not, while the connection is open or after."""
    snapshot = _snapshot(tmp_path, wal=True)
    assert snapshot.read_bytes()[18:20] == b"\x02\x02"  # the file is in WAL mode
    assert sorted(p.name for p in snapshot.parent.iterdir()) == [snapshot.name]
    before = hashlib.sha256(snapshot.read_bytes()).hexdigest()

    connection = sqlite_source.open_snapshot(snapshot)
    try:
        assert sqlite_source.memory_item_labels(connection) == _MEMORY_LABELS
        assert sorted(p.name for p in snapshot.parent.iterdir()) == [snapshot.name]
    finally:
        connection.close()

    assert sorted(p.name for p in snapshot.parent.iterdir()) == [snapshot.name]
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == before


def test_uncheckpointed_wal_pages_are_refused(tmp_path: Path) -> None:
    snapshot, writer = new_snapshot(tmp_path, _DDL, wal=True)
    try:
        writer.execute(
            "INSERT INTO memory_items (type, label, properties, ts) "
            "VALUES ('fact', 'Recent synthetic fact', '{}', '2026-01-01')"
        )
        writer.commit()
        wal = snapshot.with_name(snapshot.name + "-wal")
        assert wal.stat().st_size > 0
        assert writer.execute("SELECT count(*) FROM memory_items").fetchone() == (1,)
        with pytest.raises(SnapshotRefusal) as excinfo:
            sqlite_source.open_snapshot(snapshot)
        assert excinfo.value.state == sqlite_source.SNAPSHOT_UNREADABLE
    finally:
        writer.close()


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_an_empty_sidecar_does_not_refuse_a_snapshot(
    tmp_path: Path, suffix: str
) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot.with_name(snapshot.name + suffix).touch()
    with sqlite_source.open_snapshot(snapshot) as connection:
        assert sqlite_source.memory_item_labels(connection) == _MEMORY_LABELS
    connection.close()


def test_unfinished_rollback_journal_is_refused(tmp_path: Path) -> None:
    snapshot, writer = new_snapshot(tmp_path, _DDL)
    try:
        writer.execute(
            "INSERT INTO memory_items (type, label, properties, ts) "
            "VALUES ('fact', 'Uncommitted synthetic fact', '{}', '2026-01-01')"
        )
        journal = snapshot.with_name(snapshot.name + "-journal")
        assert journal.stat().st_size > 0
        with pytest.raises(SnapshotRefusal) as excinfo:
            sqlite_source.open_snapshot(snapshot)
        assert excinfo.value.state == sqlite_source.SNAPSHOT_UNREADABLE
        assert "journal" in str(excinfo.value)
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_uninspectable_snapshot_sidecar_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suffix: str
) -> None:
    snapshot = _snapshot(tmp_path)
    sidecar = snapshot.with_name(snapshot.name + suffix)
    original_stat = Path.stat

    def guarded_stat(path: Path, **kwargs: object):
        if path == sidecar:
            raise PermissionError("synthetic sidecar refusal")
        return original_stat(path, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    with pytest.raises(SnapshotRefusal) as excinfo:
        sqlite_source.open_snapshot(snapshot)
    assert excinfo.value.state == sqlite_source.SNAPSHOT_UNREADABLE


def test_schema_introspection_reads_tables_columns_and_foreign_keys(
    tmp_path: Path,
) -> None:
    connection = sqlite_source.open_snapshot(_snapshot(tmp_path))
    try:
        tables = {
            obj.name
            for obj in sqlite_source.schema_objects(connection)
            if obj.type == "table"
        }
        columns = sqlite_source.table_columns(connection, "memory_items")
        keys = sqlite_source.table_foreign_keys(connection, "memory_items")
    finally:
        connection.close()
    assert {"memory_items", "app_secret"} <= tables
    assert [c.name for c in columns][:3] == ["id", "type", "label"]
    assert columns[0].primary_key and columns[2].not_null
    assert keys == [sqlite_source.ForeignKey("superseded_by", "memory_items", "id")]


def test_app_secret_is_never_selected(tmp_path: Path) -> None:
    connection = sqlite_source.open_snapshot(_snapshot(tmp_path))
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        with pytest.raises(SnapshotRefusal) as excinfo:
            sqlite_source.select_column(connection, "app_secret", "value")
        assert excinfo.value.state == sqlite_source.TABLE_NEVER_SELECTED
        with pytest.raises(SnapshotRefusal) as unmapped:
            sqlite_source.select_column(connection, "sqlite_sequence", "name")
        assert unmapped.value.state == sqlite_source.TABLE_NOT_SELECTABLE
        sqlite_source.memory_item_labels(connection)
    finally:
        connection.close()
    assert statements, "the trace callback saw no statement"
    assert not [s for s in statements if "app_secret" in s]


# --- graph_source: op lines and full records -----------------------------------------


def test_the_graph_parser_splits_op_lines_from_full_records(tmp_path: Path) -> None:
    parsed = graph_source.read_graph(_ontology(tmp_path))
    assert [(line.line_number, line.op) for line in parsed.op_lines] == [
        (7, "relate"),
        (8, "confirm_relate"),
        (9, "unrelate"),
        (10, "confirm"),
        (11, "supersede"),
    ]
    assert [(e.line_number, e.entity_id) for e in parsed.entity_lines] == [
        (1, "ent-001"),
        (2, "ent-002"),
        (3, "ent-003"),
        (4, "ent-004"),
        (5, "ent-005"),
        (6, "ent-006"),
        (12, "ent-002"),
    ]
    assert parsed.unknown_op_line_numbers == (14,)
    assert parsed.malformed_line_numbers == (15,)


def test_live_labels_take_the_last_record_and_skip_only_superseded_ones(
    tmp_path: Path,
) -> None:
    """ent-003 is named ``old`` by a supersede op, so it is not live; ent-006 has a
    ``valid_until`` but is a live head (liveness is supersession only)."""
    parsed = graph_source.read_graph(_ontology(tmp_path))
    assert graph_source.live_entity_labels(parsed) == [
        "Example Widget Co",
        "Sample  Gardener North",
        "New Greenhouse Rota",
        "Tea",
        "Draft Seed Order",
    ]


# --- the denylist subcommand ---------------------------------------------------------

_EXPECTED_WITHOUT_EXEMPTIONS = [
    "beets",
    "compost rota",
    "draft seed order",
    "example widget co",
    "greenhouse",
    "new greenhouse rota",
    "sample gardener north",
    "seed library hours",
]


@pytest.mark.parametrize("suffix", [".json", ".csv"])
def test_denylist_writes_normalized_deduplicated_lines_and_drops_short_ones(
    private_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    suffix: str,
) -> None:
    """ "tea" (graph and memory_items), "kale" (memory_items) and "ph" (harvest) are
    the three distinct lines under five characters: dropped, and only their count is
    printed."""
    out = private_root / "denylist-out"

    assert _run(tmp_path, out, suffix) == 0

    assert _written(out) == _EXPECTED_WITHOUT_EXEMPTIONS
    assert _SECRET_VALUE not in (out / cli.DENYLIST_FILE_NAME).read_text()
    captured = capsys.readouterr()
    assert captured.out == "denylist: written=8 short_dropped=3 exempted=0\n"
    assert captured.err == ""
    assert (out / cli.DENYLIST_FILE_NAME).stat().st_mode & 0o777 == 0o600


def test_a_record_whose_own_superseded_by_is_set_is_not_live(
    private_root: Path, tmp_path: Path
) -> None:
    parsed = graph_source.read_graph(_ontology(tmp_path, _SELF_SUPERSEDED))
    assert "Retired Watering Schedule" not in graph_source.live_entity_labels(parsed)

    out = private_root / "denylist-out"
    assert _run(tmp_path, out, extra_records=_SELF_SUPERSEDED) == 0
    assert _written(out) == _EXPECTED_WITHOUT_EXEMPTIONS


def test_denylist_tightens_an_existing_output_file(
    private_root: Path, tmp_path: Path
) -> None:
    out = private_root / "denylist-out"
    out.mkdir()
    target = out / cli.DENYLIST_FILE_NAME
    target.write_text("old longer synthetic content" * 30)
    target.chmod(0o644)
    assert _run(tmp_path, out) == 0
    assert _written(out) == _EXPECTED_WITHOUT_EXEMPTIONS
    assert target.stat().st_mode & 0o777 == 0o600


def test_private_cli_output_closes_if_permission_tightening_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "synthetic-output.txt"
    target.write_text("leave unchanged")
    descriptors: list[int] = []

    def refuse(descriptor: int, mode: int) -> None:
        descriptors.append(descriptor)
        raise OSError("synthetic permission failure")

    monkeypatch.setattr(cli.os, "fchmod", refuse)
    with pytest.raises(cli.InputRefusal) as excinfo:
        cli._write_private_text(target, "new synthetic content")
    assert excinfo.value.state == cli.OUTPUT_UNWRITABLE
    assert target.read_text() == "leave unchanged"
    (descriptor,) = descriptors
    with pytest.raises(OSError):
        cli.os.fstat(descriptor)


def test_an_unreadable_snapshot_is_a_refusal_not_a_traceback(
    private_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = _snapshot(tmp_path)
    out = private_root / "denylist-out"
    snapshot.chmod(0)
    try:
        assert _run(tmp_path, out, snapshot=snapshot) == 1
    finally:
        snapshot.chmod(0o600)

    assert not (out / cli.DENYLIST_FILE_NAME).exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{sqlite_source.SNAPSHOT_UNREADABLE}: ")
    assert _SECRET_VALUE not in captured.err


def test_a_deeply_nested_json_harvest_is_a_refusal_not_a_traceback(
    private_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harvest = tmp_path / "inputs" / "synthetic-harvest-nested.json"
    harvest.parent.mkdir(parents=True, exist_ok=True)
    depth = 200_000
    harvest.write_text("[" * depth + '"synthetic nested"' + "]" * depth)
    out = private_root / "denylist-out"

    assert _run(tmp_path, out, harvest=harvest) == 1

    assert not (out / cli.DENYLIST_FILE_NAME).exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{cli.HARVEST_UNREADABLE}: ")
    assert "synthetic nested" not in captured.err


def test_the_floor_drops_four_characters_and_keeps_five(
    private_root: Path, tmp_path: Path
) -> None:
    out = private_root / "denylist-out"

    assert _run(tmp_path, out) == 0

    written = _written(out)
    assert "kale" not in written
    assert "beets" in written


def test_a_live_head_with_valid_until_set_is_in_the_denylist(
    private_root: Path, tmp_path: Path
) -> None:
    """The fixture's deadline record (ent-006) carries a non-null ``valid_until``
    and is never superseded, so it migrates and its label must be denied."""
    out = private_root / "denylist-out"

    assert _run(tmp_path, out) == 0

    assert "draft seed order" in _written(out)


def test_denylist_drops_an_exempt_line_that_differs_only_in_case_and_spacing(
    private_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = private_root / "denylist-out"
    out.mkdir()
    (out / cli.EXEMPT_FILE_NAME).write_text("example  widget co\n\nCOMPOST rota\n")

    assert _run(tmp_path, out) == 0

    written = _written(out)
    assert "example widget co" not in written
    assert "compost rota" not in written
    assert written == [
        "beets",
        "draft seed order",
        "greenhouse",
        "new greenhouse rota",
        "sample gardener north",
        "seed library hours",
    ]
    assert capsys.readouterr().out == (
        "denylist: written=6 short_dropped=3 exempted=2\n"
    )


@pytest.mark.parametrize("shape", ["not-utf8", "directory"])
def test_an_unreadable_exempt_file_is_a_refusal_that_never_quotes_it(
    private_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], shape: str
) -> None:
    out = private_root / "denylist-out"
    out.mkdir()
    exempt = out / cli.EXEMPT_FILE_NAME
    if shape == "directory":
        exempt.mkdir()
    else:
        exempt.write_bytes(b"synthetic exempt line \xff\xfe\n")

    assert _run(tmp_path, out) == 1

    assert not (out / cli.DENYLIST_FILE_NAME).exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{cli.INPUT_UNREADABLE}: ")
    assert "synthetic exempt line" not in captured.err


def test_a_harvest_with_no_query_strings_is_refused(
    private_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rows that carry no top-level ``query`` (the wrong export shape) must not
    silently yield a denylist with every harvested query missing."""
    harvest = tmp_path / "inputs" / "synthetic-harvest-shape.json"
    harvest.parent.mkdir(parents=True, exist_ok=True)
    harvest.write_text(
        json.dumps([{"tool": "entity_search", "args": {"query": "hidden phrase"}}] * 3)
    )
    out = private_root / "denylist-out"

    assert _run(tmp_path, out, harvest=harvest) == 1

    assert not (out / cli.DENYLIST_FILE_NAME).exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{cli.HARVEST_EMPTY}: no query string in 3 ")
    assert "hidden phrase" not in captured.err


def test_a_malformed_csv_harvest_is_a_refusal_not_a_traceback(
    private_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A field over the csv module's size limit raises ``csv.Error``."""
    harvest = tmp_path / "inputs" / "synthetic-harvest-oversize.csv"
    harvest.parent.mkdir(parents=True, exist_ok=True)
    oversized = "synthetic" * 20_000
    harvest.write_text(f"tool,query\nentity_search,{oversized}\n")
    out = private_root / "denylist-out"

    assert _run(tmp_path, out, harvest=harvest) == 1

    assert not (out / cli.DENYLIST_FILE_NAME).exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{cli.HARVEST_UNREADABLE}: ")
    assert "synthetic" * 3 not in captured.err


def test_an_unwritable_denylist_path_is_a_refusal_not_a_traceback(
    private_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = private_root / "denylist-out"
    (out / cli.DENYLIST_FILE_NAME).mkdir(parents=True)

    assert _run(tmp_path, out) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{cli.OUTPUT_UNWRITABLE}: ")
    assert "greenhouse" not in captured.err


def test_denylist_refuses_an_out_dir_outside_the_private_root(
    private_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "outside"

    assert _run(tmp_path, outside) == 1

    assert not outside.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{private_paths.OUTPUT_ESCAPES_ROOT}: ")


def test_denylist_refuses_when_no_private_root_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(private_paths.PRIVATE_ROOT_VARIABLE, raising=False)
    out = tmp_path / "anywhere"

    assert _run(tmp_path, out) == 1

    assert not out.exists()
    assert capsys.readouterr().err.startswith(f"{private_paths.PRIVATE_ROOT_UNSET}: ")


# --- normalizer parity with the leak scan --------------------------------------------


def _load_leak_scan() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_migration_outputs", _LEAK_SCAN_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "text",
    [
        "Example Widget Co",
        "  EXAMPLE\twidget\n\nco  ",
        "tab\tseparated\tlabel",
        "line\r\nbreaks\vand\fform feeds",
        "non breaking em　ideographic line sep",
        "\x1cfile\x1dgroup\x1erecord\x1funit separators",
        "  leading and trailing  ",
        "Straße ǅemal ΣΊΣΥΦΟΣ",
        "",
        " \t\n ",
    ],
)
def test_the_denylist_normalizer_matches_the_leak_scans(text: str) -> None:
    """The leak scan is the reference: a denylist line and a scanned line must be
    put through the same normalization, or an exempt or denied line can miss."""
    leak_scan = _load_leak_scan()
    assert cli.normalize(text) == leak_scan._normalize(text)


def test_the_console_script_is_declared_in_the_recallatron_distribution() -> None:
    from importlib.metadata import entry_points

    (script,) = [
        ep
        for ep in entry_points(group="console_scripts")
        if ep.name == "recallatron-migrate"
    ]
    assert script.value == "rheo_recallatron.migration.cli:main"
    assert script.dist is not None and script.dist.name == "rheo-recallatron"
