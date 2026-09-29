"""The bridge's local SQLite state: schema, accessors, and the content-free ledger.

Every test opens a fresh database under ``tmp_path``; the session hashes and
paths are invented.
"""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from rheo_bridge import paths, state

SESSION = "a" * 64
OTHER = "b" * 64
TRANSCRIPT = "/tmp/rheo-synthetic/projects/-tmp-example/0001.jsonl"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return paths.state_path(paths.ensure_bridge_home(tmp_path / ".rheo-bridge"))


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = state.connect(db_path)
    yield connection
    state.close(connection)


def test_connect_creates_the_four_tables_exactly_as_named(
    conn: sqlite3.Connection,
) -> None:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert tables == {"spool_offset", "session", "hold", "ledger"}
    columns = {
        table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        for table in tables
    }
    assert columns == {
        "spool_offset": ["file", "offset"],
        "session": [
            "session_hash",
            "transcript_path",
            "first_seen_at",
            "last_seen_at",
            "ended_at",
            "cursor_offset",
            "cursor_inode",
            "pending_gap_key",
        ],
        "hold": ["id", "hold_until", "hold_step"],
        "ledger": ["reason", "count", "last_at"],
    }


def test_the_file_is_private_and_reopening_keeps_the_data(db_path: Path) -> None:
    first = state.connect(db_path)
    state.set_spool_offset(first, "2026-01-01.jsonl", 42)
    state.close(first)
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    paths.check_private(db_path.parent)
    second = state.connect(db_path)
    try:
        assert state.get_spool_offset(second, "2026-01-01.jsonl") == 42
    finally:
        state.close(second)


def test_a_symlinked_state_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.sqlite"
    link = tmp_path / "state.sqlite"
    link.symlink_to(target)
    with pytest.raises(OSError):
        state.connect(link)
    assert not target.exists()


def test_spool_offsets_start_at_zero_and_move(conn: sqlite3.Connection) -> None:
    assert state.get_spool_offset(conn, "2026-01-01.jsonl") == 0
    state.set_spool_offset(conn, "2026-01-01.jsonl", 10)
    state.set_spool_offset(conn, "2026-01-01.jsonl", 25)
    assert state.get_spool_offset(conn, "2026-01-01.jsonl") == 25


def test_a_new_session_then_a_later_sighting(conn: sqlite3.Connection) -> None:
    state.upsert_session(conn, SESSION, transcript_path=TRANSCRIPT, seen_at=100)
    state.advance_cursor(conn, SESSION, offset=512, inode=7)
    state.set_pending_gap_key(conn, SESSION, "cc1g:" + "c" * 64)
    state.upsert_session(conn, SESSION, transcript_path=TRANSCRIPT, seen_at=200)
    assert state.get_session(conn, SESSION) == state.SessionRow(
        session_hash=SESSION,
        transcript_path=TRANSCRIPT,
        first_seen_at=100,
        last_seen_at=200,
        ended_at=None,
        cursor_offset=512,
        cursor_inode=7,
        pending_gap_key="cc1g:" + "c" * 64,
    )


def test_an_end_is_kept_by_a_later_sighting_without_one(
    conn: sqlite3.Connection,
) -> None:
    state.upsert_session(
        conn, SESSION, transcript_path=TRANSCRIPT, seen_at=100, ended_at=150
    )
    state.upsert_session(conn, SESSION, transcript_path=TRANSCRIPT, seen_at=200)
    row = state.get_session(conn, SESSION)
    assert row is not None and row.ended_at == 150


def test_pending_gap_key_clears_and_sessions_delete(conn: sqlite3.Connection) -> None:
    state.upsert_session(conn, SESSION, transcript_path=TRANSCRIPT, seen_at=100)
    state.upsert_session(conn, OTHER, transcript_path=TRANSCRIPT, seen_at=90)
    state.set_pending_gap_key(conn, SESSION, "cc1g:" + "c" * 64)
    state.set_pending_gap_key(conn, SESSION, None)
    row = state.get_session(conn, SESSION)
    assert row is not None and row.pending_gap_key is None
    assert [s.session_hash for s in state.list_sessions(conn)] == [OTHER, SESSION]
    state.delete_session(conn, SESSION)
    assert state.get_session(conn, SESSION) is None
    assert [s.session_hash for s in state.list_sessions(conn)] == [OTHER]


def test_cursor_moves_on_an_unknown_session_are_no_ops(
    conn: sqlite3.Connection,
) -> None:
    state.advance_cursor(conn, SESSION, offset=10, inode=1)
    state.set_pending_gap_key(conn, SESSION, "cc1g:" + "c" * 64)
    assert state.get_session(conn, SESSION) is None


def test_the_hold_is_one_row_that_sets_and_clears(conn: sqlite3.Connection) -> None:
    assert state.get_hold(conn) == state.NO_HOLD
    state.set_hold(conn, hold_until=1000, hold_step=1)
    state.set_hold(conn, hold_until=2000, hold_step=2)
    assert state.get_hold(conn) == state.Hold(hold_until=2000, hold_step=2)
    assert conn.execute("SELECT count(*) FROM hold").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO hold (id, hold_step) VALUES (2, 0)")
    state.clear_hold(conn)
    assert state.get_hold(conn) == state.NO_HOLD


