"""The bridge worker's drain: fold, validate, read, select, sanitize (steps
1-5), then post or hold, and settle and close (steps 6-7).

Every test builds a temporary ``bridge_home`` and ``projects_root`` and writes
synthetic spool lines and synthetic transcripts into them; nothing here reads a
real home, a real transcript or a real settings file, and no request leaves the
process: the client is a scripted fake. Session ids, uuids, salts and paths are
invented.

Seams under test (steps 6-7, through :func:`worker.drain`): the cursor commits
only with the acknowledgement that covers it, and never past a deferred key;
the hold backs off and probes; a refusal stops the run with nothing moved; a
session row is deleted only once its whole range is acknowledged.
"""

from __future__ import annotations

import builtins
import errno
import fcntl
import json
import os
import sqlite3
import stat
import tracemalloc
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from rheo_bridge import config as bridge_config
from rheo_bridge import keys, paths, state, transcript, worker
from rheo_bridge.client import (
    IngestGap,
    IngestRecord,
    IngestRefused,
    IngestResponse,
    IngestTransportError,
    request_body,
)
from rheo_bridge.transcript import Ok

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
NOW_TS = int(NOW.timestamp())
TODAY_SPOOL = "2026-09-29.jsonl"
YESTERDAY_SPOOL = "2026-09-28.jsonl"
SESSION_ID = "0c1d2e3f-0000-4000-8000-00000000beef"
SESSION_HASH = "5a1e" * 8
OTHER_HASH = "0b0e" * 8


Responder = Callable[[Sequence[IngestRecord], Sequence[IngestGap]], IngestResponse]


def accept_all(
    records: Sequence[IngestRecord], gaps: Sequence[IngestGap]
) -> IngestResponse:
    return IngestResponse(
        accepted=tuple(r.native_key for r in records),
        deferred=(),
        gapped=tuple(g.native_key for g in gaps),
        dropped=(),
    )


def defer_all(
    records: Sequence[IngestRecord], gaps: Sequence[IngestGap]
) -> IngestResponse:
    return IngestResponse(
        accepted=(),
        deferred=tuple(item.native_key for item in (*records, *gaps)),
        gapped=(),
        dropped=(),
    )


@dataclass(frozen=True)
class Call:
    machine_fingerprint: str
    project_fingerprint: str
    records: tuple[IngestRecord, ...]
    gaps: tuple[IngestGap, ...]

    @property
    def keys(self) -> list[str]:
        return [item.native_key for item in (*self.records, *self.gaps)]


class FakeClient:
    """Records every call and answers through ``respond`` (accept by default)."""

    def __init__(self, respond: Responder = accept_all) -> None:
        self.respond = respond
        self.calls: list[Call] = []

    def post(
        self,
        machine_fingerprint: str,
        project_fingerprint: str,
        records: Sequence[IngestRecord],
        gaps: Sequence[IngestGap],
    ) -> IngestResponse:
        self.calls.append(
            Call(machine_fingerprint, project_fingerprint, tuple(records), tuple(gaps))
        )
        return self.respond(records, gaps)

    @property
    def posted_keys(self) -> list[str]:
        return [key for call in self.calls for key in call.keys]


@dataclass
class Env:
    bridge_home: Path
    projects_root: Path
    enrolled_dir: str
    slug_dir: Path
    machine_key: bytes

    def config(self) -> bridge_config.Config:
        loaded = bridge_config.load(self.bridge_home)
        assert loaded is not None
        return loaded

    def transcript(self, name: str = "session-0001.jsonl") -> Path:
        return self.slug_dir / name

    def conn(self) -> sqlite3.Connection:
        return state.connect(paths.state_path(self.bridge_home))


def make_env(tmp_path: Path, **limits: int) -> Env:
    user_home = tmp_path / "home"
    enrolled = user_home / "code" / "example-project"
    enrolled.mkdir(parents=True)
    enrolled_dir = os.path.realpath(enrolled)
    projects_root = user_home / ".claude" / "projects"
    slug_dir = projects_root / transcript.slug(enrolled_dir)
    slug_dir.mkdir(parents=True)
    bridge_home = paths.ensure_bridge_home(user_home / ".rheo-bridge")
    bridge_config.save(
        bridge_home,
        bridge_config.Config(
            api_url="https://api.example.org",
            enrollment_id=str(uuid.uuid4()),
            token_expires_at="2026-12-31T00:00:00Z",
            enrolled_dir=enrolled_dir,
            worker_argv=["/tmp/rheo-synthetic/venv/bin/rheo-bridge", "drain"],
            install_salt="synthetic-install-salt-0002",
            **limits,
        ),
    )
    machine_key = keys.load_or_create_machine_key(bridge_home)
    return Env(bridge_home, projects_root, enrolled_dir, slug_dir, machine_key)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)


@pytest.fixture
def conn(env: Env) -> Iterator[sqlite3.Connection]:
    connection = env.conn()
    yield connection
    state.close(connection)


def spool(
    env: Env, lines: Sequence[dict[str, Any] | str], name: str = TODAY_SPOOL
) -> Path:
    directory = paths.spool_dir(env.bridge_home)
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / name
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line if isinstance(line, str) else json.dumps(line) + "\n")
    os.chmod(path, 0o600)
    return path


def locator(
    event: str, *, session_hash: str = SESSION_HASH, path: Path | str, at: int
) -> dict[str, Any]:
    return {
        "v": 1,
        "event": event,
        "session_hash": session_hash,
        "transcript_path": str(path),
        "at": at,
    }


def human(
    text: str,
    *,
    line_uuid: str | None = None,
    when: datetime = NOW - timedelta(minutes=5),
    **extra: Any,
) -> dict[str, Any]:
    return {
        "type": "user",
        "origin": {"kind": "human"},
        "sessionId": SESSION_ID,
        "uuid": line_uuid or str(uuid.uuid4()),
        "timestamp": when.isoformat(),
        "entrypoint": "cli",
        "message": {"role": "user", "content": text},
        **extra,
    }


