"""The stdlib-only Stop/SessionEnd hook (FR 9): parse, append one line, maybe spawn.

Every call goes through ``hook.main`` with a ``tmp_path`` bridge home holding a
synthetic ``config.json``; nothing here reads a real home, a real transcript or a
real settings file. The session ids, salts and paths are invented.
"""

from __future__ import annotations

import ast
import fcntl
import hashlib
import hmac
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from rheo_bridge import config as bridge_config
from rheo_bridge import hook, keys, paths

SESSION_ID = "5f0c2a8e-0000-4000-8000-00000000c0de"
TRANSCRIPT = "/tmp/rheo-synthetic/projects/-tmp-example/5f0c2a8e.jsonl"
SALT = "synthetic-install-salt-0001"
WORKER_ARGV = ["/tmp/rheo-synthetic/venv/bin/rheo-bridge", "drain"]
HOOK_SOURCE = Path(hook.__file__)


class FakeSpawn:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> object:
        self.calls.append(list(argv))
        return None


@pytest.fixture
def bridge_home(tmp_path: Path) -> Path:
    home = paths.ensure_bridge_home(tmp_path / ".rheo-bridge")
    bridge_config.save(
        home,
        bridge_config.Config(
            api_url="https://api.example.org",
            enrollment_id=str(uuid.uuid4()),
            token_expires_at="2026-12-31T00:00:00Z",
            enrolled_dir="/tmp/rheo-synthetic/project",
            worker_argv=WORKER_ARGV,
            install_salt=SALT,
        ),
    )
    return home


@pytest.fixture
def spawn() -> FakeSpawn:
    return FakeSpawn()


def _payload(**extra: object) -> dict[str, object]:
    return {"session_id": SESSION_ID, "transcript_path": TRANSCRIPT, **extra}


def _run(
    bridge_home: Path,
    stdin: bytes,
    spawn: FakeSpawn,
    label: str = "Stop",
    capsys: pytest.CaptureFixture[str] | None = None,
) -> int:
    code = hook.main(
        [label], stdin=io.BytesIO(stdin), bridge_home=bridge_home, spawn=spawn
    )
    if capsys is not None:
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
    return code


def _spool_bytes(bridge_home: Path) -> bytes:
    files = sorted((bridge_home / "spool").glob("*.jsonl"))
    return b"".join(path.read_bytes() for path in files)


def _spool_lines(bridge_home: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in _spool_bytes(bridge_home).splitlines()]


def _assert_one_malformed(bridge_home: Path, label: str = "Stop") -> None:
    lines = _spool_lines(bridge_home)
    assert len(lines) == 1
    assert set(lines[0]) == {"v", "event", "malformed", "at"}
    assert lines[0]["v"] == 1
    assert lines[0]["event"] == label
    assert lines[0]["malformed"] is True
    assert isinstance(lines[0]["at"], int)


def _assert_one_locator(bridge_home: Path, label: str) -> None:
    lines = _spool_lines(bridge_home)
    assert len(lines) == 1
    line = lines[0]
    assert set(line) == {"v", "event", "session_hash", "transcript_path", "at"}
    assert line["event"] == label
    assert line["transcript_path"] == TRANSCRIPT
    session_hash = line["session_hash"]
    assert isinstance(session_hash, str) and len(session_hash) == 32
    assert SESSION_ID.encode() not in _spool_bytes(bridge_home)


# --- configuration --------------------------------------------------------------


@pytest.mark.parametrize(
    "content", [None, b"{not json", b"[]", b'{"install_salt": ""}']
)
def test_a_missing_or_unusable_config_appends_the_malformed_line_and_never_spawns(
    tmp_path: Path,
    spawn: FakeSpawn,
    capsys: pytest.CaptureFixture[str],
    content: bytes | None,
) -> None:
    home = paths.ensure_bridge_home(tmp_path / ".rheo-bridge")
    if content is not None:
        (home / "config.json").write_bytes(content)
    code = _run(home, json.dumps(_payload()).encode(), spawn, capsys=capsys)
    assert code == 0
    _assert_one_malformed(home)
    assert spawn.calls == []


def test_the_hook_reads_the_keys_config_save_writes(bridge_home: Path) -> None:
    """``config.json``'s key names are the contract between config.py and hook.py."""
    raw = json.loads((bridge_home / "config.json").read_text())
    assert raw["install_salt"] == SALT
    assert raw["worker_argv"] == WORKER_ARGV
    assert set(raw) == {
        "api_url",
        "enrollment_id",
        "token_expires_at",
        "enrolled_dir",
        "worker_argv",
        "max_bytes",
        "max_records",
        "max_pending_hours",
        "settle_seconds",
        "install_salt",
    }
    loaded = bridge_config.load(bridge_home)
    assert loaded is not None
    assert (loaded.max_bytes, loaded.max_records, loaded.max_pending_hours) == (
        1_048_576,
        1000,
        24,
    )
    assert loaded.settle_seconds == 5