def test_the_ledger_counts_fixed_reasons_and_accepted_per_client(
    conn: sqlite3.Connection,
) -> None:
    state.bump_ledger(conn, "path_escape", at=100)
    state.bump_ledger(conn, "path_escape", at=150)
    state.bump_ledger(conn, state.accepted_reason("cli"), at=160, by=3)
    state.bump_ledger(conn, state.accepted_reason("claude-desktop"), at=170)
    assert state.get_ledger(conn) == {
        "accepted:claude-desktop": state.LedgerEntry(count=1, last_at=170),
        "accepted:cli": state.LedgerEntry(count=3, last_at=160),
        "path_escape": state.LedgerEntry(count=2, last_at=150),
    }


@pytest.mark.parametrize(
    "reason",
    [
        SESSION,
        TRANSCRIPT,
        "accepted:",
        "accepted:/tmp/rheo-synthetic/x",
        "accepted:" + "x" * 40,
        "made_up_counter",
    ],
)
def test_the_ledger_refuses_anything_but_a_counter_name(
    conn: sqlite3.Connection, reason: str
) -> None:
    with pytest.raises(ValueError) as caught:
        state.bump_ledger(conn, reason, at=100)
    assert reason not in str(caught.value)
    assert state.get_ledger(conn) == {}


def test_a_failed_write_rolls_back_and_leaves_the_connection_usable(
    conn: sqlite3.Connection,
) -> None:
    state.set_hold(conn, hold_until=1000, hold_step=1)
    with pytest.raises(sqlite3.IntegrityError):
        # ``hold_step`` is NOT NULL.
        state.set_hold(conn, hold_until=2000, hold_step=None)  # type: ignore[arg-type]
    assert not conn.in_transaction
    assert state.get_hold(conn) == state.Hold(hold_until=1000, hold_step=1)


class Boom(Exception):
    pass


def _session_count(db_path: Path) -> int:
    reader = sqlite3.connect(db_path)
    try:
        return int(reader.execute("SELECT count(*) FROM session").fetchone()[0])
    finally:
        reader.close()


def test_a_grouped_write_that_raises_midway_leaves_neither_write(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(Boom), state.transaction(conn):
        state.set_spool_offset(conn, "2026-01-01.jsonl", 99)
        state.upsert_session(conn, SESSION, transcript_path=TRANSCRIPT, seen_at=100)
        raise Boom
    assert not conn.in_transaction
    assert state.get_spool_offset(conn, "2026-01-01.jsonl") == 0
    assert state.get_session(conn, SESSION) is None


def test_a_grouped_write_commits_together_only_at_the_outermost_exit(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    with state.transaction(conn):
        state.upsert_session(conn, SESSION, transcript_path=TRANSCRIPT, seen_at=100)
        # Joined, not committed: a second connection sees nothing yet.
        assert conn.in_transaction
        assert _session_count(db_path) == 0
        state.advance_cursor(conn, SESSION, offset=64, inode=3)
        state.bump_ledger(conn, state.accepted_reason("cli"), at=100, by=2)
        state.set_hold(conn, hold_until=500, hold_step=1)
    assert not conn.in_transaction
    assert _session_count(db_path) == 1
    row = state.get_session(conn, SESSION)
    assert row is not None and row.cursor_offset == 64
    assert state.get_ledger(conn)["accepted:cli"].count == 2
    assert state.get_hold(conn) == state.Hold(hold_until=500, hold_step=1)


def test_a_nested_transaction_joins_the_outer_one(conn: sqlite3.Connection) -> None:
    with pytest.raises(Boom), state.transaction(conn):
        with state.transaction(conn):
            state.set_spool_offset(conn, "2026-01-01.jsonl", 7)
        # The inner block's exit did not commit: the outer rollback undoes it.
        assert conn.in_transaction
        raise Boom
    assert state.get_spool_offset(conn, "2026-01-01.jsonl") == 0


def test_a_failed_commit_rolls_back_and_leaves_the_connection_usable(
    conn: sqlite3.Connection,
) -> None:
    # A deferred foreign key is checked only at COMMIT, and SQLite leaves the
    # transaction open when that check fails.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE child (parent_id INTEGER "
        "REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED)"
    )
    with pytest.raises(sqlite3.IntegrityError), state.transaction(conn):
        state.set_spool_offset(conn, "2026-01-01.jsonl", 5)
        conn.execute("INSERT INTO child (parent_id) VALUES (1)")
    assert not conn.in_transaction
    assert state.get_spool_offset(conn, "2026-01-01.jsonl") == 0
    state.set_spool_offset(conn, "2026-01-01.jsonl", 6)
    assert state.get_spool_offset(conn, "2026-01-01.jsonl") == 6


def test_writes_are_committed_for_a_second_connection(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    state.upsert_session(conn, SESSION, transcript_path=TRANSCRIPT, seen_at=100)
    reader = sqlite3.connect(db_path)
    try:
        assert reader.execute("SELECT count(*) FROM session").fetchone() == (1,)
    finally:
        reader.close()
