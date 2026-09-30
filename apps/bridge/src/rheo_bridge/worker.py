"""The bridge worker's drain (spec § Architecture, System Components item 9).

One pass, under ``worker.lock``:

1. :func:`fold_spool` folds the hook's spool lines into ``session`` rows;
2. :func:`collect` validates each session's source and, for an admitted one,
3. reads complete lines from its committed cursor,
4. selects the human lines, and
5. sanitizes them locally, turning each into a record or an ``expired_pending``
   gap keyed by an HMAC under ``machine.key``;
6. posts one batch, or holds while the server defers or cannot be reached, and
7. closes a session once its whole range is acknowledged.

What :func:`collect` returns is a list of :class:`SessionBatch`: candidates to
post, in line order, with each line's byte range. :func:`collect` posts nothing
and moves no transcript cursor: a cursor moves only in the same SQLite commit as
the server's acknowledgement of the range it covers, so a crash before that
commit re-reads a range the server already holds and answers identically.
Besides dropping refused sessions and counting a source it cannot open, the one
write :func:`collect` makes is ``pending_gap_key``: set when a source vanishes
(the durable note that it owes the server a gap), cleared when the source is
back.

The drain repeats steps 2-7 while a pass moves something, so a backlog larger
than one pass's limits drains in one run. Nothing keys on ``SessionEnd``'s
``reason``, whose values differ between clients.

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
from typing import Final, Literal, assert_never

from rheo_core.evidence.sanitize import MAX_INPUT_CHARS, sanitize

from rheo_bridge import config as bridge_config
from rheo_bridge import keys, paths, state, transcript
from rheo_bridge.client import (
    IngestClient,
    IngestGap,
    IngestRecord,
)
from rheo_bridge.settle import (
    _GONE_ERRNOS,
    _OPEN_FLAGS,
    _Outcome,
    _PassContext,
    _post_and_settle,
    token_expiring,
)
from rheo_bridge.transcript import Missing, Ok, Refused

EXIT_OK: Final = 0
# The worker cannot run safely: a loose ``bridge_home``, no configuration, or
# no usable machine key. It touches no state.
EXIT_FAILURE: Final = 1
# The server refused the batch for a reason only a person can fix: a rejected
# or under-permitted token (rotate), an inactive or mismatched enrollment
# (re-enroll), or ``input_invalid`` (a bridge bug). Nothing moved; the worker
# holds so later runs send one probe per back-off window, not the backlog.
EXIT_REFUSED: Final = 2

# How long a closed session's row is kept as a tombstone. A session sighted
# again within it resumes from its final cursor; after it, a new sighting
# starts a fresh row at 0.
TOMBSTONE_RETENTION_SECONDS: Final = 30 * 24 * 60 * 60

RECORD_KEY_PREFIX: Final = "cc1:"
GAP_KEY_PREFIX: Final = "cc1g:"

SPOOL_EVENTS: Final = frozenset({"Stop", "SessionEnd"})
_SPOOL_NAME: Final = re.compile(r"(\d{4}-\d{2}-\d{2})\.jsonl")
_READ_CHUNK: Final = 65_536
# Mirrors ``rheo_core.evidence.ingest.INGEST_MAX_TEXT_CHARS`` (a test pins the
# two equal). Not imported: that module pulls the database stack, which the
# laptop's cold import must not load (spec § A6).
INGEST_MAX_TEXT_CHARS: Final = 4 * MAX_INPUT_CHARS
# A character costs at most six bytes in a JSON line (``\uXXXX``), so a line
# longer than this carries more text than the server accepts. The worker never
# buffers past it: such a line is scanned over and counted ``line_oversize``.
LINE_CEILING_BYTES: Final = 6 * INGEST_MAX_TEXT_CHARS


@dataclass(frozen=True)
class LineCandidate:
    """One line's record or ``expired_pending`` gap, and the line's byte range."""

    item: IngestRecord | IngestGap
    # The line is ``[start_offset, end_offset)``; ``end_offset`` is just past
    # its ``\n``.
    start_offset: int
    end_offset: int
    # The client that wrote the line, for the per-client ``accepted`` counters.
    # Kept locally; never sent. [capture 8] — may be revised at reconciliation.
    entrypoint: str | None


SkipReason = Literal[
    "clock_unreadable", "line_oversize", "missing_ids", "sanitized_empty"
]


@dataclass(frozen=True)
class LineSkip:
    """A line that made no candidate but owes a ledger count once passed.

    ``line_oversize`` is a line past :data:`LINE_CEILING_BYTES`, scanned over
    without being buffered; the server would refuse its text anyway.
    """

    reason: SkipReason
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class SessionBatch:
    """What one pass produced for one session, not yet posted."""

    session_hash: str
    # A ``source_truncated`` gap: the source vanished, became unreadable, or
    # was replaced or shrank since the stored cursor.
    source_gap: IngestGap | None
    items: tuple[LineCandidate, ...]
    # Lines in the range that owe a ledger count, in line order. Commit a
    # skip's count with the cursor that passes its ``end_offset``.
    skips: tuple[LineSkip, ...]
    # The read covered ``[start_offset, end_offset)`` of the file with inode
    # ``inode``. After a replacement ``start_offset`` is 0; when nothing was
    # read both equal the stored cursor. Every line in the range advances the
    # cursor, whether or not it made a candidate or a skip.
    start_offset: int
    end_offset: int
    inode: int | None
    # ``source_gap`` is owed because the source is gone (the row's
    # ``pending_gap_key``), not because it was replaced; nothing was read.
    source_gone: bool = False

    @property
    def clock_unreadable(self) -> int:
        return sum(1 for skip in self.skips if skip.reason == "clock_unreadable")


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
            return _run(
                connection,
                bridge_home,
                config=config,
                machine_key=machine_key,
                projects_root=projects_root,
                client=client,
                now=moment,
            )
        finally:
            state.close(connection)


def _run(
    connection: sqlite3.Connection,
    bridge_home: Path,
    *,
    config: bridge_config.Config,
    machine_key: bytes,
    projects_root: Path,
    client: IngestClient,
    now: datetime,
) -> int:
    at = int(now.timestamp())
    fold_spool(connection, bridge_home, now=now)
    state.prune_closed(connection, closed_before=at - TOMBSTONE_RETENTION_SECONDS)
    if token_expiring(config, now):
        state.bump_ledger(connection, "token_expiring", at=at)
    hold = state.get_hold(connection)
    if hold.hold_until is not None and at < hold.hold_until:
        return EXIT_OK
    context = _PassContext(
        connection=connection,
        config=config,
        machine_key=machine_key,
        projects_root=projects_root,
        client=client,
        now=now,
        hold=hold,
    )
    pass_number = 0
    while True:
        batches = collect(
            connection,
            config=config,
            machine_key=machine_key,
            projects_root=projects_root,
            now=now,
            rotate=pass_number,
        )
        outcome = _post_and_settle(context, batches, probe=hold.hold_step > 0)
        if outcome is _Outcome.REFUSED:
            return EXIT_REFUSED
        # A probe is one request per run; the next run posts in full.
        if outcome is not _Outcome.PROGRESS or hold.hold_step > 0:
            return EXIT_OK
        pass_number += 1


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

    A spool directory or file that cannot be read (say, a symlink planted
    after ``check_private``, which ``O_NOFOLLOW`` refuses) is skipped with a
    ``spool_unreadable`` count; its offset stays, so nothing is lost.
    """
    today = now.astimezone(UTC).date()
    at = int(now.timestamp())
    try:
        dir_fd = os.open(
            paths.spool_dir(bridge_home), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    except FileNotFoundError:
        return
    except OSError:
        state.bump_ledger(connection, "spool_unreadable", at=at)
        return
    try:
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError:
            state.bump_ledger(connection, "spool_unreadable", at=at)
            return
        for name in names:
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
    try:
        read = _read_spool_file(connection, dir_fd, name)
    except OSError:
        state.bump_ledger(connection, "spool_unreadable", at=at)
        return
    if read is None:
        return
    offset, data = read
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
            if existing is not None and existing.closed_at is not None:
                _reopen(connection, existing, sighting)
                continue
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


def _reopen(
    connection: sqlite3.Connection, tombstone: state.SessionRow, sighting: _Sighting
) -> None:
    """A closed session sighted again resumes from its tombstone cursor.

    Resuming, not replaying from 0, matters: turns the server already accepted
    may by now be older than ``max_pending_hours``, and a replay would send
    them again as ``expired_pending`` gaps under different keys, a false loss.
    A sighting from before the close is stale and changes nothing. If the file
    was replaced meanwhile, the next read sees a new inode and follows the
    ordinary replacement rule.
    """
    closed_at = tombstone.closed_at
    assert closed_at is not None
    if sighting.seen_at < closed_at:
        return
    ended_at = sighting.ended_at
    if ended_at is not None and ended_at < closed_at:
        ended_at = None
    state.reopen_session(
        connection,
        tombstone.session_hash,
        transcript_path=sighting.transcript_path,
        seen_at=sighting.seen_at,
        ended_at=ended_at,
    )


def _read_spool_file(
    connection: sqlite3.Connection, dir_fd: int, name: str
) -> tuple[int, bytes] | None:
    """``(offset, bytes from offset to EOF)``, or ``None`` for a non-file."""
    fd = os.open(name, _OPEN_FLAGS, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        offset = state.get_spool_offset(connection, name)
        if offset > info.st_size:
            # The file was recreated under a name whose offset outlived it.
            offset = 0
        return offset, _read_to_eof(fd, offset)
    finally:
        os.close(fd)


def _delete_consumed(
    connection: sqlite3.Connection, dir_fd: int, name: str, consumed: int
) -> None:
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if info.st_size != consumed:
            return  # a late line arrived since the read; the next pass takes it
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        return  # left for a later pass; its offset still says it is consumed
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


@dataclass
class _Budget:
    """The local ``max_bytes``/``max_records`` limits, shared across one pass.

    ``items_left`` counts every gap and record, ``source_truncated`` gaps
    included, so one pass never gathers more than ``max_records`` of them.
    """

    max_bytes: int
    bytes_left: int
    items_left: int

    @property
    def untouched(self) -> bool:
        """Nothing has been read yet this pass."""
        return self.bytes_left == self.max_bytes


def collect(
    connection: sqlite3.Connection,
    *,
    config: bridge_config.Config,
    machine_key: bytes,
    projects_root: Path,
    now: datetime,
    rotate: int = 0,
) -> list[SessionBatch]:
    """Validate every session's source and gather what it has to send.

    Every session is re-validated on every pass; no verdict is cached. A
    refused session is dropped here, with its ledger count, and makes no
    request of any kind. The read limits are shared by all sessions in the
    pass; a session past them is validated but not read, and makes no gap,
    this time.

    ``rotate`` starts the pass that many sessions into the list. The drain
    passes its pass number, so a session parked behind a line larger than the
    byte budget left over is, within as many passes as there are sessions,
    read first, when the whole-line path is open to it.
    """
    _aware(now)
    budget = _Budget(
        max_bytes=config.max_bytes,
        bytes_left=config.max_bytes,
        items_left=config.max_records,
    )
    rows = state.list_sessions(connection)
    if rows:
        shift = rotate % len(rows)
        rows = rows[shift:] + rows[:shift]
    batches: list[SessionBatch] = []
    for row in rows:
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
            return _source_gone(
                connection, row, machine_key=machine_key, now=now, budget=budget
            )
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


def _source_gap(machine_key: bytes, row: state.SessionRow, now: datetime) -> IngestGap:
    return IngestGap(
        native_key=source_gap_key(machine_key, row.session_hash, row.cursor_offset),
        reason="source_truncated",
        recorded_at=now,
    )


def _source_gone(
    connection: sqlite3.Connection,
    row: state.SessionRow,
    *,
    machine_key: bytes,
    now: datetime,
    budget: _Budget,
) -> SessionBatch | None:
    """The source is missing, swapped or not a regular file: owe one gap.

    Its key is stored in ``pending_gap_key``; the row is kept until the server
    acknowledges that gap, and the source is re-validated next pass. With no
    record budget left this pass, nothing is written and the next pass retries.
    """
    if budget.items_left <= 0:
        return None
    budget.items_left -= 1
    gap = _source_gap(machine_key, row, now)
    state.set_pending_gap_key(connection, row.session_hash, gap.native_key)
    return SessionBatch(
        session_hash=row.session_hash,
        source_gap=gap,
        items=(),
        skips=(),
        start_offset=row.cursor_offset,
        end_offset=row.cursor_offset,
        inode=row.cursor_inode,
        source_gone=True,
    )


def _source_unreadable(connection: sqlite3.Connection, now: datetime) -> None:
    """A local failure to open or read an admitted source: count it, change
    nothing else, and try again next pass. No gap: the source is not gone."""
    state.bump_ledger(connection, "source_unreadable", at=int(now.timestamp()))


@dataclass(frozen=True)
class _Window:
    """What one read produced: complete buffered lines, or one oversize line.

    When ``oversize_end`` is set, ``data`` is empty and the single line
    ``[start, oversize_end)`` was scanned over without being buffered.
    """

    data: bytes
    oversize_end: int | None = None


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
    except OSError as exc:
        if exc.errno in _GONE_ERRNOS:
            return _source_gone(
                connection, row, machine_key=machine_key, now=now, budget=budget
            )
        _source_unreadable(connection, now)
        return None
    try:
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return _source_gone(
                    connection, row, machine_key=machine_key, now=now, budget=budget
                )
            replaced = (
                row.cursor_inode is not None and info.st_ino != row.cursor_inode
            ) or info.st_size < row.cursor_offset
            # Replaced or shrank: one gap, then replay from 0. Replay is safe
            # because every key is content-derived and the server holds each
            # key once. The gap takes one item of the record budget.
            start = 0 if replaced else row.cursor_offset
            items_left = budget.items_left - (1 if replaced else 0)
            # [capture 4] — may be revised at reconciliation: a byte-offset
            # cursor over a file that only grows within a session.
            window = (
                _read_window(
                    fd,
                    start,
                    budget.bytes_left,
                    allow_oversize=budget.untouched,
                )
                if items_left > 0
                else _Window(b"")
            )
        except OSError:
            _source_unreadable(connection, now)
            return None
    finally:
        os.close(fd)
    source_gap = _source_gap(machine_key, row, now) if replaced else None
    if row.pending_gap_key is not None:
        # The source is back: the gap an earlier Missing pass noted is no
        # longer owed, so the row must not later be closed as gone.
        state.set_pending_gap_key(connection, row.session_hash, None)
    items, skips, consumed = _select(
        window,
        start,
        machine_key=machine_key,
        now=now,
        max_age=timedelta(hours=config.max_pending_hours),
        items_left=items_left,
    )
    # An oversize line was scanned, not buffered, so it costs no byte budget.
    budget.bytes_left -= len(window.data) if window.oversize_end is None else 0
    budget.items_left = items_left - len(items)
    return SessionBatch(
        session_hash=row.session_hash,
        source_gap=source_gap,
        items=tuple(items),
        skips=tuple(skips),
        start_offset=start,
        end_offset=start + consumed,
        inode=info.st_ino,
    )


def _read_window(fd: int, start: int, limit: int, *, allow_oversize: bool) -> _Window:
    """Complete ``\\n``-terminated lines from ``start``, up to ``limit`` bytes.

    A first line longer than ``limit`` is taken whole, alone, but only when
    ``allow_oversize`` (nothing else read this pass), so ``max_bytes`` holds
    across sessions and the cursor still never stalls on a long line. Past
    :data:`LINE_CEILING_BYTES` the line is no longer buffered: the rest is
    scanned for its ``\\n`` and the line comes back as ``oversize_end``.
    """
    data = os.pread(fd, limit, start)
    end = data.rfind(b"\n") + 1
    if end or len(data) < limit or not allow_oversize:
        return _Window(data[:end])
    buffer = bytearray(data)
    position = start + len(data)
    while len(buffer) <= LINE_CEILING_BYTES:
        chunk = os.pread(fd, _READ_CHUNK, position)
        if not chunk:
            return _Window(b"")  # the long line is not finished yet
        newline = chunk.find(b"\n")
        if newline >= 0:
            buffer += chunk[: newline + 1]
            if len(buffer) <= LINE_CEILING_BYTES:
                return _Window(bytes(buffer))
            return _Window(b"", oversize_end=start + len(buffer))
        buffer += chunk
        position += len(chunk)
    del buffer
    while chunk := os.pread(fd, _READ_CHUNK, position):
        newline = chunk.find(b"\n")
        if newline >= 0:
            return _Window(b"", oversize_end=position + newline + 1)
        position += len(chunk)
    return _Window(b"")


def _lines(window: bytes) -> Iterator[bytes]:
    position = 0
    while position < len(window):
        end = window.index(b"\n", position) + 1
        yield window[position:end]
        position = end


def _select(
    window: _Window,
    start: int,
    *,
    machine_key: bytes,
    now: datetime,
    max_age: timedelta,
    items_left: int,
) -> tuple[list[LineCandidate], list[LineSkip], int]:
    """Steps 4 and 5 over one read window.

    Returns the candidates, the skips owing a ledger count, and the bytes
    consumed (every line up to the last one looked at, whether or not it made
    a candidate). Stops once ``items_left`` candidates are made.
    """
    if window.oversize_end is not None:
        skip = LineSkip("line_oversize", start, window.oversize_end)
        return [], [skip], window.oversize_end - start
    items: list[LineCandidate] = []
    skips: list[LineSkip] = []
    consumed = 0
    cutoff = now - max_age
    for raw in _lines(window.data):
        if len(items) >= items_left:
            break
        line_start = start + consumed
        consumed += len(raw)
        line_end = start + consumed
        line = _parse_line(raw)
        if line is None:
            continue
        text = transcript.human_text(line)
        if text is None:
            continue
        clock = transcript.parse_clock(line)
        if clock is None:
            skips.append(LineSkip("clock_unreadable", line_start, line_end))
            continue
        ids = transcript.extract_ids(line)
        if ids is None:
            skips.append(LineSkip("missing_ids", line_start, line_end))
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
                    start_offset=line_start,
                    end_offset=line_end,
                    entrypoint=None,
                )
            )
            continue
        # sanitize() strips NUL and lone surrogates first (strip_unstorable),
        # so no posted text carries either; None drops the line locally.
        clean = sanitize(text)
        if clean is None:
            skips.append(LineSkip("sanitized_empty", line_start, line_end))
            continue
        items.append(
            LineCandidate(
                item=IngestRecord(
                    native_key=record_key(machine_key, session_id, line_uuid),
                    recorded_at=clock,
                    text=clean,
                ),
                start_offset=line_start,
                end_offset=line_end,
                entrypoint=transcript.entrypoint(line),
            )
        )
    return items, skips, consumed


def _parse_line(raw: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(raw)
    except (RecursionError, ValueError):
        return None
    return value if isinstance(value, dict) else None
