"""The receiver: each file lands whole, and nothing lands before the commit step.

``receive`` runs in four steps, under one lock:

1. Staging. ``<staging_root>/.finance-remote-staging/`` (0700) holds ``lock`` and one
   ``run-<random>/`` directory per run. Holding the lock, anything else there is a
   killed run's leftover and is removed. Staged files are named ``0``, ``1``, ...: no
   name from the stream touches staging.
2. Receive all. Each file is written to staging as it arrives, synced and checked
   against its ``H`` record. Nothing touches an area until the stream ended cleanly.
3. Check all. Every target's parent is opened one component at a time without
   following a link, and must resolve inside its area. An existing target must be a
   regular file: the same bytes are skipped, different bytes are refused
   (``target_differs``, with the count) unless ``replaceable`` allows that path. A
   refusal here leaves every area as it was; no directory has been made.
4. Commit. New files are hard-linked into place, which never replaces a file that
   appeared since step 3 (``target_appeared``), and the staged name is removed. A
   replaceable path is renamed over its target. Each file lands atomically, never
   partly under its final name.

Finally the run directory is removed and the lock released. Files are 0600 and made
directories 0700; modification times are not carried. Any other ``OSError`` is
``io_failed``.

A refusal during the commit step, or after it, keeps the files already linked or
renamed and says how many: ``receive`` counts each file as it lands and puts that
count on whatever refusal ends the run, whatever its code. Before the commit step
nothing has landed, and the count is the refusal's own (``target_differs``'s
differing targets) or 0.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import shutil
import stat
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from rheo_finance.remote.frames import (
    CHUNK,
    Direction,
    FrameReader,
    Header,
    Readable,
    RemoteRefused,
)

STAGING_NAME: Final = ".finance-remote-staging"
LOCK_NAME: Final = "lock"
FILE_MODE: Final = 0o600
DIR_MODE: Final = 0o700

_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# Each component of a target's parent: never through a link.
_COMPONENT_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_TARGET_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_STAGE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

Replaceable = Callable[[str, PurePosixPath], bool]


@dataclass(frozen=True)
class Counts:
    new: int = 0
    identical: int = 0
    replaced: int = 0


@dataclass
class _Tally:
    """What the commit step has done so far, kept outside it so a refusal from any
    later point still knows how many files landed."""

    new: int = 0
    identical: int = 0
    replaced: int = 0

    @property
    def written(self) -> int:
        return self.new + self.replaced

    def counts(self) -> Counts:
        return Counts(new=self.new, identical=self.identical, replaced=self.replaced)


@dataclass
class _Staged:
    name: str
    header: Header
    digest: str
    action: str = "new"


def never(_area: str, _rest: PurePosixPath) -> bool:
    return False


def owner_inputs(area: str, rest: PurePosixPath) -> bool:
    """What ``--replace`` may replace: ``data/settings.toml``, ``data/identities.toml``
    and ``data/manual/**``, the owner's edited inputs. Never a statement, a key or a
    transfer file."""
    if area != "data":
        return False
    return str(rest) in ("settings.toml", "identities.toml") or (
        len(rest.parts) > 1 and rest.parts[0] == "manual"
    )


def receive(
    raw: Readable,
    areas: Mapping[str, Path],
    staging_root: Path,
    *,
    direction: Direction,
    replaceable: Replaceable = never,
    before_commit: Callable[[], None] | None = None,
) -> Counts:
    """Receive one stream into ``areas`` (area name to directory); see the module."""
    tally = _Tally()
    try:
        _receive(raw, areas, staging_root, direction, replaceable, before_commit, tally)
    except RemoteRefused as exc:
        if tally.written:
            raise RemoteRefused(exc.code, files=tally.written) from None
        raise
    except OSError:
        raise RemoteRefused("io_failed", files=tally.written) from None
    return tally.counts()


def _receive(
    raw: Readable,
    areas: Mapping[str, Path],
    staging_root: Path,
    direction: Direction,
    replaceable: Replaceable,
    before_commit: Callable[[], None] | None,
    tally: _Tally,
) -> None:
    with ExitStack() as stack:
        area_fds = {name: _open_area(path, stack) for name, path in areas.items()}
        stage_fd = _take_staging(staging_root, stack)
        run_name = f"run-{secrets.token_hex(8)}"
        os.mkdir(run_name, DIR_MODE, dir_fd=stage_fd)
        stack.callback(_remove_entry, run_name, stage_fd)
        run_fd = os.open(run_name, _DIR_FLAGS, dir_fd=stage_fd)
        stack.callback(os.close, run_fd)
        os.fchmod(run_fd, DIR_MODE)
        device = os.fstat(run_fd).st_dev
        if any(os.fstat(fd).st_dev != device for fd in area_fds.values()):
            raise RemoteRefused("staging_cross_device")
        staged = _receive_all(FrameReader(raw, direction), run_fd)
        _check_all(staged, areas, area_fds, replaceable)
        if before_commit is not None:
            before_commit()
        run_path = staging_root / STAGING_NAME / run_name
        _commit_all(staged, areas, area_fds, run_fd, run_path, tally)


def _open_area(path: Path, stack: ExitStack) -> int:
    try:
        fd = os.open(path, _DIR_FLAGS)
    except OSError:
        raise RemoteRefused("target_outside_layout") from None
    stack.callback(os.close, fd)
    return fd


def _take_staging(root: Path, stack: ExitStack) -> int:
    """The staging directory's descriptor, locked, with every leftover removed."""
    refused = RemoteRefused("staging_refused")
    try:
        root_fd = os.open(root, _DIR_FLAGS)
    except OSError:
        raise refused from None
    stack.callback(os.close, root_fd)
    try:
        os.mkdir(STAGING_NAME, DIR_MODE, dir_fd=root_fd)
    except FileExistsError:
        pass
    try:
        stage_fd = os.open(STAGING_NAME, _DIR_FLAGS, dir_fd=root_fd)
    except OSError:
        raise refused from None
    stack.callback(os.close, stage_fd)
    os.fchmod(stage_fd, DIR_MODE)
    try:
        lock_fd = os.open(
            LOCK_NAME,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            FILE_MODE,
            dir_fd=stage_fd,
        )
    except OSError:
        raise refused from None
    stack.callback(os.close, lock_fd)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RemoteRefused("staging_busy") from None
    stack.callback(fcntl.flock, lock_fd, fcntl.LOCK_UN)
    # Holding the lock, no other run is live: everything else is a dead run's.
    for name in os.listdir(stage_fd):
        if name != LOCK_NAME:
            _remove_entry(name, stage_fd)
    return stage_fd


def _remove_entry(name: str, dir_fd: int) -> None:
    """Remove ``name`` under ``dir_fd``; a directory's tree is removed without
    following any link in it."""
    try:
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode):
        shutil.rmtree(name, dir_fd=dir_fd)
    else:
        os.unlink(name, dir_fd=dir_fd)


def _receive_all(reader: FrameReader, run_fd: int) -> list[_Staged]:
    reader.start()
    staged: list[_Staged] = []
    while (header := reader.next_file()) is not None:
        name = str(len(staged))
        fd = os.open(name, _STAGE_FLAGS, FILE_MODE, dir_fd=run_fd)
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), FILE_MODE)
            for piece in reader.chunks():
                digest.update(piece)
                handle.write(piece)
            handle.flush()
            os.fsync(handle.fileno())
        reader.expect_hash(digest.hexdigest())
        staged.append(_Staged(name=name, header=header, digest=digest.hexdigest()))
    return staged


def _walk(start_fd: int, parts: tuple[str, ...]) -> int | None:
    """The directory ``parts`` names under ``start_fd``, opened a component at a time
    without following a link; ``None`` when a component does not exist yet."""
    fd = os.dup(start_fd)
    for part in parts:
        try:
            child = os.open(part, _COMPONENT_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            os.close(fd)
            return None
        except OSError:
            os.close(fd)
            raise RemoteRefused("target_outside_layout") from None
        os.close(fd)
        fd = child
    return fd


def _digest_of(parent_fd: int, name: str) -> str | None:
    """The sha256 of an existing regular target, ``None`` when there is none."""
    try:
        fd = os.open(name, _TARGET_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except PermissionError:
        raise RemoteRefused("io_failed") from None
    except OSError:
        raise RemoteRefused("target_not_regular") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RemoteRefused("target_not_regular")
        digest = hashlib.sha256()
        while piece := os.read(fd, CHUNK):
            digest.update(piece)
        return digest.hexdigest()
    finally:
        os.close(fd)


def _check_all(
    staged: list[_Staged],
    areas: Mapping[str, Path],
    area_fds: Mapping[str, int],
    replaceable: Replaceable,
) -> None:
    differs = 0
    for item in staged:
        header = item.header
        parent_fd = _walk(area_fds[header.area], header.rest.parent.parts)
        area = os.path.realpath(areas[header.area])
        parent = os.path.realpath(areas[header.area] / header.rest.parent)
        if not Path(parent).is_relative_to(area):
            if parent_fd is not None:
                os.close(parent_fd)
            raise RemoteRefused("target_outside_layout")
        if parent_fd is None:
            continue
        try:
            existing = _digest_of(parent_fd, header.rest.name)
        finally:
            os.close(parent_fd)
        if existing is None:
            continue
        if existing == item.digest:
            item.action = "identical"
        elif replaceable(header.area, header.rest):
            item.action = "replace"
        else:
            differs += 1
    if differs:
        raise RemoteRefused("target_differs", files=differs)


def _make_parents(start_fd: int, parts: tuple[str, ...]) -> int:
    """The directory ``parts`` names under ``start_fd``, made 0700 where missing,
    opened a component at a time without following a link."""
    fd = os.dup(start_fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, DIR_MODE, dir_fd=fd)
                made = True
            except FileExistsError:
                made = False
            try:
                child = os.open(part, _COMPONENT_FLAGS, dir_fd=fd)
            except OSError:
                raise RemoteRefused("target_outside_layout") from None
            if made:
                os.fchmod(child, DIR_MODE)
                os.fsync(fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _sync_close(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace(
    run_fd: int, staged: str, parent_fd: int, name: str, paths: tuple[Path, Path]
) -> None:
    """Rename the staged file over its target, in the parent ``_make_parents`` opened
    without following a link, so a component swapped for a link since is not used.

    CPython lists ``renameat`` under ``os.rename`` alone in ``os.supports_dir_fd``
    (never under ``os.replace``), and ``os.replace`` takes the same two descriptors
    wherever it is there (Linux and macOS both). Only a platform without ``renameat``
    falls back to the two paths."""
    if os.rename in os.supports_dir_fd:
        os.replace(staged, name, src_dir_fd=run_fd, dst_dir_fd=parent_fd)
    else:
        os.replace(*paths)


def _commit_all(
    staged: list[_Staged],
    areas: Mapping[str, Path],
    area_fds: Mapping[str, int],
    run_fd: int,
    run_path: Path,
    tally: _Tally,
) -> None:
    """Link or rename each file into place, counting each in ``tally`` the moment it
    lands; ``receive`` puts that count on any refusal from here on."""
    key: tuple[str, tuple[str, ...]] | None = None
    parent_fd: int | None = None
    try:
        for item in staged:
            header = item.header
            if item.action == "identical":
                tally.identical += 1
                continue
            here = (header.area, header.rest.parent.parts)
            if here != key or parent_fd is None:
                # Forget the descriptor before closing it, so a failed close is
                # never closed a second time below.
                done, parent_fd = parent_fd, None
                _sync_close(done)
                parent_fd = _make_parents(area_fds[header.area], here[1])
                key = here
            name = header.rest.name
            if item.action == "replace":
                target = areas[header.area] / header.rest
                _replace(
                    run_fd, item.name, parent_fd, name, (run_path / item.name, target)
                )
                tally.replaced += 1
                continue
            try:
                os.link(
                    item.name,
                    name,
                    src_dir_fd=run_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                if _digest_of(parent_fd, name) == item.digest:
                    tally.identical += 1
                    continue
                raise RemoteRefused("target_appeared") from None
            # Linked: it counts now, even if removing the staged name fails.
            tally.new += 1
            os.unlink(item.name, dir_fd=run_fd)
    finally:
        _sync_close(parent_fd)
