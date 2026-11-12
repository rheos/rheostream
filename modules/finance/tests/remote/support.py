"""Helpers for the remote tests: a layout under ``tmp_path``, hand-built streams, and a
snapshot of a tree that a copy-only step must leave as it was.

No fixtures here (``tests/guards/test_test_layout.py``): tests call these with their
own ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Sequence
from pathlib import Path

from rheo_finance import db
from rheo_finance.remote.frames import MAGIC
from rheo_finance.remote.receive import STAGING_NAME

TreeRow = tuple[str, str, int, int, int]


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
