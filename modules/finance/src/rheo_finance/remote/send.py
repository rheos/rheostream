"""What a push or a pull sends: collected before any connection, streamed once.

``collect`` turns the push flags into the files to send, sorted by name:

- ``--archive DIR`` and ``--inbox DIR``: every regular file, recursively, as
  ``archive/<rel>`` and ``inbox/<rel>``;
- ``--data DIR``: ``settings.toml``, ``identities.toml`` and ``manual/**`` only, as
  ``data/...`` (``db.env``, ``dumps/`` and the rest are never taken);
- ``--state DIR``: a ``finance state export`` directory that reads back whole, as
  ``data/transfer/manifest.json`` and ``data/transfer/state/<table>.jsonl``;
- ``--keys DIR``: its top-level files, each readable by its owner alone, as
  ``keys/<name>``.

Hidden entries and links are skipped and counted, as the archive walk skips them.
Sources are only ever read: never opened for writing, renamed, removed or chmodded.
``send`` streams exactly the size each file had when opened; a file that grew or
shrank stops the stream with no end record (``source_changed``), and so does a source
whose ``fstat`` or ``read`` fails (``source_unreadable``), so the receiver keeps
nothing. ``send_packs`` is the server half of a pull: ``data/packs/**``.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from rheo_finance import db
from rheo_finance.cli import state as state_files
from rheo_finance.remote.frames import (
    ABORT_CODES,
    CHUNK,
    Direction,
    RemoteRefused,
    Writable,
    check_path,
    write_abort,
    write_end,
    write_hash,
    write_header,
    write_magic,
)

_SOURCE_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_DATA_FILES: Final = ("identities.toml", "settings.toml")
_DATA_TREE: Final = "manual"


@dataclass(frozen=True)
class Outgoing:
    """One file to send: its name in the stream and where it is read from."""

    path: str
    local: Path


@dataclass(frozen=True)
class Sources:
    archive: Path | None = None
    inbox: Path | None = None
    data: Path | None = None
    state: Path | None = None
    keys: Path | None = None


@dataclass(frozen=True)
class Collected:
    items: tuple[Outgoing, ...]
    skipped: int


def _check_root(root: Path, missing: str = "source_missing") -> None:
    """``root`` must be a directory and not a link."""
    try:
        mode = os.lstat(root).st_mode
    except OSError:
        raise RemoteRefused(missing) from None
    if not stat.S_ISDIR(mode):
        raise RemoteRefused(missing)


def _probe(local: Path) -> int:
    """The mode of a regular file that can be opened for reading, without following a
    link; the file is closed again at once."""
    try:
        fd = os.open(local, _SOURCE_FLAGS)
    except OSError:
        raise RemoteRefused("source_unreadable") from None
    try:
        mode = os.fstat(fd).st_mode
    finally:
        os.close(fd)
    if not stat.S_ISREG(mode):
        raise RemoteRefused("source_not_regular")
    return mode


class _Walker:
    def __init__(self, direction: Direction) -> None:
        self.direction: Direction = direction
        self.items: list[Outgoing] = []
        self.skipped = 0

    def entries(self, directory: Path) -> list[os.DirEntry[str]]:
        try:
            with os.scandir(directory) as found:
                return sorted(found, key=lambda entry: entry.name)
        except OSError:
            raise RemoteRefused("source_unreadable") from None

    def add(self, local: Path, path: str, *, owner_only: bool = False) -> None:
        check_path(path, self.direction)
        mode = _probe(local)
        if owner_only and mode & 0o077:
            raise RemoteRefused("key_mode_refused")
        self.items.append(Outgoing(path=path, local=local))

    def entry(
        self, entry: os.DirEntry[str], prefix: str, *, recurse: bool, owner_only: bool
    ) -> None:
        if entry.name.startswith(".") or entry.is_symlink():
            self.skipped += 1
            return
        try:
            mode = entry.stat(follow_symlinks=False).st_mode
        except OSError:
            raise RemoteRefused("source_unreadable") from None
        path = f"{prefix}/{entry.name}"
        if stat.S_ISDIR(mode) and recurse:
            self.tree(Path(entry.path), path)
        elif stat.S_ISREG(mode):
            self.add(Path(entry.path), path, owner_only=owner_only)
        else:
            raise RemoteRefused("source_not_regular")

    def tree(self, directory: Path, prefix: str) -> None:
        for each in self.entries(directory):
            self.entry(each, prefix, recurse=True, owner_only=False)


def collect(sources: Sources) -> Collected:
    """Every file the flags name, sorted by its encoded name; refused before any
    connection when a source cannot be sent whole."""
    walker = _Walker("push")
    for root, prefix in ((sources.archive, "archive"), (sources.inbox, "inbox")):
        if root is not None:
            _check_root(root)
            walker.tree(root, prefix)
    if sources.data is not None:
        _check_root(sources.data)
        for each in walker.entries(sources.data):
            if each.name in _DATA_FILES:
                walker.entry(each, "data", recurse=False, owner_only=False)
            elif each.name == _DATA_TREE:
                walker.entry(each, "data", recurse=True, owner_only=False)
    if sources.state is not None:
        _check_root(sources.state)
        try:
            _head, tables = state_files.read_export(sources.state)
        except state_files.StateFilesRefused:
            raise RemoteRefused("state_export_invalid") from None
        walker.add(
            sources.state / state_files.MANIFEST_NAME,
            f"data/transfer/{state_files.MANIFEST_NAME}",
        )
        for name, _lines in tables:
            walker.add(
                sources.state / state_files.STATE_DIR / f"{name}.jsonl",
                f"data/transfer/{state_files.STATE_DIR}/{name}.jsonl",
            )
    if sources.keys is not None:
        _check_root(sources.keys)
        for each in walker.entries(sources.keys):
            walker.entry(each, "keys", recurse=False, owner_only=True)
    if not walker.items:
        raise RemoteRefused("nothing_to_push")
    items = sorted(walker.items, key=lambda item: item.path.encode("utf-8"))
    return Collected(items=tuple(items), skipped=walker.skipped)


def _source_stat(fd: int) -> os.stat_result:
    """``fstat`` of an open local source; a failure (a failing disk, a stale network
    mount) is ``source_unreadable``, a local refusal that cuts the stream."""
    try:
        return os.fstat(fd)
    except OSError:
        raise RemoteRefused("source_unreadable") from None


def _source_read(fd: int, size: int) -> bytes:
    """One ``read`` of an open local source, mapped as ``_source_stat``. Only the
    source's own reads: a write to the stream is the caller's to handle."""
    try:
        return os.read(fd, size)
    except OSError:
        raise RemoteRefused("source_unreadable") from None


def _send_one(out: Writable, item: Outgoing) -> None:
    try:
        fd = os.open(item.local, _SOURCE_FLAGS)
    except OSError:
        raise RemoteRefused("source_changed") from None
    try:
        info = _source_stat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RemoteRefused("source_changed")
        write_header(out, info.st_size, item.path)
        digest = hashlib.sha256()
        left = info.st_size
        while left:
            piece = _source_read(fd, min(CHUNK, left))
            if not piece:
                raise RemoteRefused("source_changed")
            digest.update(piece)
            out.write(piece)
            left -= len(piece)
        if _source_read(fd, 1):
            raise RemoteRefused("source_changed")
        write_hash(out, digest.hexdigest())
    finally:
        os.close(fd)


def send(out: Writable, items: tuple[Outgoing, ...]) -> None:
    """The whole stream for ``items``; on ``source_changed`` or ``source_unreadable``
    it stops with no end record, and the caller closes it."""
    write_magic(out)
    for item in items:
        _send_one(out, item)
    write_end(out, len(items))
    out.flush()


def send_packs(out: Writable, layout: db.FinanceLayout) -> None:
    """The server half of a pull: every regular file under ``data/packs``. A source
    that cannot be sent is an ``X`` record, and nothing else is sent."""
    walker = _Walker("pull")
    try:
        packs = layout.data / "packs"
        _check_root(packs, missing="packs_missing")
        walker.tree(packs, "packs")
    except RemoteRefused as exc:
        write_magic(out)
        write_abort(out, exc.code if exc.code in ABORT_CODES else "io_failed")
        out.flush()
        return
    items = sorted(walker.items, key=lambda item: item.path.encode("utf-8"))
    send(out, tuple(items))
