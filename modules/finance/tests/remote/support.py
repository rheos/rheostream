"""Helpers for the remote tests: a layout under ``tmp_path``, hand-built streams, a
snapshot of a tree that a copy-only step must leave as it was, and ``FakeRemote``,
which puts ``fake_ssh.py`` first on ``PATH`` as ``ssh``.

No fixtures here (``tests/guards/test_test_layout.py``): tests call these with their
own ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from rheo_finance import db
from rheo_finance.remote.frames import MAGIC
from rheo_finance.remote.receive import STAGING_NAME

TreeRow = tuple[str, str, int, int, int]

# Placeholders only (ADR-0026): no real host, user, project or workspace.
DESTINATION = "finance-host"
PROJECT = "sample-project"
SERVICE = "worker"
WORKSPACE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CONTAINER = "cafebabecafe"
FAKE_SSH = Path(__file__).resolve().with_name("fake_ssh.py")


def write_config(
    path: Path, values: Mapping[str, object], *, mode: int = 0o600
) -> Path:
    """A TOML config with ``values`` (strings and lists of strings), at ``mode``."""
    path.write_text(
        "".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    path.chmod(mode)
    return path


def config_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "destination": DESTINATION,
        "compose_project": PROJECT,
        "compose_service": SERVICE,
        "workspace_id": WORKSPACE,
    }
    values.update(overrides)
    return values


@dataclass(frozen=True)
class FakeRemote:
    """A remote host made of a script: see ``fake_ssh.py``."""

    root: Path
    config: Path
    layout: db.FinanceLayout
    log: Path
    bin: Path

    @classmethod
    def install(
        cls,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        destination: str = DESTINATION,
        docker: Sequence[str] = ("docker",),
    ) -> FakeRemote:
        layout = make_layout(tmp_path / "remote_uploads")
        config = write_config(
            tmp_path / "remote.toml",
            config_values(destination=destination, docker=list(docker)),
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        ssh = bin_dir / "ssh"
        ssh.write_text(
            "#!/bin/sh\n"
            f'exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_SSH))} "$@"\n',
            encoding="utf-8",
        )
        ssh.chmod(0o700)
        log = tmp_path / "ssh.log"
        log.write_text("", encoding="utf-8")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        environment = {
            "FAKE_SSH_LOG": str(log),
            "FAKE_SSH_DEST": destination,
            "FAKE_SSH_CONTAINERS": CONTAINER,
            "FAKE_SSH_UPLOADS": str(layout.root.parent),
            "FAKE_SSH_DOCKER": " ".join(docker),
            "FAKE_SSH_PROJECT": PROJECT,
            "FAKE_SSH_SERVICE": SERVICE,
            "FAKE_SSH_WORKSPACE": WORKSPACE,
            "FAKE_SSH_FAULT": "",
            "FAKE_SSH_EXIT": "0",
        }
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("FINANCE_REMOTE_CONFIG", raising=False)
        return cls(root=tmp_path, config=config, layout=layout, log=log, bin=bin_dir)

    def calls(self) -> list[list[str]]:
        """Each ssh call's argument list, in order."""
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [list(json.loads(line)["argv"]) for line in lines]


def make_layout(uploads: Path) -> db.FinanceLayout:
    """``db.layout_under(uploads)`` with its five directories made, 0700."""
    layout = db.layout_under(uploads)
    for directory in layout.directories:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return layout


def push_areas(layout: db.FinanceLayout) -> dict[str, Path]:
    return {
        "archive": layout.archive,
        "inbox": layout.inbox,
        "data": layout.data,
        "keys": layout.keys,
    }


def frame(files: Sequence[tuple[str, bytes]], *, end: bool = True) -> bytes:
    """A well-formed stream for ``files`` (name, bytes), with or without its end."""
    out = bytearray(MAGIC + b"\n")
    for path, body in files:
        out += b"F %d " % len(body) + path.encode("utf-8", "surrogateescape") + b"\n"
        out += body + b"H " + hashlib.sha256(body).hexdigest().encode() + b"\n"
    if end:
        out += b"E %d\n" % len(files)
    return bytes(out)


def tree_state(root: Path) -> list[TreeRow]:
    """``(relpath, sha256 or link target, mode, size, mtime_ns)`` of every entry under
    ``root``, sorted; links are listed and never followed."""
    rows: list[TreeRow] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(current) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                what = "link:" + os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                try:
                    what = hashlib.sha256(path.read_bytes()).hexdigest()
                except PermissionError:
                    what = "unreadable"
            else:
                what = ""
            rows.append(
                (
                    str(path.relative_to(root)),
                    what,
                    info.st_mode,
                    info.st_size,
                    info.st_mtime_ns,
                )
            )
    return sorted(rows)


def staging_entries(root: Path) -> list[str]:
    """The names directly inside ``root``'s staging directory."""
    return sorted(os.listdir(root / STAGING_NAME))


def staged_leftovers(root: Path) -> list[str]:
    """Every staged file under a ``run-*`` directory, as ``run-*/<n>`` names."""
    staging = root / STAGING_NAME
    if not staging.is_dir():
        return []
    return sorted(
        str(path.relative_to(staging))
        for path in staging.glob("run-*/*")
        if path.is_file()
    )
