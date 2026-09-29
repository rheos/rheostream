"""The bridge worker's drain, steps 1-5: fold, validate, read, select, sanitize.

Every test builds a temporary ``bridge_home`` and ``projects_root`` and writes
synthetic spool lines and synthetic transcripts into them; nothing here reads a
real home, a real transcript or a real settings file, and the fake client
refuses nothing because nothing may call it yet. Session ids, uuids, salts and
paths are invented.
"""

from __future__ import annotations

import builtins
import fcntl
import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from rheo_bridge import config as bridge_config
from rheo_bridge import keys, paths, state, transcript, worker
from rheo_bridge.client import IngestGap, IngestRecord, IngestResponse
from rheo_bridge.transcript import Ok

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
TODAY_SPOOL = "2026-09-29.jsonl"
YESTERDAY_SPOOL = "2026-09-28.jsonl"
SESSION_ID = "0c1d2e3f-0000-4000-8000-00000000beef"
SESSION_HASH = "5a1e" * 8
OTHER_HASH = "0b0e" * 8


class FakeClient:
    """Records every call; this prompt's drain must make none."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def post(
        self,
        machine_fingerprint: str,
        project_fingerprint: str,
        records: Sequence[IngestRecord],
        gaps: Sequence[IngestGap],
    ) -> IngestResponse:
        self.calls.append((machine_fingerprint, project_fingerprint, records, gaps))
        return IngestResponse((), (), (), ())


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


def drain(env: Env, client: FakeClient) -> int:
    return worker.drain(
        env.bridge_home, projects_root=env.projects_root, client=client, now=lambda: NOW
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


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
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
    else:
        os.mkfifo(path)
    spool(env, [locator("Stop", path=path, at=100)])
    monkeypatch.setattr(transcript, "validate_source", lambda *args, **kwargs: Ok(path))
    batch = only(run_pass(env, conn))
    assert batch.items == ()
    assert batch.source_gap is not None
    assert batch.source_gap.native_key == gap_key(env, SESSION_HASH, 0)
    row = state.get_session(conn, SESSION_HASH)
    assert row is not None and row.pending_gap_key == batch.source_gap.native_key


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


def test_the_drain_folds_and_reads_but_posts_nothing(env: Env) -> None:
    path = env.transcript()
    write_transcript(path, [human("hello there")])
    spool(env, [locator("Stop", path=path, at=100)])
    client = FakeClient()
    assert drain(env, client) == worker.EXIT_OK
    assert client.calls == []
    connection = env.conn()
    try:
        row = state.get_session(connection, SESSION_HASH)
        # No acknowledgement, so the cursor has not moved.
        assert row is not None and row.cursor_offset == 0
    finally:
        state.close(connection)


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