def assistant(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "sessionId": SESSION_ID,
        "uuid": str(uuid.uuid4()),
        "timestamp": NOW.isoformat(),
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def write_transcript(path: Path, lines: Sequence[dict[str, Any]]) -> bytes:
    data = "".join(json.dumps(line) + "\n" for line in lines).encode("utf-8")
    path.write_bytes(data)
    return data


def run_pass(env: Env, connection: sqlite3.Connection) -> list[worker.SessionBatch]:
    worker.fold_spool(connection, env.bridge_home, now=NOW)
    return worker.collect(
        connection,
        config=env.config(),
        machine_key=env.machine_key,
        projects_root=env.projects_root,
        now=NOW,
    )


def only(batches: list[worker.SessionBatch]) -> worker.SessionBatch:
    assert len(batches) == 1, batches
    return batches[0]


def records(batch: worker.SessionBatch) -> list[IngestRecord]:
    return [c.item for c in batch.items if isinstance(c.item, IngestRecord)]


def gap_key(env: Env, session_hash: str, offset: int) -> str:
    return "cc1g:" + keys.hmac_key_for(
        env.machine_key, session_hash, "gap", str(offset)
    )


def acknowledge(connection: sqlite3.Connection, batch: worker.SessionBatch) -> None:
    """Stand in for Prompt 13's acknowledgement commit of a batch's range."""
    state.advance_cursor(
        connection, batch.session_hash, offset=batch.end_offset, inode=batch.inode
    )


def drain(env: Env, client: FakeClient, at: datetime = NOW) -> int:
    return worker.drain(
        env.bridge_home, projects_root=env.projects_root, client=client, now=lambda: at
    )


# --- step 1: fold -------------------------------------------------------------


def test_repeated_and_out_of_order_locators_fold_into_one_row(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    write_transcript(path, [human("first"), human("second")])
    base = int(NOW.timestamp())
    spool(
        env,
        [
            locator("SessionEnd", path=path, at=base - 10),
            locator("Stop", path=path, at=base - 20),
            locator("Stop", path=path, at=base - 20),
            locator("SessionEnd", path=path, at=base - 10),
            # Read last, written earliest: last_seen_at must not move back.
            locator("Stop", path=path, at=base - 30),
        ],
    )
    batch = only(run_pass(env, conn))
    rows = state.list_sessions(conn)
    assert [r.session_hash for r in rows] == [SESSION_HASH]
    assert rows[0].ended_at == base - 10
    assert rows[0].last_seen_at == base - 10
    assert [r.text for r in records(batch)] == ["first", "second"]


def test_stop_only_sessions_are_selected_without_a_session_end(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    write_transcript(path, [human("one"), human("two")])
    spool(env, [locator("Stop", path=path, at=100), locator("Stop", path=path, at=200)])
    batch = only(run_pass(env, conn))
    row = state.get_session(conn, SESSION_HASH)
    assert row is not None and row.ended_at is None and row.last_seen_at == 200
    assert [r.text for r in records(batch)] == ["one", "two"]


def test_a_partial_trailing_spool_line_waits_and_offsets_commit(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    write_transcript(path, [human("hello")])
    whole = json.dumps(locator("Stop", path=path, at=100)) + "\n"
    later = json.dumps(locator("Stop", session_hash=OTHER_HASH, path=path, at=110))
    spool_file = spool(env, [whole, later[:20]])
    worker.fold_spool(conn, env.bridge_home, now=NOW)
    assert [r.session_hash for r in state.list_sessions(conn)] == [SESSION_HASH]
    assert state.get_spool_offset(conn, TODAY_SPOOL) == len(whole.encode())
    spool(env, [later[20:] + "\n"])
    worker.fold_spool(conn, env.bridge_home, now=NOW)
    assert {r.session_hash for r in state.list_sessions(conn)} == {
        SESSION_HASH,
        OTHER_HASH,
    }
    assert state.get_spool_offset(conn, TODAY_SPOOL) == spool_file.stat().st_size
    # A re-fold reads nothing new: the Stop lines are not counted twice.
    worker.fold_spool(conn, env.bridge_home, now=NOW)
    assert len(state.list_sessions(conn)) == 2


def test_a_consumed_spool_file_from_before_today_is_deleted(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    old = spool(env, [locator("Stop", path=path, at=100)], name=YESTERDAY_SPOOL)
    new = spool(env, [locator("Stop", path=path, at=200)])
    worker.fold_spool(conn, env.bridge_home, now=NOW)
    assert not old.exists()
    assert new.exists()
    assert state.get_spool_offset(conn, YESTERDAY_SPOOL) == 0
    assert state.get_spool_offset(conn, TODAY_SPOOL) == new.stat().st_size


def test_labels_come_from_the_malformed_key_first(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    unknown = locator("Stop", path=path, at=100) | {"event": "unknown"}
    flagged = locator("Stop", path=path, at=100) | {"malformed": True}
    # The key's presence decides, not its value.
    flagged_false = locator("Stop", path=path, at=100) | {"malformed": False}
    spool(env, [unknown, flagged, flagged_false, "not json\n"])
    assert run_pass(env, conn) == []
    assert state.list_sessions(conn) == []
    assert state.get_ledger(conn)["hook_malformed"].count == 4


def test_an_unreadable_spool_file_is_skipped_and_counted(
    env: Env, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # A symlink planted after check_private: O_NOFOLLOW refuses it (ELOOP).
    target = tmp_path / "elsewhere.jsonl"
    target.write_text(json.dumps(locator("Stop", path=env.transcript(), at=100)) + "\n")
    directory = paths.spool_dir(env.bridge_home)
    directory.mkdir(mode=0o700)
    (directory / TODAY_SPOOL).symlink_to(target)
    good = spool(
        env,
        [locator("Stop", session_hash=OTHER_HASH, path=env.transcript(), at=100)],
        name=YESTERDAY_SPOOL,
    )
    worker.fold_spool(conn, env.bridge_home, now=NOW)
    assert [r.session_hash for r in state.list_sessions(conn)] == [OTHER_HASH]
    assert state.get_ledger(conn)["spool_unreadable"].count == 1
    assert state.get_spool_offset(conn, TODAY_SPOOL) == 0
    assert not good.exists()


# --- step 2: validate ---------------------------------------------------------


def test_refused_paths_are_dropped_with_their_ledger_count_and_no_candidate(
    env: Env, conn: sqlite3.Connection
) -> None:
    other_project = env.projects_root / "-tmp-some-other-project"
    other_project.mkdir()
    unmatched = other_project / "session-0002.jsonl"
    write_transcript(unmatched, [human("elsewhere")])
    escaping = f"{env.slug_dir}/../{env.slug_dir.name}/session-0003.jsonl"
    spool(
        env,
        [
            locator("Stop", session_hash=SESSION_HASH, path=unmatched, at=100),
            locator("Stop", session_hash=OTHER_HASH, path=escaping, at=100),
        ],
    )
    assert run_pass(env, conn) == []
    assert state.list_sessions(conn) == []
    ledger = state.get_ledger(conn)
    assert ledger["project_unmatched"].count == 1
    assert ledger["path_escape"].count == 1


def test_refused_paths_make_no_request_through_the_drain(env: Env) -> None:
    other_project = env.projects_root / "-tmp-some-other-project"
    other_project.mkdir()
    unmatched = other_project / "session-0002.jsonl"
    write_transcript(unmatched, [human("elsewhere")])
    spool(env, [locator("Stop", path=unmatched, at=100)])
    client = FakeClient()
    assert drain(env, client) == worker.EXIT_OK
    assert client.calls == []
    connection = env.conn()
    try:
        assert state.list_sessions(connection) == []
        assert state.get_ledger(connection)["project_unmatched"].count == 1
    finally:
        state.close(connection)


# --- step 3: read ---------------------------------------------------------------


def test_a_replaced_shorter_transcript_yields_one_gap_and_restarts_at_zero(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    first = write_transcript(path, [human("alpha " * 20), human("beta " * 20)])
    spool(env, [locator("Stop", path=path, at=100)])
    batch = only(run_pass(env, conn))
    assert batch.source_gap is None and batch.end_offset == len(first)
    acknowledge(conn, batch)

    replacement = path.with_name("replacement.tmp")
    write_transcript(replacement, [human("gamma")])
    os.replace(replacement, path)
    batch = only(run_pass(env, conn))
    assert batch.source_gap is not None
    assert batch.source_gap.reason == "source_truncated"
    assert batch.source_gap.native_key == gap_key(env, SESSION_HASH, len(first))
    assert batch.start_offset == 0
    assert [r.text for r in records(batch)] == ["gamma"]
    gaps = [c.item for c in batch.items if isinstance(c.item, IngestGap)]
    assert gaps == []


def test_a_replaced_longer_transcript_is_caught_by_its_inode(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    first = write_transcript(path, [human("alpha")])
    spool(env, [locator("Stop", path=path, at=100)])
    acknowledge(conn, only(run_pass(env, conn)))

    replacement = path.with_name("replacement.tmp")
    write_transcript(replacement, [human("gamma " * 20), human("delta")])
    os.replace(replacement, path)
    assert path.stat().st_size > len(first)
    batch = only(run_pass(env, conn))
    assert batch.source_gap is not None
    assert batch.source_gap.native_key == gap_key(env, SESSION_HASH, len(first))
    assert batch.start_offset == 0
    assert [r.text for r in records(batch)] == [("gamma " * 20).strip(), "delta"]


def test_an_appended_transcript_reads_on_from_its_cursor(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    first = write_transcript(path, [human("alpha")])
    spool(env, [locator("Stop", path=path, at=100)])
    acknowledge(conn, only(run_pass(env, conn)))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(human("beta")) + "\n")
    batch = only(run_pass(env, conn))
    assert batch.source_gap is None
    assert batch.start_offset == len(first)
    assert [r.text for r in records(batch)] == ["beta"]


def test_a_deleted_transcript_yields_the_gap_and_a_pending_key(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    data = write_transcript(path, [human("alpha")])
    spool(env, [locator("Stop", path=path, at=100)])
    acknowledge(conn, only(run_pass(env, conn)))
    path.unlink()
    batch = only(run_pass(env, conn))
    expected = gap_key(env, SESSION_HASH, len(data))
    assert batch.source_gap is not None
    assert batch.source_gap.native_key == expected
    assert batch.items == ()
    row = state.get_session(conn, SESSION_HASH)
    assert row is not None and row.pending_gap_key == expected
    assert SESSION_ID not in expected and SESSION_HASH not in expected


def _open_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    opened: list[str] = []
    real_os_open = os.open
    real_open = builtins.open

    def spy_os_open(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fsdecode(path) if not isinstance(path, int) else str(path))
        return real_os_open(path, *args, **kwargs)

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(file, int):
            opened.append(os.fsdecode(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(builtins, "open", spy_open)
    return opened


def test_a_missing_source_is_never_opened_and_is_read_once_it_appears(
    env: Env, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = env.transcript()
    spool(env, [locator("Stop", path=path, at=100)])
    worker.fold_spool(conn, env.bridge_home, now=NOW)
    opened = _open_spy(monkeypatch)
    batch = only(
        worker.collect(
            conn,
            config=env.config(),
            machine_key=env.machine_key,
            projects_root=env.projects_root,
            now=NOW,
        )
    )
    monkeypatch.undo()
    assert str(path) not in opened
    assert batch.source_gap is not None
    assert batch.source_gap.native_key == gap_key(env, SESSION_HASH, 0)
    assert batch.items == ()
    row = state.get_session(conn, SESSION_HASH)
    assert row is not None and row.pending_gap_key == batch.source_gap.native_key

    write_transcript(path, [human("arrived")])
    batch = only(run_pass(env, conn))
    assert batch.source_gap is None
    assert [r.text for r in records(batch)] == ["arrived"]
    # The source is back, so the gap the Missing pass noted is no longer owed.
    row = state.get_session(conn, SESSION_HASH)
    assert row is not None and row.pending_gap_key is None


@pytest.mark.parametrize("kind", ["symlink", "fifo", "absent"])
def test_a_source_swapped_after_validation_is_not_followed(
    env: Env,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    path = env.transcript()
    if kind == "symlink":
        target = tmp_path / "elsewhere.jsonl"
        write_transcript(target, [human("must not be read")])
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    # "absent": deleted after validation, so the open fails with ENOENT.
    spool(env, [locator("Stop", path=path, at=100)])
    monkeypatch.setattr(transcript, "validate_source", lambda *args, **kwargs: Ok(path))
    batch = only(run_pass(env, conn))
    assert batch.items == ()
    assert batch.source_gap is not None
    assert batch.source_gap.native_key == gap_key(env, SESSION_HASH, 0)
    row = state.get_session(conn, SESSION_HASH)
    assert row is not None and row.pending_gap_key == batch.source_gap.native_key


@pytest.mark.parametrize("failure", ["eacces", "emfile"])
def test_an_unreadable_source_is_skipped_with_no_gap_and_no_state_change(
    env: Env,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    path = env.transcript()
    data = write_transcript(path, [human("alpha")])
    spool(env, [locator("Stop", path=path, at=100)])
    acknowledge(conn, only(run_pass(env, conn)))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(human("beta")) + "\n")
    before = state.get_session(conn, SESSION_HASH)
    if failure == "eacces":
        if os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")
        os.chmod(path, 0)
    else:
        real_open = os.open

        def exhausted(target: Any, *args: Any, **kwargs: Any) -> int:
            if os.fspath(target) == os.fspath(path):
                raise OSError(errno.EMFILE, "Too many open files")
            return real_open(target, *args, **kwargs)

        monkeypatch.setattr(os, "open", exhausted)
    try:
        batches = run_pass(env, conn)
    finally:
        monkeypatch.undo()
        os.chmod(path, 0o600)
    assert batches == []
    assert state.get_session(conn, SESSION_HASH) == before
    assert before is not None and before.pending_gap_key is None
    assert before.cursor_offset == len(data)
    assert state.get_ledger(conn)["source_unreadable"].count == 1
    # Readable again: the next pass reads on from the unchanged cursor.
    batch = only(run_pass(env, conn))
    assert batch.source_gap is None
    assert [r.text for r in records(batch)] == ["beta"]


def test_source_gaps_count_against_the_record_limit(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_records=1)
    spool(
        env,
        [
            locator(
                "Stop",
                session_hash=SESSION_HASH,
                path=env.transcript("a.jsonl"),
                at=100,
            ),
            locator(
                "Stop", session_hash=OTHER_HASH, path=env.transcript("b.jsonl"), at=200
            ),
        ],
    )
    connection = env.conn()
    try:
        batch = only(run_pass(env, connection))
        assert batch.session_hash == SESSION_HASH and batch.source_gap is not None
        later = state.get_session(connection, OTHER_HASH)
        # Over the limit: no gap and no pending key yet; the next pass retries.
        assert later is not None and later.pending_gap_key is None
    finally:
        state.close(connection)


def test_a_replacement_gap_takes_the_last_record_slot(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_records=1)
    path = env.transcript()
    first = write_transcript(path, [human("alpha " * 20)])
    spool(env, [locator("Stop", path=path, at=100)])
    connection = env.conn()
    try:
        acknowledge(connection, only(run_pass(env, connection)))
        write_transcript(path.with_name("r.tmp"), [human("gamma")])
        os.replace(path.with_name("r.tmp"), path)
        batch = only(run_pass(env, connection))
    finally:
        state.close(connection)
    assert batch.source_gap is not None
    assert batch.source_gap.native_key == gap_key(env, SESSION_HASH, len(first))
    assert batch.items == ()
    assert batch.start_offset == batch.end_offset == 0


def test_the_byte_limit_holds_across_sessions(tmp_path: Path) -> None:
    first_lines = [human("a" * 100) for _ in range(3)]
    first_size = sum(len(json.dumps(line)) + 1 for line in first_lines)
    limit = first_size + 50
    env = make_env(tmp_path, max_bytes=limit)
    first_path = env.transcript("a.jsonl")
    second_path = env.transcript("b.jsonl")
    first = write_transcript(first_path, first_lines)
    assert len(first) == first_size
    write_transcript(second_path, [human("b" * 400), human("c")])
    spool(
        env,
        [
            locator("Stop", session_hash=SESSION_HASH, path=first_path, at=100),
            locator("Stop", session_hash=OTHER_HASH, path=second_path, at=200),
        ],
    )
    connection = env.conn()
    try:
        batches = run_pass(env, connection)
    finally:
        state.close(connection)
    assert sum(b.end_offset - b.start_offset for b in batches) <= limit
    by_hash = {b.session_hash: b for b in batches}
    assert by_hash[SESSION_HASH].end_offset == len(first)
    # The second session's first line does not fit what is left, and the
    # whole-line path is only for a pass that has read nothing yet.
    second = by_hash[OTHER_HASH]
    assert second.items == () and second.end_offset == second.start_offset == 0


def test_a_huge_line_is_scanned_over_without_being_buffered(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    huge = 12 * 1024 * 1024
    assert huge > worker.LINE_CEILING_BYTES
    head = b'{"type":"user","pad":"'
    with path.open("wb") as handle:
        handle.write(head)
        block = b"x" * (1024 * 1024)
        for _ in range(huge // len(block)):
            handle.write(block)
        handle.write(b'"}\n')
        tail = (json.dumps(human("after the huge line")) + "\n").encode()
        handle.write(tail)
    huge_end = path.stat().st_size - len(tail)
    spool(env, [locator("Stop", path=path, at=100)])
    worker.fold_spool(conn, env.bridge_home, now=NOW)
    tracemalloc.start()
    try:
        batch = only(
            worker.collect(
                conn,
                config=env.config(),
                machine_key=env.machine_key,
                projects_root=env.projects_root,
                now=NOW,
            )
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 * worker.LINE_CEILING_BYTES, peak
    assert batch.items == ()
    assert batch.skips == (worker.LineSkip("line_oversize", 0, huge_end),)
    assert batch.end_offset == huge_end
    acknowledge(conn, batch)
    batch = only(run_pass(env, conn))
    assert [r.text for r in records(batch)] == ["after the huge line"]


def test_the_ceiling_mirrors_the_servers_text_limit() -> None:
    from rheo_core.evidence.ingest import INGEST_MAX_TEXT_CHARS

    assert worker.INGEST_MAX_TEXT_CHARS == INGEST_MAX_TEXT_CHARS
    assert worker.LINE_CEILING_BYTES == 6 * INGEST_MAX_TEXT_CHARS


def test_a_line_longer_than_the_byte_limit_is_still_consumed(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_bytes=64)
    path = env.transcript()
    data = write_transcript(path, [human("x" * 500), human("next")])
    spool(env, [locator("Stop", path=path, at=100)])
    connection = env.conn()
    try:
        batch = only(run_pass(env, connection))
    finally:
        state.close(connection)
    assert [r.text for r in records(batch)] == ["x" * 500]
    assert batch.end_offset == data.index(b"\n") + 1


def test_the_record_limit_stops_the_read_after_the_last_candidate(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path, max_records=1)
    path = env.transcript()
    data = write_transcript(path, [human("one"), assistant("reply"), human("two")])
    spool(env, [locator("Stop", path=path, at=100)])
    connection = env.conn()
    try:
        batch = only(run_pass(env, connection))
    finally:
        state.close(connection)
    assert [r.text for r in records(batch)] == ["one"]
    assert batch.end_offset == data.index(b"\n") + 1


# --- step 4: select -----------------------------------------------------------


def test_only_human_lines_become_candidates_and_the_cursor_passes_every_line(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    no_clock = human("clockless")
    no_clock["timestamp"] = "not a time"
    data = write_transcript(
        path,
        [
            human("keep one"),
            assistant("an assistant reply"),
            human("meta", isMeta=True),
            human("tool", toolUseResult={"stdout": "x"}),
            no_clock,
            human("keep two"),
            assistant("trailing reply"),
        ],
    )
    spool(env, [locator("Stop", path=path, at=100)])
    batch = only(run_pass(env, conn))
    assert [r.text for r in records(batch)] == ["keep one", "keep two"]
    assert batch.end_offset == len(data)
    assert batch.clock_unreadable == 1
    assert [c.entrypoint for c in batch.items] == ["cli", "cli"]
    # Each candidate and skip carries its own line's exact byte range, so a
    # partial acknowledgement can commit the matching cursor and counts.
    bounds = [0]
    for index, byte in enumerate(data):
        if byte == ord("\n"):
            bounds.append(index + 1)
    lines = list(zip(bounds, bounds[1:], strict=False))
    assert [(c.start_offset, c.end_offset) for c in batch.items] == [
        lines[0],
        lines[5],
    ]
    assert batch.skips == (worker.LineSkip("clock_unreadable", *lines[4]),)


def test_record_and_gap_keys_have_the_fixed_shape_and_hide_their_inputs(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    fresh_uuid = "11111111-2222-4333-8444-555555555555"
    old_uuid = "66666666-7777-4888-8999-aaaaaaaaaaaa"
    write_transcript(
        path,
        [
            human("fresh", line_uuid=fresh_uuid),
            human("stale", line_uuid=old_uuid, when=NOW - timedelta(hours=30)),
        ],
    )
    spool(env, [locator("Stop", path=path, at=100)])
    batch = only(run_pass(env, conn))
    record, gap = (c.item for c in batch.items)
    assert isinstance(record, IngestRecord) and isinstance(gap, IngestGap)
    assert len(record.native_key) == 68 and record.native_key.startswith("cc1:")
    assert len(gap.native_key) == 69 and gap.native_key.startswith("cc1g:")
    assert record.native_key == "cc1:" + keys.hmac_key_for(
        env.machine_key, SESSION_ID, fresh_uuid
    )
    for key in (record.native_key, gap.native_key):
        for secret in (SESSION_ID, fresh_uuid, old_uuid, SESSION_HASH):
            assert secret not in key


# --- step 5: sanitize and expire ------------------------------------------------


def test_an_expired_line_becomes_a_textless_gap_not_a_record(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    stale_uuid = "0f0f0f0f-0000-4000-8000-000000000001"
    when = NOW - timedelta(hours=25)
    write_transcript(path, [human("too old to send", line_uuid=stale_uuid, when=when)])
    spool(env, [locator("Stop", path=path, at=100)])
    batch = only(run_pass(env, conn))
    assert records(batch) == []
    (candidate,) = batch.items
    gap = candidate.item
    assert isinstance(gap, IngestGap)
    assert gap.reason == "expired_pending"
    assert gap.recorded_at == when
    assert gap.native_key == "cc1g:" + keys.hmac_key_for(
        env.machine_key, SESSION_ID, "expired", stale_uuid
    )
    assert not hasattr(gap, "text")


def test_nul_is_scrubbed_and_a_nul_only_line_is_dropped_but_passed(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    data = write_transcript(path, [human("tea\u0000 over"), human("\u0000\u0000")])
    assert b"\\u0000" in data
    spool(env, [locator("Stop", path=path, at=100)])
    batch = only(run_pass(env, conn))
    texts = [r.text for r in records(batch)]
    assert len(texts) == 1 and "\x00" not in texts[0] and texts[0].startswith("tea")
    assert batch.end_offset == len(data)


@pytest.mark.parametrize("reason", ["sanitized_empty", "missing_ids"])
def test_local_drops_count_once_when_cursor_passes_them(
    env: Env, conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str], reason: str
) -> None:
    dropped = human("\u0000\u0000" if reason == "sanitized_empty" else "no ids")
    if reason == "missing_ids":
        dropped.pop("sessionId")
        dropped.pop("uuid")
    data = write_transcript(env.transcript(), [dropped])
    spool(env, [locator("Stop", path=env.transcript(), at=100)])
    client = FakeClient()
    assert drain(env, client) == worker.EXIT_OK
    assert client.calls == []
    row = state.get_session(conn, SESSION_HASH)
    assert row is not None and row.cursor_offset == len(data)
    assert state.get_ledger(conn)[reason].count == 1
    assert drain(env, client) == worker.EXIT_OK
    assert state.get_ledger(conn)[reason].count == 1
    from rheo_bridge import cli

    assert cli.status(env.bridge_home, now=NOW) == 0
    assert f"ledger.{reason}: 1\n" in capsys.readouterr().out


def test_a_lone_surrogate_is_scrubbed_before_sending(
    env: Env, conn: sqlite3.Connection
) -> None:
    path = env.transcript()
    data = write_transcript(path, [human("half \ud800 pair")])
    assert b"\\ud800" in data
    spool(env, [locator("Stop", path=path, at=100)])
    batch = only(run_pass(env, conn))
    (record,) = records(batch)
    assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in record.text)
    json.dumps({"text": record.text}).encode("utf-8")
    record.text.encode("utf-8")


# --- the drain ----------------------------------------------------------------


def test_a_group_readable_bridge_home_stops_the_drain_before_reading(
    env: Env,
) -> None:
    spool(env, [locator("Stop", path=env.transcript(), at=100)])
    os.chmod(env.bridge_home, 0o750)
    client = FakeClient()
    assert drain(env, client) != worker.EXIT_OK
    assert not paths.state_path(env.bridge_home).exists()
    assert not paths.worker_lock_path(env.bridge_home).exists()
    assert client.calls == []


def test_a_second_worker_returns_at_once_without_touching_state(env: Env) -> None:
    spool(env, [locator("Stop", path=env.transcript(), at=100)])
    lock_fd = os.open(
        paths.worker_lock_path(env.bridge_home), os.O_RDWR | os.O_CREAT, 0o600
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert drain(env, FakeClient()) == worker.EXIT_OK
        assert not paths.state_path(env.bridge_home).exists()
    finally:
        os.close(lock_fd)


def test_the_drain_refuses_to_mint_a_machine_key(env: Env) -> None:
    paths.machine_key_path(env.bridge_home).unlink()
    assert drain(env, FakeClient()) != worker.EXIT_OK
    assert not paths.machine_key_path(env.bridge_home).exists()


def test_the_worker_writes_state_privately(env: Env) -> None:
    path = env.transcript()
    write_transcript(path, [human("hello")])
    spool(env, [locator("Stop", path=path, at=100)])
    assert drain(env, FakeClient()) == worker.EXIT_OK
    paths.check_private(env.bridge_home)
    mode = stat.S_IMODE(paths.worker_lock_path(env.bridge_home).stat().st_mode)
    assert mode == 0o600


# --- step 6: post, or hold ------------------------------------------------------


@contextmanager
def opened(env: Env) -> Iterator[sqlite3.Connection]:
    connection = env.conn()
    try:
        yield connection
    finally:
        state.close(connection)


def row_of(env: Env, session_hash: str = SESSION_HASH) -> state.SessionRow | None:
    with opened(env) as connection:
        return state.get_session(connection, session_hash)


def is_open(env: Env, session_hash: str = SESSION_HASH) -> bool:
    row = row_of(env, session_hash)
    return row is not None and row.closed_at is None


def is_closed(env: Env, session_hash: str = SESSION_HASH) -> bool:
    """Closed, and kept as a tombstone at the final cursor."""
    row = row_of(env, session_hash)
    return row is not None and row.closed_at is not None


def cursor_of(env: Env, session_hash: str = SESSION_HASH) -> int:
    row = row_of(env, session_hash)
    assert row is not None
    return row.cursor_offset


def ledger_of(env: Env) -> dict[str, int]:
    with opened(env) as connection:
        return {k: v.count for k, v in state.get_ledger(connection).items()}


def hold_of(env: Env) -> state.Hold:
    with opened(env) as connection:
        return state.get_hold(connection)


def line_bounds(data: bytes) -> list[tuple[int, int]]:
    bounds = [0] + [i + 1 for i, byte in enumerate(data) if byte == ord("\n")]
    return list(zip(bounds, bounds[1:], strict=False))


def rkey(env: Env, line: dict[str, Any]) -> str:
    return worker.record_key(env.machine_key, line["sessionId"], line["uuid"])


def append(path: Path, lines: Sequence[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


def after(minutes: float = 0, seconds: float = 0) -> datetime:
    return NOW + timedelta(minutes=minutes, seconds=seconds)


def until(env: Env) -> datetime:
    """The moment the current hold ends."""
    hold_until = hold_of(env).hold_until
    assert hold_until is not None
    return datetime.fromtimestamp(hold_until, UTC)


def crash(records: Sequence[IngestRecord], gaps: Sequence[IngestGap]) -> IngestResponse:
    raise RuntimeError("synthetic crash before the answer is committed")


def test_the_drain_posts_and_commits_the_cursor_on_acknowledgement(env: Env) -> None:
    path = env.transcript()
    data = write_transcript(path, [human("first"), assistant("reply"), human("second")])
    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])
    client = FakeClient()
    assert drain(env, client) == worker.EXIT_OK
    (call,) = client.calls
    assert call.machine_fingerprint == keys.machine_fingerprint(env.machine_key)
    assert call.project_fingerprint == keys.project_fingerprint(env.enrolled_dir)
    assert [r.text for r in call.records] == ["first", "second"]
    row = row_of(env)
    assert row is not None and row.cursor_offset == len(data)
    assert row.cursor_inode == path.stat().st_ino
    assert ledger_of(env)["accepted:cli"] == 2
    assert hold_of(env) == state.NO_HOLD
    # Nothing new: no request, and the open session keeps its row.
    assert drain(env, client) == worker.EXIT_OK
    assert len(client.calls) == 1
    assert row_of(env) == row


def test_a_partial_answer_stops_the_cursor_at_the_first_deferred_key(env: Env) -> None:
    path = env.transcript()
    no_clock = human("clockless")
    no_clock["timestamp"] = "not a time"
    lines = [human("one"), assistant("reply"), human("two"), no_clock, human("three")]
    data = write_transcript(path, lines)
    bounds = line_bounds(data)
    one, two, three = rkey(env, lines[0]), rkey(env, lines[2]), rkey(env, lines[4])

    def defer_two(
        records: Sequence[IngestRecord], gaps: Sequence[IngestGap]
    ) -> IngestResponse:
        keys_ = tuple(r.native_key for r in records)
        return IngestResponse(tuple(k for k in keys_ if k != two), (two,), (), ())

    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])
    client = FakeClient(defer_two)
    assert drain(env, client) == worker.EXIT_OK
    # The reply between one and two passes; two's line and all after it wait.
    assert cursor_of(env) == bounds[2][0]
    ledger = ledger_of(env)
    assert ledger["accepted:cli"] == 1
    # The clockless line lies past the deferred key: not counted yet.
    assert "clock_unreadable" not in ledger
    assert client.calls[0].keys == [one, two, three]
    assert client.calls[1:] and all(one not in c.keys for c in client.calls[1:])
    # An answer that acknowledges some keys clears no hold it did not set.
    assert hold_of(env) == state.NO_HOLD

    client.respond = accept_all
    assert drain(env, client) == worker.EXIT_OK
    assert cursor_of(env) == len(data)
    ledger = ledger_of(env)
    # three was acknowledged twice but counted once, when the cursor passed it.
    assert ledger["accepted:cli"] == 3
    assert ledger["clock_unreadable"] == 1
    assert client.posted_keys.count(one) == 1


def test_a_range_with_no_candidates_commits_locally_with_no_request(env: Env) -> None:
    path = env.transcript()
    no_clock = human("clockless")
    no_clock["timestamp"] = "not a time"
    data = write_transcript(path, [assistant("a"), no_clock, assistant("b")])
    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])
    client = FakeClient()
    assert drain(env, client) == worker.EXIT_OK
    assert client.calls == []
    assert cursor_of(env) == len(data)
    assert ledger_of(env)["clock_unreadable"] == 1


def test_a_replacement_gap_must_be_acknowledged_before_the_new_file_counts(
    env: Env,
) -> None:
    path = env.transcript()
    first = write_transcript(path, [human("alpha " * 20)])
    old_inode = path.stat().st_ino
    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])
    assert drain(env, FakeClient()) == worker.EXIT_OK
    write_transcript(path.with_name("r.tmp"), [human("gamma")])
    os.replace(path.with_name("r.tmp"), path)
    gap = gap_key(env, SESSION_HASH, len(first))

    def defer_gap(
        records: Sequence[IngestRecord], gaps: Sequence[IngestGap]
    ) -> IngestResponse:
        answer = accept_all(records, gaps)
        return IngestResponse(answer.accepted, (gap,), (), ())

    assert drain(env, FakeClient(defer_gap)) == worker.EXIT_OK
    row = row_of(env)
    assert row is not None
    assert (row.cursor_offset, row.cursor_inode) == (len(first), old_inode)

    client = FakeClient()
    assert drain(env, client) == worker.EXIT_OK
    assert client.calls[0].gaps[0].native_key == gap
    row = row_of(env)
    assert row is not None
    assert (row.cursor_offset, row.cursor_inode) == (
        path.stat().st_size,
        path.stat().st_ino,
    )


# --- AC 4, Edge Case 7: restart recovery ----------------------------------------


def test_a_crash_before_the_acknowledgement_moves_nothing_and_resends(
    env: Env,
) -> None:
    path = env.transcript()
    lines = [human("one"), human("two")]
    data = write_transcript(path, lines)
    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])
    with pytest.raises(RuntimeError):
        drain(env, FakeClient(crash))
    row = row_of(env)
    assert row is not None and row.cursor_offset == 0 and row.cursor_inode is None
    assert "accepted:cli" not in ledger_of(env)

    client = FakeClient()
    assert drain(env, client) == worker.EXIT_OK
    assert client.calls[0].keys == [rkey(env, line) for line in lines]
    assert cursor_of(env) == len(data)


def test_a_crash_mid_drain_resumes_from_the_last_committed_cursor(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path, max_records=2)
    path = env.transcript()
    lines = [human(f"turn {n}") for n in range(4)]
    data = write_transcript(path, lines)
    bounds = line_bounds(data)
    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])
    answers = iter([accept_all, crash])
    client = FakeClient(lambda records, gaps: next(answers)(records, gaps))
    with pytest.raises(RuntimeError):
        drain(env, client)
    # The first pass's range committed; the second's answer never did.
    assert cursor_of(env) == bounds[1][1]
    assert ledger_of(env)["accepted:cli"] == 2

    resumed = FakeClient()
    assert drain(env, resumed) == worker.EXIT_OK
    assert resumed.posted_keys == [rkey(env, line) for line in lines[2:]]
    assert cursor_of(env) == len(data)
    assert ledger_of(env)["accepted:cli"] == 4


def test_a_failed_state_write_after_the_answer_leaves_the_pre_commit_state(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk: the commit fails, the run ends, and nothing half-applies."""
    path = env.transcript()
    data = write_transcript(path, [human("one"), human("two")])
    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])

    def full_disk(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(state, "advance_cursor", full_disk)
    client = FakeClient()
    with pytest.raises(sqlite3.OperationalError):
        drain(env, client)
    monkeypatch.undo()
    row = row_of(env)
    assert row is not None and row.cursor_offset == 0 and row.cursor_inode is None
    # The accepted count bumped before the failing write was rolled back too.
    assert "accepted:cli" not in ledger_of(env)
    assert hold_of(env) == state.NO_HOLD

    again = FakeClient()
    assert drain(env, again) == worker.EXIT_OK
    assert again.posted_keys == client.posted_keys
    assert cursor_of(env) == len(data)
    assert ledger_of(env)["accepted:cli"] == 2


# --- R9: hold and back-off --------------------------------------------------------


def test_an_all_deferred_answer_holds_then_probes_with_one_item(env: Env) -> None:
    path = env.transcript()
    lines = [human(f"turn {n}", when=after(minutes=-10 + n)) for n in range(3)]
    data = write_transcript(path, lines)
    bounds = line_bounds(data)
    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])
    client = FakeClient(defer_all)
    assert drain(env, client) == worker.EXIT_OK
    assert [len(c.keys) for c in client.calls] == [3]
    assert hold_of(env) == state.Hold(hold_until=NOW_TS + 15 * 60, hold_step=1)
    assert cursor_of(env) == 0

    # Inside the window: no request, but every run still folds the spool.
    for minutes in (1, 5, 14):
        other = f"{minutes:04x}" * 16
        empty = env.transcript(f"{other[:8]}.jsonl")
        empty.write_bytes(b"")
        at = NOW_TS + minutes * 60
        spool(env, [locator("Stop", session_hash=other, path=empty, at=at)])
        assert drain(env, client, after(minutes=minutes)) == worker.EXIT_OK
        assert len(client.calls) == 1
        assert row_of(env, other) is not None

    # The window ends: one probe of exactly one item, the oldest.
    probe_at = after(minutes=15)
    assert drain(env, client, probe_at) == worker.EXIT_OK
    assert client.calls[1].keys == [rkey(env, lines[0])]
    assert hold_of(env) == state.Hold(
        hold_until=int(probe_at.timestamp()) + 30 * 60, hold_step=2
    )
    assert cursor_of(env) == 0

    # An acknowledged probe clears the hold and moves only its own line.
    client.respond = accept_all
    assert drain(env, client, until(env)) == worker.EXIT_OK
    assert client.calls[2].keys == [rkey(env, lines[0])]
    assert hold_of(env) == state.NO_HOLD
    assert cursor_of(env) == bounds[0][1]

    # The next run posts the rest in full.
    assert drain(env, client, after(minutes=46)) == worker.EXIT_OK
    assert client.calls[3].keys == [rkey(env, line) for line in lines[1:]]
    assert cursor_of(env) == len(data)


def test_the_hold_window_doubles_and_caps_at_six_hours(env: Env) -> None:
    path = env.transcript()
    write_transcript(path, [human("one")])
    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])

    def unreachable(
        records: Sequence[IngestRecord], gaps: Sequence[IngestGap]
    ) -> IngestResponse:
        raise IngestTransportError("synthetic outage")

    outcomes: list[Responder] = [defer_all, unreachable] * 4
    windows = []
    at = NOW
    for respond in outcomes:
        client = FakeClient(respond)
        assert drain(env, client, at) == worker.EXIT_OK
        assert len(client.calls) == 1
        hold = hold_of(env)
        assert hold.hold_until is not None
        windows.append(hold.hold_until - int(at.timestamp()))
        at = until(env)
    assert windows == [m * 60 for m in (15, 30, 60, 120, 240, 360, 360, 360)]
    assert hold_of(env).hold_step == len(outcomes)
    assert cursor_of(env) == 0


@pytest.mark.parametrize(
    ("code", "http_status", "reason"),
    [
        ("token_revoked", 401, "token_rejected"),
        ("token_expired", 401, "token_rejected"),
        ("operation_not_permitted", 403, "token_not_permitted"),
        ("role_not_permitted", 403, "token_not_permitted"),
        ("enrollment_inactive", 400, "enrollment_inactive"),
        ("enrollment_mismatch", 400, "enrollment_mismatch"),
        ("input_invalid", 422, "input_invalid"),
    ],
)
def test_a_refusal_stops_the_run_with_nothing_moved(
    env: Env, code: str, http_status: int, reason: str
) -> None:
    path = env.transcript()
    lines = [human("one"), human("two")]
    write_transcript(path, lines)
    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])

    def refuse(
        records: Sequence[IngestRecord], gaps: Sequence[IngestGap]
    ) -> IngestResponse:
        raise IngestRefused(code, http_status)

    client = FakeClient(refuse)
    assert drain(env, client) == worker.EXIT_REFUSED
    assert worker.EXIT_REFUSED not in (worker.EXIT_OK, worker.EXIT_FAILURE)
    row = row_of(env)
    assert row is not None
    assert (row.cursor_offset, row.cursor_inode, row.pending_gap_key) == (0, None, None)
    ledger = ledger_of(env)
    assert ledger[reason] == 1
    assert not any(name.startswith("accepted:") for name in ledger)
    # No resend loop: runs inside the window send nothing, and the one after
    # it sends a single probe, never the backlog.
    assert drain(env, client, after(minutes=1)) == worker.EXIT_OK
    assert len(client.calls) == 1
    assert drain(env, client, until(env)) == worker.EXIT_REFUSED
    assert client.calls[1].keys == [rkey(env, lines[0])]
    assert ledger_of(env)[reason] == 2
    assert cursor_of(env) == 0


def test_every_run_inside_fourteen_days_of_expiry_counts_token_expiring(
    env: Env,
) -> None:
    expiring = (NOW + timedelta(days=10)).isoformat()
    bridge_config.save(
        env.bridge_home, replace(env.config(), token_expires_at=expiring)
    )
    path = env.transcript()
    write_transcript(path, [human("one")])
    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])
    client = FakeClient(defer_all)
    assert drain(env, client) == worker.EXIT_OK
    # Held: posts nothing, and still counts.
    assert drain(env, client, after(minutes=1)) == worker.EXIT_OK
    assert len(client.calls) == 1
    assert ledger_of(env)["token_expiring"] == 2


def test_a_distant_expiry_counts_nothing(env: Env) -> None:
    spool(env, [locator("Stop", path=env.transcript(), at=NOW_TS - 60)])
    assert drain(env, FakeClient()) == worker.EXIT_OK
    assert "token_expiring" not in ledger_of(env)


# --- step 7: settle and close -----------------------------------------------------


def test_a_session_with_no_end_keeps_its_row_until_everything_is_acknowledged(
    env: Env,
) -> None:
    """Edge Case 4 and 5: no SessionEnd, idle past ``max_pending_hours``."""
    path = env.transcript()
    idle_since = NOW - timedelta(hours=30)
    lines = [
        human("an old turn", when=idle_since - timedelta(minutes=1)),
        human("a later turn", when=NOW - timedelta(hours=2)),
    ]
    write_transcript(path, lines)
    spool(env, [locator("Stop", path=path, at=int(idle_since.timestamp()))])
    client = FakeClient(defer_all)
    at = NOW
    for _ in range(10):
        assert drain(env, client, at) == worker.EXIT_OK
        row = row_of(env)
        assert row is not None and row.closed_at is None
        assert (row.cursor_offset, row.cursor_inode) == (0, None)
        at = until(env)
    assert len(client.calls) == 10

    client.respond = accept_all
    assert drain(env, client, at) == worker.EXIT_OK  # the probe: one item
    assert is_open(env)
    assert drain(env, client, at) == worker.EXIT_OK
    assert is_closed(env)
    delivered = client.calls[-2:]
    assert sum(len(c.keys) for c in delivered) == 2
    # Aged past max_pending_hours by now: content-free gaps, never text.
    assert all(g.reason == "expired_pending" for c in delivered for g in c.gaps)
    assert delivered[0].gaps and delivered[0].records == ()


def test_an_ended_session_whose_final_batch_is_unacknowledged_is_kept(
    env: Env,
) -> None:
    path = env.transcript()
    write_transcript(path, [human("last words")])
    spool(env, [locator("SessionEnd", path=path, at=NOW_TS - 60)])
    client = FakeClient(defer_all)
    assert drain(env, client) == worker.EXIT_OK
    assert is_open(env)
    assert drain(env, client, after(minutes=1)) == worker.EXIT_OK
    assert is_open(env)
    client.respond = accept_all
    assert drain(env, client, until(env)) == worker.EXIT_OK
    assert is_closed(env)


def test_a_settled_session_with_one_deferred_key_is_kept(env: Env) -> None:
    path = env.transcript()
    lines = [human("one"), human("two")]
    data = write_transcript(path, lines)
    two = rkey(env, lines[1])

    def defer_two(
        records: Sequence[IngestRecord], gaps: Sequence[IngestGap]
    ) -> IngestResponse:
        keys_ = tuple(r.native_key for r in records)
        return IngestResponse(tuple(k for k in keys_ if k != two), (two,), (), ())

    spool(env, [locator("SessionEnd", path=path, at=NOW_TS - 60)])
    assert drain(env, FakeClient(defer_two)) == worker.EXIT_OK
    # Ended and settled, but two is still owed: the row stays.
    assert cursor_of(env) == line_bounds(data)[1][0]
    # The run's second pass re-posted only two, got all-deferred, and held.
    assert drain(env, FakeClient(), until(env)) == worker.EXIT_OK
    assert is_closed(env)


def test_a_line_that_lands_after_the_session_end_is_read_by_the_settle_pass(
    env: Env,
) -> None:
    """[capture 5]: the last turn may reach the file after the hook fires."""
    path = env.transcript()
    data = write_transcript(path, [human("before the end")])
    spool(env, [locator("SessionEnd", path=path, at=NOW_TS)])
    client = FakeClient()
    assert drain(env, client, after(seconds=1)) == worker.EXIT_OK
    # Inside settle_seconds: acknowledged, at EOF, and still not closed.
    assert cursor_of(env) == len(data)
    append(path, [human("after the hook")])
    assert drain(env, client, after(seconds=6)) == worker.EXIT_OK
    assert [r.text for c in client.calls for r in c.records] == [
        "before the end",
        "after the hook",
    ]
    assert is_closed(env)


def test_repeated_and_out_of_order_locators_post_each_turn_once(env: Env) -> None:
    """AC 5 through the posting path."""
    path = env.transcript()
    lines = [human(f"turn {n}") for n in range(3)]
    write_transcript(path, lines[:2])
    spool(
        env,
        [
            locator("SessionEnd", path=path, at=NOW_TS),
            locator("Stop", path=path, at=NOW_TS - 20),
            locator("Stop", path=path, at=NOW_TS - 20),
        ],
    )
    client = FakeClient()
    assert drain(env, client, after(seconds=1)) == worker.EXIT_OK
    append(path, lines[2:])
    spool(
        env,
        [
            locator("Stop", path=path, at=NOW_TS - 30),
            locator("SessionEnd", path=path, at=NOW_TS),
            locator("Stop", path=path, at=NOW_TS - 5),
        ],
    )
    assert drain(env, client, after(seconds=2)) == worker.EXIT_OK
    assert drain(env, client, after(seconds=10)) == worker.EXIT_OK
    assert sorted(client.posted_keys) == sorted(rkey(env, line) for line in lines)
    assert ledger_of(env)["accepted:cli"] == 3
    assert is_closed(env)


def test_accepted_counts_are_kept_per_entrypoint_and_never_sent(env: Env) -> None:
    """[capture 8]: ``entrypoint`` is local-only."""
    cli_path, desk_path = env.transcript("cli.jsonl"), env.transcript("desk.jsonl")
    write_transcript(cli_path, [human("typed in a terminal") for _ in range(2)])
    desk = [human("typed in the app", entrypoint="claude-desktop") for _ in range(4)]
    write_transcript(desk_path, desk)
    dropped = rkey(env, desk[0])

    def drop_one(
        records: Sequence[IngestRecord], gaps: Sequence[IngestGap]
    ) -> IngestResponse:
        keys_ = tuple(r.native_key for r in records)
        return IngestResponse(
            tuple(k for k in keys_ if k != dropped), (), (), (dropped,)
        )

    spool(
        env,
        [
            locator("Stop", session_hash=SESSION_HASH, path=cli_path, at=NOW_TS - 60),
            locator("Stop", session_hash=OTHER_HASH, path=desk_path, at=NOW_TS - 60),
        ],
    )
    client = FakeClient(drop_one)
    assert drain(env, client) == worker.EXIT_OK
    ledger = ledger_of(env)
    assert ledger["accepted:cli"] == 2
    # A dropped record is acknowledged (the cursor passes it) but not accepted.
    assert ledger["accepted:claude-desktop"] == 3
    assert cursor_of(env, OTHER_HASH) == desk_path.stat().st_size
    assert all("entrypoint" not in vars(r) for c in client.calls for r in c.records)
    for call in client.calls:
        body = request_body(
            call.machine_fingerprint, call.project_fingerprint, call.records, call.gaps
        )
        assert b"entrypoint" not in body and b"claude-desktop" not in body


def test_a_gone_source_closes_once_its_gap_comes_back_gapped(env: Env) -> None:
    path = env.transcript()
    data = write_transcript(path, [human("alpha")])
    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])
    client = FakeClient()
    assert drain(env, client) == worker.EXIT_OK
    path.unlink()
    assert drain(env, client) == worker.EXIT_OK
    gap = client.calls[-1].gaps[0]
    assert gap.native_key == gap_key(env, SESSION_HASH, len(data))
    assert gap.reason == "source_truncated"
    assert is_closed(env)


def test_a_deferred_gap_keeps_the_row_and_its_pending_key(env: Env) -> None:
    path = env.transcript()
    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])
    assert drain(env, FakeClient(defer_all)) == worker.EXIT_OK
    row = row_of(env)
    assert row is not None and row.pending_gap_key == gap_key(env, SESSION_HASH, 0)


def test_a_source_back_before_the_close_keeps_its_row(env: Env) -> None:
    """Rule (b) re-validates: the gap came back gapped, but the file returned."""
    path = env.transcript()
    spool(env, [locator("Stop", path=path, at=NOW_TS - 60)])

    def restore_then_accept(
        records: Sequence[IngestRecord], gaps: Sequence[IngestGap]
    ) -> IngestResponse:
        write_transcript(path, [human("back again")])
        return accept_all(records, gaps)

    client = FakeClient(restore_then_accept)
    assert drain(env, client) == worker.EXIT_OK
    assert client.calls[0].gaps[0].native_key == gap_key(env, SESSION_HASH, 0)
    row = row_of(env)
    assert row is not None and row.pending_gap_key is None


def at_ts(moment: datetime) -> int:
    return int(moment.timestamp())


def test_an_idle_closed_session_sighted_a_day_later_sends_only_its_new_lines(
    env: Env,
) -> None:
    """The tombstone keeps the final cursor, so the accepted turn, by now older
    than max_pending_hours, is not replayed as an ``expired_pending`` gap."""
    path = env.transcript()
    first = human("first turn", when=after(minutes=-10))
    write_transcript(path, [first])
    spool(env, [locator("Stop", path=path, at=NOW_TS - 600)])
    client = FakeClient()
    assert drain(env, client) == worker.EXIT_OK
    # Idle past max_pending_hours with nothing new: closed with no request.
    assert drain(env, client, after(minutes=25 * 60)) == worker.EXIT_OK
    assert is_closed(env) and len(client.calls) == 1

    later = after(minutes=49 * 60)
    second = human("next day turn", when=later - timedelta(minutes=1))
    append(path, [second])
    spool(
        env,
        [locator("Stop", path=path, at=at_ts(later) - 30)],
        name="2026-10-01.jsonl",
    )
    assert drain(env, client, later) == worker.EXIT_OK
    assert client.calls[1].keys == [rkey(env, second)]
    assert all(call.gaps == () for call in client.calls)
    assert ledger_of(env)["accepted:cli"] == 2
    assert is_open(env) and cursor_of(env) == path.stat().st_size


def test_a_sighting_from_before_the_close_leaves_the_tombstone(env: Env) -> None:
    path = env.transcript()
    write_transcript(path, [human("done")])
    spool(env, [locator("SessionEnd", path=path, at=NOW_TS - 60)])
    client = FakeClient()
    assert drain(env, client) == worker.EXIT_OK
    assert is_closed(env)
    spool(env, [locator("Stop", path=path, at=NOW_TS - 30)])
    assert drain(env, client, after(minutes=1)) == worker.EXIT_OK
    assert is_closed(env) and len(client.calls) == 1


def test_a_closed_session_whose_file_was_replaced_follows_the_replacement_rule(
    env: Env,
) -> None:
    path = env.transcript()
    first = write_transcript(path, [human("alpha " * 20)])
    spool(env, [locator("SessionEnd", path=path, at=NOW_TS - 60)])
    client = FakeClient()
    assert drain(env, client) == worker.EXIT_OK
    assert is_closed(env)
    fresh = human("gamma")
    write_transcript(path.with_name("r.tmp"), [fresh])
    os.replace(path.with_name("r.tmp"), path)
    spool(env, [locator("Stop", path=path, at=NOW_TS + 60)])
    assert drain(env, client, after(minutes=2)) == worker.EXIT_OK
    call = client.calls[1]
    assert [g.native_key for g in call.gaps] == [gap_key(env, SESSION_HASH, len(first))]
    assert [r.native_key for r in call.records] == [rkey(env, fresh)]
    row = row_of(env)
    assert row is not None and row.closed_at is None
    assert (row.cursor_offset, row.cursor_inode) == (
        path.stat().st_size,
        path.stat().st_ino,
    )


def test_tombstones_are_pruned_after_the_retention_bound(env: Env) -> None:
    path = env.transcript()
    write_transcript(path, [human("done")])
    spool(env, [locator("SessionEnd", path=path, at=NOW_TS - 60)])
    assert drain(env, FakeClient()) == worker.EXIT_OK
    assert is_closed(env)
    retention = timedelta(seconds=worker.TOMBSTONE_RETENTION_SECONDS)
    assert retention == timedelta(days=30)
    assert drain(env, FakeClient(), NOW + retention) == worker.EXIT_OK
    assert is_closed(env)
    past = NOW + retention + timedelta(seconds=1)
    assert drain(env, FakeClient(), past) == worker.EXIT_OK
    assert row_of(env) is None


@pytest.mark.parametrize("outcome", ["deferred", "unreachable", "refused"])
def test_a_range_with_no_candidates_commits_even_when_the_run_holds(
    env: Env, outcome: str
) -> None:
    quiet, busy = env.transcript("quiet.jsonl"), env.transcript("busy.jsonl")
    data = write_transcript(quiet, [assistant("a"), assistant("b")])
    write_transcript(busy, [human("one")])
    spool(
        env,
        [
            locator("Stop", session_hash=SESSION_HASH, path=quiet, at=NOW_TS - 60),
            locator("Stop", session_hash=OTHER_HASH, path=busy, at=NOW_TS - 60),
        ],
    )

    def fail(
        records: Sequence[IngestRecord], gaps: Sequence[IngestGap]
    ) -> IngestResponse:
        if outcome == "unreachable":
            raise IngestTransportError("synthetic outage")
        raise IngestRefused("token_revoked", 401)

    client = FakeClient(defer_all if outcome == "deferred" else fail)
    expected = worker.EXIT_REFUSED if outcome == "refused" else worker.EXIT_OK
    assert drain(env, client) == expected
    assert len(client.calls) == 1 and hold_of(env).hold_step == 1
    assert cursor_of(env, SESSION_HASH) == len(data)
    assert cursor_of(env, OTHER_HASH) == 0


def test_a_session_parked_behind_the_byte_budget_is_read_first_next_pass(
    tmp_path: Path,
) -> None:
    """The start order rotates each pass, so a long line is not starved."""
    first_lines = [human("a" * 40) for _ in range(6)]
    size = len(json.dumps(first_lines[0])) + 1
    env = make_env(tmp_path, max_bytes=2 * size + 10)
    first_path, second_path = env.transcript("a.jsonl"), env.transcript("b.jsonl")
    write_transcript(first_path, first_lines)
    big = human("b" * (4 * size))
    write_transcript(second_path, [big])
    spool(
        env,
        [
            locator("Stop", session_hash=SESSION_HASH, path=first_path, at=NOW_TS - 90),
            locator("Stop", session_hash=OTHER_HASH, path=second_path, at=NOW_TS - 60),
        ],
    )
    client = FakeClient()
    assert drain(env, client) == worker.EXIT_OK
    first_call, second_call = client.calls[:2]
    assert rkey(env, big) not in first_call.keys
    assert second_call.keys == [rkey(env, big)]
    assert cursor_of(env, OTHER_HASH) == second_path.stat().st_size
