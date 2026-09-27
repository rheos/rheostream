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
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
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
_SECRET_VALUE = "synthetic-secret-value-0000"
_MEMORY_LABELS = [
    "Seed Library Hours",
    "tea",
    "EXAMPLE widget   co",
    "  Compost   Rota  ",
]


def _snapshot(tmp_path: Path) -> Path:
    path = tmp_path / "inputs" / "synthetic-snapshot.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(_DDL)
    connection.executemany(
        "INSERT INTO memory_items (type, label, properties, ts, superseded_by) "
        "VALUES ('fact', ?, '{}', '2026-01-01T00:00:00.000Z', NULL)",
        [(label,) for label in _MEMORY_LABELS],
    )
    connection.execute("UPDATE memory_items SET superseded_by = 1 WHERE id = 2")
    connection.execute(
        "INSERT INTO app_secret VALUES ('synthetic-key', ?)", (_SECRET_VALUE,)
    )
    connection.commit()
    connection.close()
    return path


def _ontology(tmp_path: Path) -> Path:
    directory = tmp_path / "inputs" / "ontology"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_GRAPH_FIXTURE, directory / graph_source.GRAPH_FILE_NAME)
    return directory


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
    rows = [{"tool": "entity_search", "query": query} for query in _HARVEST_QUERIES]
    if suffix == ".json":
        path.write_text(json.dumps(rows))
    else:
        lines = ["tool,query"] + [f"entity_search,{q or ''}" for q in _HARVEST_QUERIES]
        path.write_text("\n".join(lines) + "\n")
    return path


def _ignored_root(tmp_path: Path) -> Path:
    repo = tmp_path / "private-home"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("private/\n")
    root = repo / "private"
    root.mkdir()
    return root


@pytest.fixture
def private_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = _ignored_root(tmp_path)
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    return root


def _run(tmp_path: Path, out: Path, suffix: str = ".json") -> int:
    return cli.main(
        [
            "denylist",
            "--snapshot",
            str(_snapshot(tmp_path)),
            "--ontology",
            str(_ontology(tmp_path)),
            "--harvest",
            str(_harvest(tmp_path, suffix)),
            "--out",
            str(out),
        ]
    )


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


def test_live_labels_take_the_last_record_and_skip_superseded_and_expired(
    tmp_path: Path,
) -> None:
    parsed = graph_source.read_graph(_ontology(tmp_path))
    assert graph_source.live_entity_labels(parsed) == [
        "Example Widget Co",
        "Sample  Gardener North",
        "New Greenhouse Rota",
        "Tea",
    ]


# --- the denylist subcommand ---------------------------------------------------------

_EXPECTED_WITHOUT_EXEMPTIONS = [
    "compost rota",
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
    """ "tea" (graph and memory_items) and "ph" (harvest) are the two distinct lines
    under five characters: dropped, and only their count is printed."""
    out = private_root / "denylist-out"

    assert _run(tmp_path, out, suffix) == 0

    written = (out / cli.DENYLIST_FILE_NAME).read_text().splitlines()
    assert written == _EXPECTED_WITHOUT_EXEMPTIONS
    assert _SECRET_VALUE not in (out / cli.DENYLIST_FILE_NAME).read_text()
    captured = capsys.readouterr()
    assert captured.out == "denylist: written=6 short_dropped=2 exempted=0\n"
    assert captured.err == ""
    assert (out / cli.DENYLIST_FILE_NAME).stat().st_mode & 0o777 == 0o600


def test_denylist_drops_an_exempt_line_that_differs_only_in_case_and_spacing(
    private_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = private_root / "denylist-out"
    out.mkdir()
    (out / cli.EXEMPT_FILE_NAME).write_text("example  widget co\n\nCOMPOST rota\n")

    assert _run(tmp_path, out) == 0

    written = (out / cli.DENYLIST_FILE_NAME).read_text().splitlines()
    assert "example widget co" not in written
    assert "compost rota" not in written
    assert written == [
        "greenhouse",
        "new greenhouse rota",
        "sample gardener north",
        "seed library hours",
    ]
    assert capsys.readouterr().out == (
        "denylist: written=4 short_dropped=2 exempted=2\n"
    )


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


def test_the_console_script_is_declared_in_the_recallatron_distribution() -> None:
    from importlib.metadata import entry_points

    (script,) = [
        ep
        for ep in entry_points(group="console_scripts")
        if ep.name == "recallatron-migrate"
    ]
    assert script.value == "rheo_recallatron.migration.cli:main"
    assert script.dist is not None and script.dist.name == "rheo-recallatron"