# --- the locator line -----------------------------------------------------------


@pytest.mark.parametrize("label", ["Stop", "SessionEnd"])
def test_a_valid_payload_appends_one_hashed_line_without_the_session_id(
    bridge_home: Path,
    spawn: FakeSpawn,
    capsys: pytest.CaptureFixture[str],
    label: str,
) -> None:
    code = _run(bridge_home, json.dumps(_payload()).encode(), spawn, label, capsys)
    assert code == 0
    _assert_one_locator(bridge_home, label)
    assert spawn.calls == [WORKER_ARGV]


def test_the_session_hash_is_salted_and_stable(
    bridge_home: Path, spawn: FakeSpawn
) -> None:
    for _ in range(2):
        _run(bridge_home, json.dumps(_payload()).encode(), spawn)
    first, second = _spool_lines(bridge_home)
    assert first["session_hash"] == second["session_hash"]
    unsalted = hashlib.sha256(SESSION_ID.encode()).hexdigest()[:32]
    assert first["session_hash"] != unsalted


def test_spool_file_and_directory_are_private(
    bridge_home: Path, spawn: FakeSpawn
) -> None:
    _run(bridge_home, json.dumps(_payload()).encode(), spawn)
    spool = bridge_home / "spool"
    assert stat.S_IMODE(spool.stat().st_mode) == 0o700
    (spool_file,) = spool.glob("*.jsonl")
    assert stat.S_IMODE(spool_file.stat().st_mode) == 0o600
    paths.check_private(bridge_home)


def _oversize_stop() -> bytes:
    base = json.dumps(_payload(last_assistant_message=""))
    padding = hook.HOOK_STDIN_CAP + 1 - len(base)
    body = json.dumps(_payload(last_assistant_message="x" * padding)).encode()
    assert len(body) == hook.HOOK_STDIN_CAP + 1 == 8_388_609
    return body


@pytest.mark.parametrize(
    "stdin",
    [
        pytest.param(b"{not json", id="malformed-json"),
        pytest.param(b"", id="empty"),
        pytest.param(b"\xff\xfe", id="not-utf8"),
        pytest.param(json.dumps([SESSION_ID, TRANSCRIPT]).encode(), id="non-dict"),
        pytest.param(
            json.dumps({"transcript_path": TRANSCRIPT}).encode(), id="no-session-id"
        ),
        pytest.param(
            json.dumps({"session_id": SESSION_ID}).encode(), id="no-transcript-path"
        ),
        pytest.param(
            json.dumps(_payload(transcript_path="relative/5f0c2a8e.jsonl")).encode(),
            id="relative-transcript-path",
        ),
        pytest.param(
            json.dumps(_payload(session_id=12345)).encode(), id="non-string-id"
        ),
        pytest.param(_oversize_stop(), id="oversize"),
    ],
)
def test_malformed_input_appends_only_the_content_free_line(
    bridge_home: Path,
    spawn: FakeSpawn,
    capsys: pytest.CaptureFixture[str],
    stdin: bytes,
) -> None:
    code = _run(bridge_home, stdin, spawn, capsys=capsys)
    assert code == 0
    _assert_one_malformed(bridge_home)
    assert SESSION_ID.encode() not in _spool_bytes(bridge_home)
    assert TRANSCRIPT.encode() not in _spool_bytes(bridge_home)


def test_an_unknown_event_label_is_malformed(
    bridge_home: Path, spawn: FakeSpawn
) -> None:
    assert _run(bridge_home, json.dumps(_payload()).encode(), spawn, label="Other") == 0
    _assert_one_malformed(bridge_home, label="unknown")


def test_a_stop_over_the_old_64_kib_cap_still_appends_its_locator(
    bridge_home: Path, spawn: FakeSpawn
) -> None:
    body = json.dumps(_payload(last_assistant_message="y" * 100 * 1024)).encode()
    assert 64 * 1024 < len(body) < hook.HOOK_STDIN_CAP
    assert _run(bridge_home, body, spawn) == 0
    _assert_one_locator(bridge_home, "Stop")


