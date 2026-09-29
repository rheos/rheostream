"""The bridge worker's drain (spec § Architecture, System Components item 9).

One pass, under ``worker.lock``:

1. :func:`fold_spool` folds the hook's spool lines into ``session`` rows;
2. :func:`collect` validates each session's source and, for an admitted one,
3. reads complete lines from its committed cursor,
4. selects the human lines, and
5. sanitizes them locally, turning each into a record or an ``expired_pending``
   gap keyed by an HMAC under ``machine.key``.

What :func:`collect` returns is a list of :class:`SessionBatch`: candidates to
post, in line order, with the byte offset each one ends at. Nothing is posted
here, and the transcript cursor is not moved: a cursor moves only in the same
SQLite commit as the server's acknowledgement of the range it covers, so a
crash before that commit re-reads a range the server already holds and answers
identically. The one write :func:`collect` makes besides dropping refused
sessions is ``pending_gap_key``, the durable note that a vanished source owes
the server a gap.

No function here reads ``$HOME``, ``Path.home()``, ``~`` or the working
directory: ``bridge_home`` and ``projects_root`` are passed in, and the clock is
an injected callable.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final, assert_never

from rheo_core.evidence.sanitize import sanitize

from rheo_bridge import config as bridge_config
from rheo_bridge import keys, paths, state, transcript
from rheo_bridge.client import IngestClient, IngestGap, IngestRecord
from rheo_bridge.transcript import Missing, Ok, Refused

EXIT_OK: Final = 0
# The worker cannot run safely: a loose ``bridge_home``, no configuration, or
# no usable machine key. It touches no state.
EXIT_FAILURE: Final = 1

RECORD_KEY_PREFIX: Final = "cc1:"
GAP_KEY_PREFIX: Final = "cc1g:"

SPOOL_EVENTS: Final = frozenset({"Stop", "SessionEnd"})
_SPOOL_NAME: Final = re.compile(r"(\d{4}-\d{2}-\d{2})\.jsonl")
_READ_CHUNK: Final = 65_536
# O_NONBLOCK keeps a FIFO planted at a transcript path from blocking the open
# until a writer appears; it changes nothing for a regular file.
_OPEN_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


@dataclass(frozen=True)
class LineCandidate:
    """One line's record or ``expired_pending`` gap, and where that line ends."""

    item: IngestRecord | IngestGap
    # The byte offset just past this line's ``\n``.
    end_offset: int
    # The client that wrote the line, for the per-client ``accepted`` counters.
    # Kept locally; never sent. [capture 8] — may be revised at reconciliation.
    entrypoint: str | None


@dataclass(frozen=True)
class SessionBatch:
    """What one pass produced for one session, not yet posted."""

    session_hash: str
    # A ``source_truncated`` gap: the source vanished, became unreadable, or
    # was replaced or shrank since the stored cursor.
    source_gap: IngestGap | None
    items: tuple[LineCandidate, ...]
    # The read covered ``[start_offset, end_offset)`` of the file with inode
    # ``inode``. After a replacement ``start_offset`` is 0; when nothing was
    # read both equal the stored cursor. Every line in the range advances the
    # cursor, whether or not it made a candidate.
    start_offset: int
    end_offset: int
    inode: int | None
    # Human lines skipped for an unreadable clock; the ``clock_unreadable``
    # ledger count to commit with this range's cursor.
    clock_unreadable: int


@dataclass
class _Budget:
    """The local ``max_bytes``/``max_records`` limits, shared across one pass."""

    bytes_left: int
    items_left: int


# --- keys -------------------------------------------------------------------


def record_key(machine_key: bytes, session_id: str, line_uuid: str) -> str:
    """``cc1:`` + 64 hex: spec A5's ``HMAC(machine key, sessionId:uuid)``."""
    return RECORD_KEY_PREFIX + keys.hmac_key_for(machine_key, session_id, line_uuid)


