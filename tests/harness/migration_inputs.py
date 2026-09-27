"""Synthetic inputs shared by the migration-groundwork tests.

Plain helpers, no module import: this package is part of the absence-proof import
surface (``tests/test_module_import_graph.py``), so nothing here may name the module
under test. Callers pass the private-root variable's name in.
"""

import sqlite3
import subprocess
from pathlib import Path

import pytest

#: The one ``app_secret`` value every synthetic snapshot carries. No reader, output
#: or refusal may ever contain it.
SECRET_VALUE = "synthetic-secret-value-0000"


def ignored_private_root(tmp_path: Path) -> Path:
    """A ``private/`` directory that an unrelated temp git repo ignores."""
    repo = tmp_path / "private-home"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("private/\n")
    root = repo / "private"
    root.mkdir()
    return root


def use_private_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str
) -> Path:
    root = ignored_private_root(tmp_path)
    monkeypatch.setenv(variable, str(root))
    return root


def new_snapshot(
    tmp_path: Path, ddl: str, *, wal: bool = False
) -> tuple[Path, sqlite3.Connection]:
    """``<tmp_path>/inputs/synthetic-snapshot.db`` built from ``ddl`` (which must
    create ``app_secret``), with :data:`SECRET_VALUE` inserted. The caller adds its
    own rows, then commits and closes the returned connection."""
    path = tmp_path / "inputs" / "synthetic-snapshot.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(ddl)
    connection.execute(
        "INSERT INTO app_secret VALUES ('synthetic-key', ?)", (SECRET_VALUE,)
    )
    return path, connection