def test_no_payload_string_but_the_transcript_path_reaches_the_spool(
    bridge_home: Path, spawn: FakeSpawn
) -> None:
    distinctive = {
        "last_assistant_message": "zebra-marmalade-assistant-reply",
        "cwd": "/tmp/quokka-synthetic-cwd",
        "prompt_id": "axolotl-prompt-id-0042",
        "scratchpad_dir": "/tmp/narwhal-synthetic-scratchpad",
        "permission_mode": "pangolin-permission-mode",
        "effort": "wombat-effort-level",
        "background_tasks": ["capybara-background-task"],
        "session_crons": [{"name": "okapi-session-cron"}],
        "reason": "tapir-end-reason",
    }
    assert _run(bridge_home, json.dumps(_payload(**distinctive)).encode(), spawn) == 0
    written = _spool_bytes(bridge_home)
    needles = [
        "zebra-marmalade",
        "quokka",
        "axolotl",
        "narwhal",
        "pangolin",
        "wombat",
        "capybara",
        "okapi",
        "tapir",
        SESSION_ID,
    ]
    for needle in needles:
        assert needle.encode() not in written, needle
    (line,) = _spool_lines(bridge_home)
    assert set(line) == {"v", "event", "session_hash", "transcript_path", "at"}


# --- exit 0, silent, on every path ----------------------------------------------