def expired_gap_key(machine_key: bytes, session_id: str, line_uuid: str) -> str:
    return GAP_KEY_PREFIX + keys.hmac_key_for(
        machine_key, session_id, "expired", line_uuid
    )


def source_gap_key(machine_key: bytes, session_hash: str, cursor_offset: int) -> str:
    """The ``source_truncated`` gap key.

    Built from ``session_hash``, the session row's key, because the worker never
    holds the raw session id for a missing file: the hook does not spool it and
    there is no line to read it from.
    """
    return GAP_KEY_PREFIX + keys.hmac_key_for(
        machine_key, session_hash, "gap", str(cursor_offset)
    )


# --- the drain --------------------------------------------------------------


def drain(
    bridge_home: Path,
    *,
    projects_root: Path,
    client: IngestClient,
    now: Callable[[], datetime],
) -> int:
    """Run one pass and return an exit code.

    A second worker that finds ``worker.lock`` held returns :data:`EXIT_OK` at
    once without touching state.
    """
    try:
        paths.check_private(bridge_home)
    except OSError:
        # InsecurePermissionsError, or a bridge_home that is not there.
        return EXIT_FAILURE
    with _worker_lock(bridge_home) as held:
        if not held:
            return EXIT_OK
        try:
            config = bridge_config.load(bridge_home)
        except bridge_config.ConfigError:
            return EXIT_FAILURE
        if config is None:
            return EXIT_FAILURE
        # Only ``init`` mints the machine key; a worker that minted one would
        # key every record differently from what the server already holds.
        if not os.path.lexists(paths.machine_key_path(bridge_home)):
            return EXIT_FAILURE
        try:
            machine_key = keys.load_or_create_machine_key(bridge_home)
        except (OSError, keys.MachineKeyError):
            return EXIT_FAILURE
        moment = _aware(now())
        connection = state.connect(paths.state_path(bridge_home))
        try:
            fold_spool(connection, bridge_home, now=moment)
            collect(
                connection,
                config=config,
                machine_key=machine_key,
                projects_root=projects_root,
                now=moment,
            )
            # Prompt 13 posts the batches through ``client`` and settles them.
        finally:
            state.close(connection)
    return EXIT_OK


