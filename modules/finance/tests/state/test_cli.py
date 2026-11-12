"""``finance state export`` and ``import`` files (L12): modes, a non-empty ``--out``,
and a changed byte or count refused before anything is dispatched. ``import
--pushed`` reads the bound layout's ``data/transfer`` after binding it.

No database: ``composition`` and ``operations.dispatch`` are replaced by fakes.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from rheo_finance import composition, db, operations
from rheo_finance.cli import state as state_cli
from rheo_finance.cli.main import main
from rheo_finance.state.transfer import ExportOut, ImportOut, TableCount, TableState

LINES = {
    "decision": ('[["id",["uuid","x"]]]', '[["id",["uuid","y"]]]'),
    "rate": (),
}


def written(tmp_path: Path) -> Path:
    out = tmp_path / "export"
    state_cli.write_export(
        out,
        schema_head="head-synthetic",
        derived_digest="0" * 64,
        tables=list(LINES.items()),
    )
    return out


def test_an_export_directory_is_private_and_reads_back(tmp_path: Path) -> None:
    out = written(tmp_path)
    for directory in (out, out / "state"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    files = [out / "manifest.json", *sorted((out / "state").iterdir())]
    assert [each.name for each in files[1:]] == ["decision.jsonl", "rate.jsonl"]
    for each in files:
        assert stat.S_IMODE(each.stat().st_mode) == 0o600
    assert (out / "state" / "decision.jsonl").read_bytes().endswith(b"\n")
    assert (out / "state" / "rate.jsonl").read_bytes() == b""
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "finance-state/1"
    assert [(t["name"], t["rows"]) for t in manifest["tables"]] == [
        ("decision", 2),
        ("rate", 0),
    ]
    head, tables = state_cli.read_export(out)
    assert head == "head-synthetic"
    assert dict(tables) == LINES


def test_one_changed_byte_is_a_mismatch(tmp_path: Path) -> None:
    out = written(tmp_path)
    target = out / "state" / "decision.jsonl"
    body = bytearray(target.read_bytes())
    body[5] ^= 0x01
    target.write_bytes(bytes(body))
    with pytest.raises(state_cli.StateFilesRefused, match="manifest_mismatch"):
        state_cli.read_export(out)


def test_a_changed_count_is_a_mismatch(tmp_path: Path) -> None:
    out = written(tmp_path)
    path = out / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["tables"][0]["rows"] = 3
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(state_cli.StateFilesRefused, match="manifest_mismatch"):
        state_cli.read_export(out)


@pytest.mark.parametrize(
    ("index", "rows"),
    [(0, 2.0), (0, "2"), (1, False), (0, None)],
    ids=["float", "string", "bool", "null"],
)
def test_a_count_that_is_not_a_strict_integer_is_a_mismatch(
    tmp_path: Path, index: int, rows: object
) -> None:
    # Each value equals the table's real count (2 or 0) under ==, except the string.
    out = written(tmp_path)
    path = out / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["tables"][index]["rows"] = rows
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(state_cli.StateFilesRefused, match="manifest_mismatch"):
        state_cli.read_export(out)


def test_a_missing_file_or_a_path_name_is_a_mismatch(tmp_path: Path) -> None:
    out = written(tmp_path)
    path = out / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["tables"][0]["name"] = "../decision"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(state_cli.StateFilesRefused, match="manifest_mismatch"):
        state_cli.read_export(out)
    (tmp_path / "again").mkdir()
    fresh = written(tmp_path / "again")
    (fresh / "state" / "rate.jsonl").unlink()
    with pytest.raises(state_cli.StateFilesRefused, match="manifest_mismatch"):
        state_cli.read_export(fresh)


@pytest.mark.parametrize(
    "which", ["export", "manifest.json", "state", "state/rate.jsonl"]
)
def test_nothing_is_read_through_a_symlink(tmp_path: Path, which: str) -> None:
    out = written(tmp_path)
    # The bytes behind each link are the export's own, so only the link refuses.
    path = tmp_path / "export" if which == "export" else out / which
    moved = tmp_path / "moved"
    path.rename(moved)
    path.symlink_to(moved)
    with pytest.raises(state_cli.StateFilesRefused, match="state_file_refused"):
        state_cli.read_export(out)


@pytest.mark.parametrize("depth", [1, 2], ids=["parent", "grandparent"])
def test_nothing_is_read_through_a_symlinked_ancestor(
    tmp_path: Path, depth: int
) -> None:
    # tmp_path is already resolved, so the only link on the path is the one made here.
    real = tmp_path / "real"
    inner = real / "inner" if depth == 2 else real
    inner.mkdir(parents=True)
    written(inner)
    (tmp_path / "link").symlink_to(real)
    through = tmp_path / "link" / ("inner/export" if depth == 2 else "export")
    assert state_cli.read_export(inner / "export")[0] == "head-synthetic", "control"
    with pytest.raises(state_cli.StateFilesRefused, match="state_file_refused"):
        state_cli.read_export(through)


def test_a_relative_path_reads_the_same_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert dict(state_cli.read_export(Path("export"))[1]) == LINES


def test_a_file_where_a_directory_belongs_is_refused(tmp_path: Path) -> None:
    out = written(tmp_path)
    for each in (out / "state").iterdir():
        each.unlink()
    (out / "state").rmdir()
    (out / "state").write_bytes(b"")
    with pytest.raises(state_cli.StateFilesRefused, match="state_file_refused"):
        state_cli.read_export(out)


def test_a_table_file_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    out = written(tmp_path)
    table = out / "state" / "rate.jsonl"
    table.unlink()
    os.mkfifo(table, 0o600)
    with pytest.raises(state_cli.StateFilesRefused, match="state_file_refused"):
        state_cli.read_export(out)


def test_a_file_that_fails_once_open_is_refused_with_the_code_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = written(tmp_path)

    def failing(_fd: int) -> os.stat_result:
        raise OSError(5, "synthetic input/output error", "/synthetic/path")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", failing)
        with pytest.raises(state_cli.StateFilesRefused) as caught:
            state_cli.read_export(out)
    assert str(caught.value) == "state_file_refused"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def _refuse(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("reached past the file checks")


def test_import_refuses_a_changed_file_before_any_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = written(tmp_path)
    (out / "state" / "decision.jsonl").write_bytes(b"[]\n")
    monkeypatch.setattr(composition, "build_context", _refuse)
    monkeypatch.setattr(operations, "dispatch", _refuse)
    assert main(["state", "import", "--from", str(out)]) == 1
    assert "manifest_mismatch" in capsys.readouterr().err


def test_export_refuses_a_non_empty_out_before_any_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "busy"
    out.mkdir()
    (out / "left.txt").write_text("synthetic", encoding="utf-8")
    monkeypatch.setattr(composition, "build_context", _refuse)
    monkeypatch.setattr(operations, "dispatch", _refuse)
    assert main(["state", "export", "--out", str(out)]) == 1
    assert "out_not_empty" in capsys.readouterr().err
    with pytest.raises(state_cli.StateFilesRefused, match="out_not_empty"):
        state_cli.write_export(out, schema_head="h", derived_digest="d", tables=[])


def test_import_pushed_reads_the_bound_layouts_transfer_after_binding_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    layout = db.layout_under(tmp_path / "uploads")
    layout.data.mkdir(parents=True)
    state_cli.write_export(
        layout.data / "transfer",
        schema_head="head-synthetic",
        derived_digest="0" * 64,
        tables=list(LINES.items()),
    )
    tokens: list[db.LayoutToken] = []
    calls: list[str] = []

    def build(_role: object) -> object:
        calls.append("build_context")
        tokens.append(db.use_layout(layout))
        return object()

    def dispatch(name: str, payload: Any, _role: object, *, ctx: object) -> Any:
        calls.append(name)
        return ImportOut(
            tables=tuple(
                TableCount(name=each["name"], rows=len(each["lines"]))
                for each in payload["tables"]
            )
        )

    monkeypatch.setattr(composition, "build_context", build)
    monkeypatch.setattr(operations, "dispatch", dispatch)
    try:
        assert main(["state", "import", "--pushed"]) == 0
    finally:
        for token in reversed(tokens):
            db.release_layout(token)
    assert calls == ["build_context", "finance.state.import"]
    assert "2 tables, 2 rows" in capsys.readouterr().out


def test_import_takes_one_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(composition, "build_context", _refuse)
    out = written(tmp_path)
    assert main(["state", "import", "--pushed", "--from", str(out)]) == 2
    assert main(["state", "import"]) == 2
    assert "one of the arguments --from --pushed is required" in capsys.readouterr().err


def test_export_then_import_through_the_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, Any]] = []
    exported = ExportOut(
        schema_head="head-synthetic",
        derived_digest="1" * 64,
        tables=tuple(
            TableState(name=name, rows=len(lines), sha256="", lines=lines)
            for name, lines in LINES.items()
        ),
    )

    def dispatch(name: str, payload: Any, _role: object, *, ctx: object) -> Any:
        calls.append((name, payload))
        if name == "finance.state.export":
            return exported
        return ImportOut(
            tables=tuple(
                TableCount(name=each["name"], rows=len(each["lines"]))
                for each in payload["tables"]
            )
        )

    monkeypatch.setattr(composition, "build_context", lambda _role: object())
    monkeypatch.setattr(operations, "dispatch", dispatch)
    out = tmp_path / "fresh"
    assert main(["state", "export", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "2 tables, 2 rows" in printed and "txn" not in printed
    assert main(["state", "import", "--from", str(out)]) == 0
    assert "run finance rebuild" in capsys.readouterr().out
    assert calls[1] == (
        "finance.state.import",
        {
            "schema_head": "head-synthetic",
            "tables": [
                {"name": name, "lines": list(lines)} for name, lines in LINES.items()
            ],
        },
    )
