"""The bridge's local state, ``bridge_home/state.sqlite`` (spec § Architecture,
"Laptop state (not a database of record)").

A thin wrapper over stdlib ``sqlite3``: one local file with one writer (the
worker, under ``worker.lock``), so no ORM and no migration chain. The four
tables are created if absent when a connection opens. Losing the file loses
cursors and counters, not evidence: the server holds every record by its native
key, so a rebuilt cursor re-sends what the server already has.

Every function takes the connection explicitly; there is no module-level
connection. Each writer runs inside :func:`transaction`: alone it commits its
own step, and inside a caller's ``with transaction(conn):`` it joins, so
several writes (a cursor, a ledger count and the hold, say) commit together.
Times are integer Unix seconds supplied by the caller; nothing here reads a
clock.

The file keeps SQLite's default rollback journal, not WAL. The worker is the
one writer; a status reader is blocked only for the moment a ``COMMIT`` holds
the exclusive lock, which the default 5 s busy timeout rides out, while WAL
would add two sidecar files that must also stay 0600 under ``bridge_home``.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from rheo_bridge.paths import FILE_MODE

SCHEMA: Final = (
    """
    CREATE TABLE IF NOT EXISTS spool_offset (
        file TEXT PRIMARY KEY,
        offset INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session (
        session_hash TEXT PRIMARY KEY,
        transcript_path TEXT,
        first_seen_at INTEGER,
        last_seen_at INTEGER,
        ended_at INTEGER,
        cursor_offset INTEGER NOT NULL DEFAULT 0,
        cursor_inode INTEGER,
        pending_gap_key TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hold (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        hold_until INTEGER,
        hold_step INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger (
        reason TEXT PRIMARY KEY,
        count INTEGER NOT NULL DEFAULT 0,
        last_at INTEGER
    )
    """,
)

# The ledger is content-free: its ``reason`` column holds only these fixed
# counter names, plus one ``accepted:<entrypoint>`` row per client kind.
LEDGER_REASONS: Final = frozenset(
    {
        "path_escape",
        "project_unmatched",
        "hook_malformed",
        "clock_unreadable",
        "token_expiring",
        # The worker's local read failures: a transcript it could not open for
        # a reason other than its absence, a spool file it could not read,
        # and a transcript line too large for the server to accept.
        "source_unreadable",
        "spool_unreadable",
        "line_oversize",
        # Refusals of a whole batch by the ingest operation. Each needs a
        # person (rotate the token, re-enroll, or report a bridge bug); the
        # worker holds instead of resending.
        "token_rejected",
        "token_not_permitted",
        "enrollment_inactive",
        "enrollment_mismatch",
        "input_invalid",
    }
)
ACCEPTED_PREFIX: Final = "accepted:"
# A client kind is a short token such as ``cli`` or ``claude-desktop``. The
# pattern keeps a path, a hash or free text out of the reason column even if a
# transcript line carries an odd ``entrypoint`` value.
_ENTRYPOINT: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")


@dataclass(frozen=True)
class SessionRow:
    session_hash: str
    transcript_path: str | None
    first_seen_at: int | None
    last_seen_at: int | None
    ended_at: int | None
    cursor_offset: int
    cursor_inode: int | None
    pending_gap_key: str | None


@dataclass(frozen=True)
class Hold:
    hold_until: int | None
    hold_step: int


NO_HOLD: Final = Hold(hold_until=None, hold_step=0)


@dataclass(frozen=True)
class LedgerEntry:
    count: int
    last_at: int | None


# --- connection -------------------------------------------------------------


def connect(path: Path) -> sqlite3.Connection:
    """Open ``state.sqlite`` (created at 0600 if absent) and ensure the schema.

    The file is created with ``O_NOFOLLOW`` before SQLite opens it, so a
    symlink planted at the name is refused and a new file never exists at the
    umask's mode. SQLite gives its journal the database file's mode.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
    finally:
        os.close(fd)
    # ``isolation_level=None``: no implicit transactions; :func:`transaction`
    # opens every one explicitly.
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        migrate(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    """Create any of the four tables that is absent. Idempotent."""
    with transaction(connection):
        for statement in SCHEMA:
            connection.execute(statement)


def close(connection: sqlite3.Connection) -> None:
    connection.close()


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Group writes so they commit together or not at all.

    Outermost use opens ``BEGIN IMMEDIATE`` and commits on a clean exit. When
    the connection is already in a transaction, the block joins it: it neither
    begins nor commits, and an exception passes out to the outermost block,
    which rolls everything back. There is no savepoint, so a caller that catches
    an exception inside a joined block and carries on commits whatever that
    block had already written.

    A failed ``COMMIT`` (say, a deferred constraint) is rolled back before the
    error is raised, so the connection is never left inside a transaction.
    Every writer in this module runs inside one of these, so any mix of them
    can be grouped under a caller's own ``with transaction(conn):``.
    """
    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        _rollback(connection)
        raise
    try:
        connection.execute("COMMIT")
    except BaseException:
        _rollback(connection)
        raise


def _rollback(connection: sqlite3.Connection) -> None:
    # SQLite has already rolled back after some errors (a full disk, an I/O
    # error); a second ROLLBACK would then raise and hide the first error.
    if connection.in_transaction:
        connection.execute("ROLLBACK")


# --- spool_offset -----------------------------------------------------------


def get_spool_offset(connection: sqlite3.Connection, file: str) -> int:
    """How far into a spool file the worker has read; 0 for an unseen file."""
    row = connection.execute(
        "SELECT offset FROM spool_offset WHERE file = ?", (file,)
    ).fetchone()
    return 0 if row is None else int(row[0])


def set_spool_offset(connection: sqlite3.Connection, file: str, offset: int) -> None:
    with transaction(connection):
        connection.execute(
            "INSERT INTO spool_offset (file, offset) VALUES (?, ?) "
            "ON CONFLICT (file) DO UPDATE SET offset = excluded.offset",
            (file, offset),
        )


def delete_spool_offset(connection: sqlite3.Connection, file: str) -> None:
    """Forget a spool file's offset once the file itself is deleted."""
    with transaction(connection):
        connection.execute("DELETE FROM spool_offset WHERE file = ?", (file,))


# --- session ----------------------------------------------------------------


def upsert_session(
    connection: sqlite3.Connection,
    session_hash: str,
    *,
    transcript_path: str,
    seen_at: int,
    ended_at: int | None = None,
) -> None:
    """Record a sighting of a session.

    A new session starts with ``first_seen_at = last_seen_at = seen_at`` and a
    zero cursor. A known one moves ``last_seen_at`` and its transcript path; its
    cursor and pending gap key are untouched, and an ``ended_at`` already set is
    kept when this sighting carries none.
    """
    with transaction(connection):
        connection.execute(
            "INSERT INTO session "
            "(session_hash, transcript_path, first_seen_at, last_seen_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (session_hash) DO UPDATE SET "
            "transcript_path = excluded.transcript_path, "
            "last_seen_at = excluded.last_seen_at, "
            "ended_at = COALESCE(excluded.ended_at, session.ended_at)",
            (session_hash, transcript_path, seen_at, seen_at, ended_at),
        )


def get_session(connection: sqlite3.Connection, session_hash: str) -> SessionRow | None:
    row = connection.execute(
        "SELECT session_hash, transcript_path, first_seen_at, last_seen_at, "
        "ended_at, cursor_offset, cursor_inode, pending_gap_key "
        "FROM session WHERE session_hash = ?",
        (session_hash,),
    ).fetchone()
    return None if row is None else SessionRow(*row)


def list_sessions(connection: sqlite3.Connection) -> list[SessionRow]:
    """Every known session, oldest first sighting first."""
    rows = connection.execute(
        "SELECT session_hash, transcript_path, first_seen_at, last_seen_at, "
        "ended_at, cursor_offset, cursor_inode, pending_gap_key "
        "FROM session ORDER BY first_seen_at, session_hash"
    ).fetchall()
    return [SessionRow(*row) for row in rows]


def advance_cursor(
    connection: sqlite3.Connection,
    session_hash: str,
    *,
    offset: int,
    inode: int | None,
) -> None:
    """Move a known session's transcript cursor. An unknown session is a no-op."""
    with transaction(connection):
        connection.execute(
            "UPDATE session SET cursor_offset = ?, cursor_inode = ? "
            "WHERE session_hash = ?",
            (offset, inode, session_hash),
        )


def set_pending_gap_key(
    connection: sqlite3.Connection, session_hash: str, gap_key: str | None
) -> None:
    """Set or clear (``None``) a known session's unsent gap key."""
    with transaction(connection):
        connection.execute(
            "UPDATE session SET pending_gap_key = ? WHERE session_hash = ?",
            (gap_key, session_hash),
        )


def delete_session(connection: sqlite3.Connection, session_hash: str) -> None:
    with transaction(connection):
        connection.execute(
            "DELETE FROM session WHERE session_hash = ?", (session_hash,)
        )


# --- hold -------------------------------------------------------------------


def get_hold(connection: sqlite3.Connection) -> Hold:
    """The recording-off back-off; :data:`NO_HOLD` when none is set."""
    row = connection.execute(
        "SELECT hold_until, hold_step FROM hold WHERE id = 1"
    ).fetchone()
    return NO_HOLD if row is None else Hold(hold_until=row[0], hold_step=int(row[1]))


def set_hold(
    connection: sqlite3.Connection, *, hold_until: int, hold_step: int
) -> None:
    with transaction(connection):
        connection.execute(
            "INSERT INTO hold (id, hold_until, hold_step) VALUES (1, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET "
            "hold_until = excluded.hold_until, hold_step = excluded.hold_step",
            (hold_until, hold_step),
        )


def clear_hold(connection: sqlite3.Connection) -> None:
    with transaction(connection):
        connection.execute("DELETE FROM hold WHERE id = 1")


# --- ledger -----------------------------------------------------------------


def accepted_reason(entrypoint: str) -> str:
    """The ledger reason counting acknowledged-accepted records for one client."""
    return ACCEPTED_PREFIX + entrypoint


def is_client_kind(entrypoint: str) -> bool:
    """Whether ``entrypoint`` may name an ``accepted:`` counter."""
    return _ENTRYPOINT.fullmatch(entrypoint) is not None


def _check_reason(reason: str) -> None:
    if reason in LEDGER_REASONS:
        return
    if reason.startswith(ACCEPTED_PREFIX) and _ENTRYPOINT.fullmatch(
        reason[len(ACCEPTED_PREFIX) :]
    ):
        return
    # The value is not echoed: a reason that fails the check is exactly the
    # kind of string the ledger must never hold, and this message may be logged.
    raise ValueError("not a ledger counter name")


def bump_ledger(
    connection: sqlite3.Connection, reason: str, *, at: int, by: int = 1
) -> None:
    """Add ``by`` to one counter and stamp it ``at``."""
    _check_reason(reason)
    if by < 1:
        raise ValueError("a ledger bump must be positive")
    with transaction(connection):
        connection.execute(
            "INSERT INTO ledger (reason, count, last_at) VALUES (?, ?, ?) "
            "ON CONFLICT (reason) DO UPDATE SET "
            "count = ledger.count + excluded.count, last_at = excluded.last_at",
            (reason, by, at),
        )


def get_ledger(connection: sqlite3.Connection) -> dict[str, LedgerEntry]:
    rows = connection.execute(
        "SELECT reason, count, last_at FROM ledger ORDER BY reason"
    ).fetchall()
    return {
        reason: LedgerEntry(count=int(count), last_at=last_at)
        for reason, count, last_at in rows
    }