def test_an_unwritable_spool_directory_still_exits_zero_silently(
    bridge_home: Path,
    spawn: FakeSpawn,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = bridge_home / "spool"
    spool.mkdir(mode=0o700)
    os.chmod(spool, 0o500)

    # The hook fchmods the spool back to 0700, which its owner may do; a spool
    # that is truly unwritable belongs to someone else, where fchmod is EPERM.
    def not_owner(fd: int, mode: int) -> None:
        raise PermissionError("synthetic: not the owner")

    monkeypatch.setattr(hook.os, "fchmod", not_owner)
    try:
        code = _run(bridge_home, json.dumps(_payload()).encode(), spawn, capsys=capsys)
    finally:
        os.chmod(spool, 0o700)
    assert code == 0
    assert list(spool.iterdir()) == []


def test_a_write_that_raises_still_exits_zero_silently(
    bridge_home: Path,
    spawn: FakeSpawn,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(fd: int, data: bytes) -> int:
        raise OSError("synthetic write failure")

    monkeypatch.setattr(hook.os, "write", boom)
    assert _run(bridge_home, json.dumps(_payload()).encode(), spawn, capsys=capsys) == 0


def test_a_spawn_that_raises_still_exits_zero_silently(
    bridge_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing_spawn(argv: list[str]) -> object:
        raise OSError("synthetic spawn failure")

    code = hook.main(
        ["Stop"],
        stdin=io.BytesIO(json.dumps(_payload()).encode()),
        bridge_home=bridge_home,
        spawn=failing_spawn,
    )
    assert code == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    _assert_one_locator(bridge_home, "Stop")


# --- lock-gated spawn -----------------------------------------------------------


@pytest.fixture
def held_worker_lock(bridge_home: Path) -> Iterator[None]:
    fd = os.open(bridge_home / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@pytest.mark.usefixtures("held_worker_lock")
def test_no_spawn_while_a_worker_holds_the_lock(
    bridge_home: Path, spawn: FakeSpawn
) -> None:
    assert _run(bridge_home, json.dumps(_payload()).encode(), spawn) == 0
    _assert_one_locator(bridge_home, "Stop")
    assert spawn.calls == []


def test_spawn_once_the_lock_is_free(bridge_home: Path, spawn: FakeSpawn) -> None:
    fd = os.open(bridge_home / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    assert _run(bridge_home, json.dumps(_payload()).encode(), spawn) == 0
    assert spawn.calls == [WORKER_ARGV]


# --- stdlib only ----------------------------------------------------------------


def test_every_hook_import_is_in_the_standard_library() -> None:
    tree = ast.parse(HOOK_SOURCE.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "hook.py must not use relative imports"
            assert node.module is not None
            roots.add(node.module.split(".")[0])
    assert roots, "parsed no imports; the check is not looking at hook.py"
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}, roots - set(
        sys.stdlib_module_names
    )


# --- keys -----------------------------------------------------------------------


def test_the_machine_key_is_created_once_private_and_reread(tmp_path: Path) -> None:
    home = paths.ensure_bridge_home(tmp_path / ".rheo-bridge")
    first = keys.load_or_create_machine_key(home)
    assert len(first) == 32
    assert stat.S_IMODE((home / "machine.key").stat().st_mode) == 0o600
    assert keys.load_or_create_machine_key(home) == first
    paths.check_private(home)


def test_the_key_digests_match_spec_a5() -> None:
    key = bytes(range(32))
    assert (
        keys.machine_fingerprint(key)
        == hashlib.sha256(("rheo-machine:" + key.hex()).encode()).hexdigest()
    )
    assert (
        keys.project_fingerprint("/tmp/rheo-synthetic/project")
        == hashlib.sha256(b"rheo-project:/tmp/rheo-synthetic/project").hexdigest()
    )
    record = keys.hmac_key_for(key, SESSION_ID, "0000-uuid")
    assert (
        record
        == hmac.new(key, f"{SESSION_ID}:0000-uuid".encode(), "sha256").hexdigest()
    )
    assert len(record) == 64


def test_check_private_refuses_a_group_readable_file(tmp_path: Path) -> None:
    home = paths.ensure_bridge_home(tmp_path / ".rheo-bridge")
    (home / "token").write_text("synthetic")
    os.chmod(home / "token", 0o644)
    with pytest.raises(paths.InsecurePermissionsError, match="token"):
        paths.check_private(home)


# --- hardening: SIGINT, symlinks, atomic key, modes, config errors ---------------


def test_sigint_while_waiting_on_stdin_still_exits_zero_silently(
    bridge_home: Path, held_worker_lock: None
) -> None:
    """SessionEnd fires on Ctrl-C exits, so the hook itself can receive SIGINT."""
    installed = bridge_home / "hook.py"
    shutil.copyfile(HOOK_SOURCE, installed)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(bridge_home.parent)}
    proc = subprocess.Popen(
        [sys.executable, str(installed), "SessionEnd"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        # Let the interpreter start and install the handler; the hook then
        # blocks reading the still-open stdin.
        time.sleep(1.5)
        proc.send_signal(signal.SIGINT)
        time.sleep(0.2)
        out, err = proc.communicate(json.dumps(_payload()).encode(), timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    assert (out, err) == (b"", b"")
    _assert_one_locator(bridge_home, "SessionEnd")


def test_a_symlinked_spool_directory_fails_closed(
    bridge_home: Path,
    tmp_path: Path,
    spawn: FakeSpawn,
    capsys: pytest.CaptureFixture[str],
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (bridge_home / "spool").symlink_to(elsewhere)
    assert _run(bridge_home, json.dumps(_payload()).encode(), spawn, capsys=capsys) == 0
    assert list(elsewhere.iterdir()) == []


def test_a_symlinked_spool_file_fails_closed(
    bridge_home: Path,
    tmp_path: Path,
    spawn: FakeSpawn,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_bytes(b"")
    spool = bridge_home / "spool"
    spool.mkdir(mode=0o700)
    today = time.strftime("%Y-%m-%d", time.gmtime(time.time())) + ".jsonl"
    (spool / today).symlink_to(target)
    assert _run(bridge_home, json.dumps(_payload()).encode(), spawn, capsys=capsys) == 0
    assert target.read_bytes() == b""


def test_an_existing_loose_spool_is_tightened(
    bridge_home: Path, spawn: FakeSpawn
) -> None:
    spool = bridge_home / "spool"
    spool.mkdir()
    os.chmod(spool, 0o755)
    today = time.strftime("%Y-%m-%d", time.gmtime(time.time())) + ".jsonl"
    (spool / today).write_bytes(b"")
    os.chmod(spool / today, 0o644)
    assert _run(bridge_home, json.dumps(_payload()).encode(), spawn) == 0
    assert stat.S_IMODE(spool.stat().st_mode) == 0o700
    assert stat.S_IMODE((spool / today).stat().st_mode) == 0o600
    _assert_one_locator(bridge_home, "Stop")


def test_a_symlinked_machine_key_is_refused(tmp_path: Path) -> None:
    home = paths.ensure_bridge_home(tmp_path / ".rheo-bridge")
    target = tmp_path / "planted.key"
    target.write_bytes(bytes(32))
    (home / "machine.key").symlink_to(target)
    with pytest.raises(keys.MachineKeyError, match="symlink"):
        keys.load_or_create_machine_key(home)


def test_a_leftover_temp_file_does_not_block_key_creation(tmp_path: Path) -> None:
    home = paths.ensure_bridge_home(tmp_path / ".rheo-bridge")
    leftover = home / ".machine.key.crashed0"
    leftover.write_bytes(b"")
    os.chmod(leftover, 0o600)
    key = keys.load_or_create_machine_key(home)
    assert len(key) == 32
    assert keys.load_or_create_machine_key(home) == key
    # Only the key and the untouched leftover; the creator's own temp is gone.
    assert sorted(p.name for p in home.iterdir()) == [
        ".machine.key.crashed0",
        "machine.key",
    ]


def test_an_empty_machine_key_is_a_clear_error(tmp_path: Path) -> None:
    home = paths.ensure_bridge_home(tmp_path / ".rheo-bridge")
    (home / "machine.key").write_bytes(b"")
    with pytest.raises(keys.MachineKeyError, match="0 bytes"):
        keys.load_or_create_machine_key(home)


@pytest.mark.parametrize("kind", ["not-utf8", "unreadable"])
def test_config_load_wraps_read_failures_as_config_error(
    tmp_path: Path, kind: str
) -> None:
    home = paths.ensure_bridge_home(tmp_path / ".rheo-bridge")
    if kind == "not-utf8":
        (home / "config.json").write_bytes(b"\xff\xfe{}")
    else:
        (home / "config.json").mkdir()
    with pytest.raises(bridge_config.ConfigError):
        bridge_config.load(home)