@contextmanager
def _worker_lock(bridge_home: Path) -> Iterator[bool]:
    """Hold ``flock(LOCK_EX | LOCK_NB)`` on ``worker.lock``; yield whether held."""
    fd = os.open(
        paths.worker_lock_path(bridge_home),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        paths.FILE_MODE,
    )
    try:
        os.fchmod(fd, paths.FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        # Closing the descriptor releases the lock.
        os.close(fd)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("the worker's clock must be timezone-aware")
    return moment


# --- step 1: fold the spool ---------------------------------------------------


@dataclass
class _Sighting:
    transcript_path: str
    seen_at: int
    ended_at: int | None


def fold_spool(
    connection: sqlite3.Connection, bridge_home: Path, *, now: datetime
) -> None:
    """Fold every spool file's new complete lines into ``session`` rows.

    [capture 2, 3] — may be revised at reconciliation. Any number of ``Stop``
    and ``SessionEnd`` lines for one session, in any order, become one row;
    that upsert is the coalescing. Each file's new offset commits in the same
    transaction as the rows its lines produced. A fully consumed file dated
    before today (UTC, the hook's naming) is deleted.
    """
    today = now.astimezone(UTC).date()
    at = int(now.timestamp())
    try:
        dir_fd = os.open(
            paths.spool_dir(bridge_home), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    except FileNotFoundError:
        return
    try:
        for name in sorted(os.listdir(dir_fd)):
            day = _spool_day(name)
            if day is not None:
                _fold_file(connection, dir_fd, name, past=day < today, at=at)
    finally:
        os.close(dir_fd)


def _spool_day(name: str) -> date | None:
    match = _SPOOL_NAME.fullmatch(name)
    if match is None:
        return None
    try:
        return date.fromisoformat(match[1])
    except ValueError:
        return None


def _fold_file(
    connection: sqlite3.Connection, dir_fd: int, name: str, *, past: bool, at: int
) -> None:
    fd = os.open(name, _OPEN_FLAGS, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return
        offset = state.get_spool_offset(connection, name)
        if offset > info.st_size:
            # The file was recreated under a name whose offset outlived it.
            offset = 0
        data = _read_to_eof(fd, offset)
    finally:
        os.close(fd)
    # Whole lines only: a trailing partial line is the hook mid-write.
    complete = data[: data.rfind(b"\n") + 1]
    sightings: dict[str, _Sighting] = {}
    malformed = 0
    for raw in complete.splitlines():
        locator = _spool_locator(raw)
        if locator is None:
            malformed += 1
            continue
        event, session_hash, transcript_path, line_at = locator
        seen_at = line_at if line_at is not None else at
        sighting = sightings.get(session_hash)
        if sighting is None:
            sighting = _Sighting(transcript_path, seen_at, None)
            sightings[session_hash] = sighting
        else:
            sighting.transcript_path = transcript_path
            sighting.seen_at = max(sighting.seen_at, seen_at)
        if event == "SessionEnd":
            sighting.ended_at = max(sighting.ended_at or 0, seen_at)
    new_offset = offset + len(complete)
    with state.transaction(connection):
        for session_hash, sighting in sightings.items():
            existing = state.get_session(connection, session_hash)
            seen_at = sighting.seen_at
            if existing is not None and existing.last_seen_at is not None:
                # Never move last_seen_at backwards for a late-read old line.
                seen_at = max(seen_at, existing.last_seen_at)
            state.upsert_session(
                connection,
                session_hash,
                transcript_path=sighting.transcript_path,
                seen_at=seen_at,
                ended_at=sighting.ended_at,
            )
        if malformed:
            state.bump_ledger(connection, "hook_malformed", at=at, by=malformed)
        state.set_spool_offset(connection, name, new_offset)
    if past and len(complete) == len(data):
        _delete_consumed(connection, dir_fd, name, new_offset)


def _delete_consumed(
    connection: sqlite3.Connection, dir_fd: int, name: str, consumed: int
) -> None:
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if info.st_size != consumed:
        return  # a late line arrived since the read; the next pass takes it
    os.unlink(name, dir_fd=dir_fd)
    state.delete_spool_offset(connection, name)


def _spool_locator(raw: bytes) -> tuple[str, str, str, int | None] | None:
    """``(event, session_hash, transcript_path, at)``, or ``None`` if malformed.

    The ``malformed`` key is read first: a line carrying it is malformed
    whatever its ``event`` says. Otherwise only ``Stop`` or ``SessionEnd`` with
    string ``session_hash`` and ``transcript_path`` is a locator; any other
    label is malformed, never a third kind of sighting.
    """
    try:
        line = json.loads(raw)
    except (RecursionError, ValueError):
        return None
    if not isinstance(line, dict) or "malformed" in line:
        return None
    event = line.get("event")
    session_hash = line.get("session_hash")
    transcript_path = line.get("transcript_path")
    if event not in SPOOL_EVENTS:
        return None
    if not isinstance(session_hash, str) or not session_hash:
        return None
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    line_at = line.get("at")
    if isinstance(line_at, bool) or not isinstance(line_at, int):
        line_at = None
    return str(event), session_hash, transcript_path, line_at


def _read_to_eof(fd: int, offset: int) -> bytes:
    chunks: list[bytes] = []
    position = offset
    while chunk := os.pread(fd, _READ_CHUNK, position):
        chunks.append(chunk)
        position += len(chunk)
    return b"".join(chunks)


# --- steps 2-5: validate, read, select, sanitize ------------------------------


def collect(
    connection: sqlite3.Connection,
    *,
    config: bridge_config.Config,
    machine_key: bytes,
    projects_root: Path,
    now: datetime,
) -> list[SessionBatch]:
    """Validate every session's source and gather what it has to send.

    Every session is re-validated on every pass; no verdict is cached. A
    refused session is dropped here, with its ledger count, and makes no
    request of any kind. The read limits are shared by all sessions in the
    pass; a session past them is validated but not read this time.
    """
    _aware(now)
    budget = _Budget(bytes_left=config.max_bytes, items_left=config.max_records)
    batches: list[SessionBatch] = []
    for row in state.list_sessions(connection):
        batch = _session_pass(
            connection,
            row,
            config=config,
            machine_key=machine_key,
            projects_root=projects_root,
            now=now,
            budget=budget,
        )
        if batch is not None:
            batches.append(batch)
    return batches


def _session_pass(
    connection: sqlite3.Connection,
    row: state.SessionRow,
    *,
    config: bridge_config.Config,
    machine_key: bytes,
    projects_root: Path,
    now: datetime,
    budget: _Budget,
) -> SessionBatch | None:
    if row.transcript_path is None:
        verdict: transcript.ValidationResult = Refused("path_escape")
    else:
        verdict = transcript.validate_source(
            row.transcript_path, config.enrolled_dir, projects_root=projects_root
        )
    match verdict:
        case Refused(reason=reason):
            with state.transaction(connection):
                state.bump_ledger(connection, reason, at=int(now.timestamp()))
                state.delete_session(connection, row.session_hash)
            return None
        case Missing():
            # Never opened in any mode: the gap is all this pass does.
            return _source_gone(connection, row, machine_key=machine_key, now=now)
        case Ok(path=path):
            return _read_session(
                connection,
                row,
                path,
                config=config,
                machine_key=machine_key,
                now=now,
                budget=budget,
            )
        case _:
            assert_never(verdict)


def _source_gone(
    connection: sqlite3.Connection,
    row: state.SessionRow,
    *,
    machine_key: bytes,
    now: datetime,
) -> SessionBatch:
    """The source is missing, unopenable or not a regular file: owe one gap.

    Its key is stored in ``pending_gap_key``; the row is kept until the server
    acknowledges that gap, and the source is re-validated next pass.
    """
    gap = IngestGap(
        native_key=source_gap_key(machine_key, row.session_hash, row.cursor_offset),
        reason="source_truncated",
        recorded_at=now,
    )
    state.set_pending_gap_key(connection, row.session_hash, gap.native_key)
    return SessionBatch(
        session_hash=row.session_hash,
        source_gap=gap,
        items=(),
        start_offset=row.cursor_offset,
        end_offset=row.cursor_offset,
        inode=row.cursor_inode,
        clock_unreadable=0,
    )


def _read_session(
    connection: sqlite3.Connection,
    row: state.SessionRow,
    path: Path,
    *,
    config: bridge_config.Config,
    machine_key: bytes,
    now: datetime,
    budget: _Budget,
) -> SessionBatch | None:
    if budget.bytes_left <= 0 or budget.items_left <= 0:
        return None
    # O_NOFOLLOW and S_ISREG on the descriptor close the window between
    # validate_source and this open: a symlink swapped in since (ELOOP) or a
    # non-regular file is treated as a missing source, never followed.
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError:
        return _source_gone(connection, row, machine_key=machine_key, now=now)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return _source_gone(connection, row, machine_key=machine_key, now=now)
        source_gap: IngestGap | None = None
        start = row.cursor_offset
        if (
            row.cursor_inode is not None and info.st_ino != row.cursor_inode
        ) or info.st_size < row.cursor_offset:
            # Replaced or shrank: one gap, then replay from 0. Replay is safe
            # because every key is content-derived and the server holds each
            # key once.
            source_gap = IngestGap(
                native_key=source_gap_key(
                    machine_key, row.session_hash, row.cursor_offset
                ),
                reason="source_truncated",
                recorded_at=now,
            )
            start = 0
        # [capture 4] — may be revised at reconciliation: a byte-offset cursor
        # over a file that only grows within a session.
        window = _read_complete_lines(fd, start, budget.bytes_left)
    finally:
        os.close(fd)
    items, consumed, clock_unreadable = _select(
        window,
        start,
        machine_key=machine_key,
        now=now,
        max_age=timedelta(hours=config.max_pending_hours),
        items_left=budget.items_left,
    )
    budget.bytes_left -= consumed
    budget.items_left -= len(items)
    return SessionBatch(
        session_hash=row.session_hash,
        source_gap=source_gap,
        items=tuple(items),
        start_offset=start,
        end_offset=start + consumed,
        inode=info.st_ino,
        clock_unreadable=clock_unreadable,
    )


def _read_complete_lines(fd: int, start: int, limit: int) -> bytes:
    """Complete ``\\n``-terminated lines from ``start``, up to ``limit`` bytes.

    A single line longer than ``limit`` is still read whole, alone, so an
    oversize line is consumed (and dropped by the selection or the sanitizer)
    rather than stalling the cursor for ever.
    """
    data = os.pread(fd, limit, start)
    end = data.rfind(b"\n") + 1
    if end or len(data) < limit:
        return data[:end]
    buffer = bytearray(data)
    position = start + len(data)
    while chunk := os.pread(fd, _READ_CHUNK, position):
        newline = chunk.find(b"\n")
        if newline >= 0:
            buffer += chunk[: newline + 1]
            return bytes(buffer)
        buffer += chunk
        position += len(chunk)
    return b""  # the long line is not finished yet


def _lines(window: bytes) -> Iterator[bytes]:
    position = 0
    while position < len(window):
        end = window.index(b"\n", position) + 1
        yield window[position:end]
        position = end


def _select(
    window: bytes,
    start: int,
    *,
    machine_key: bytes,
    now: datetime,
    max_age: timedelta,
    items_left: int,
) -> tuple[list[LineCandidate], int, int]:
    """Steps 4 and 5 over one read window.

    Returns the candidates, the bytes consumed (every line up to the last one
    looked at, whether or not it made a candidate) and the unreadable-clock
    count. Stops once ``items_left`` candidates are made.
    """
    items: list[LineCandidate] = []
    clock_unreadable = 0
    consumed = 0
    cutoff = now - max_age
    for raw in _lines(window):
        if len(items) >= items_left:
            break
        consumed += len(raw)
        end_offset = start + consumed
        line = _parse_line(raw)
        if line is None:
            continue
        text = transcript.human_text(line)
        if text is None:
            continue
        clock = transcript.parse_clock(line)
        if clock is None:
            clock_unreadable += 1
            continue
        ids = transcript.extract_ids(line)
        if ids is None:
            continue
        session_id, line_uuid = ids
        if clock < cutoff:
            # Expired text never leaves the machine: a gap with no text.
            items.append(
                LineCandidate(
                    item=IngestGap(
                        native_key=expired_gap_key(machine_key, session_id, line_uuid),
                        reason="expired_pending",
                        recorded_at=clock,
                    ),
                    end_offset=end_offset,
                    entrypoint=None,
                )
            )
            continue
        # sanitize() strips NUL and lone surrogates first (strip_unstorable),
        # so no posted text carries either; None drops the line locally.
        clean = sanitize(text)
        if clean is None:
            continue
        items.append(
            LineCandidate(
                item=IngestRecord(
                    native_key=record_key(machine_key, session_id, line_uuid),
                    recorded_at=clock,
                    text=clean,
                ),
                end_offset=end_offset,
                entrypoint=transcript.entrypoint(line),
            )
        )
    return items, consumed, clock_unreadable


def _parse_line(raw: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(raw)
    except (RecursionError, ValueError):
        return None
    return value if isinstance(value, dict) else None
