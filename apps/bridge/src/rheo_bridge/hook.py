"""The Claude Code ``Stop``/``SessionEnd`` hook: append one locator line and return.

STANDARD LIBRARY ONLY. This file is copied to ``<bridge_home>/hook.py`` and run as
``<python> <bridge_home>/hook.py <Stop|SessionEnd>`` by Claude Code on every turn
end and session end. It must be fast, and it must keep working when the checkout
that runs the worker is on another branch or missing a dependency, so it imports
no Rheo code (``tests/test_bridge_hook.py`` proves the import set mechanically).

It finds its state from its own location, never from ``$HOME``: ``bridge_home``
is the directory holding this file. It reads ``config.json`` raw for
``install_salt`` and ``worker_argv`` (the key names ``rheo_bridge.config``
writes).

What it writes: one JSON line per call to ``spool/<UTC date>.jsonl``, holding
only the event label, ``session_hash``, ``transcript_path`` and the time. The
session id is never written; it is hashed with the install salt. No other
payload string is kept: a ``Stop`` payload also carries the assistant's reply
text, the working directory and several session fields, and none of them is
read beyond parsing. Any malformed input writes a content-free line instead, so
the worker can still count it. Then, if no worker holds ``worker.lock``, it
spawns the worker detached.

It prints nothing and exits 0 on every path. A hook that fails or talks would
surface in the user's session; a lost locator costs one late drain at most.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO

# 8 MiB. A Stop payload carries the assistant's last reply, so a long reply
# would pass a smaller cap and lose its locator.
HOOK_STDIN_CAP = 8_388_608
EVENTS = frozenset({"Stop", "SessionEnd"})
SPOOL_VERSION = 1

Spawn = Callable[[list[str]], object]


def _read_config(bridge_home: Path) -> tuple[str, list[str]] | None:
    try:
        with open(bridge_home / "config.json", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    salt = raw.get("install_salt")
    argv = raw.get("worker_argv")
    if not isinstance(salt, str) or not salt:
        return None
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(part, str) and part for part in argv)
    ):
        return None
    return salt, list(argv)


def _locator(raw: bytes) -> tuple[str, str] | None:
    """Return ``(session_id, transcript_path)``, or ``None`` if malformed."""
    if len(raw) > HOOK_STDIN_CAP:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(transcript_path, str) or not os.path.isabs(transcript_path):
        return None
    return session_id, transcript_path


def _append(bridge_home: Path, line: dict[str, object], now: float) -> None:
    spool = bridge_home / "spool"
    os.makedirs(spool, mode=0o700, exist_ok=True)
    name = time.strftime("%Y-%m-%d", time.gmtime(now)) + ".jsonl"
    data = (json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(spool / name, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        # One write of one line under O_APPEND, so concurrent hooks do not
        # interleave within a line.
        os.write(fd, data)
    finally:
        os.close(fd)


def _worker_lock_free(bridge_home: Path) -> bool:
    fd = os.open(bridge_home / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def spawn_detached(argv: list[str]) -> object:
    """Start the worker in its own session with every stdio stream on /dev/null."""
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _run(argv: Sequence[str], stdin: BinaryIO, bridge_home: Path, spawn: Spawn) -> None:
    label = argv[0] if argv and argv[0] in EVENTS else "unknown"
    now = time.time()
    # One byte past the cap is enough to know the stream is oversize.
    raw = stdin.read(HOOK_STDIN_CAP + 1)
    config = _read_config(bridge_home)
    locator = _locator(raw) if label != "unknown" else None
    at = int(now)
    if config is None or locator is None:
        _append(
            bridge_home,
            {"v": SPOOL_VERSION, "event": label, "malformed": True, "at": at},
            now,
        )
        if config is None:
            return
    else:
        salt, _ = config
        session_id, transcript_path = locator
        session_hash = hashlib.sha256(
            (salt + ":" + session_id).encode("utf-8")
        ).hexdigest()[:32]
        # Built from these four fields only, never by copying the payload.
        _append(
            bridge_home,
            {
                "v": SPOOL_VERSION,
                "event": label,
                "session_hash": session_hash,
                "transcript_path": transcript_path,
                "at": at,
            },
            now,
        )
    _, worker_argv = config
    if _worker_lock_free(bridge_home):
        spawn(worker_argv)


def main(
    argv: Sequence[str], *, stdin: BinaryIO, bridge_home: Path, spawn: Spawn
) -> int:
    """Run the hook body; always return 0.

    ``argv`` is the arguments after the script name (``["Stop"]``). Tests call
    this with a temporary ``bridge_home`` and a fake ``spawn``.
    """
    try:
        _run(argv, stdin, bridge_home, spawn)
    except Exception:  # noqa: BLE001 - a hook must never break the session
        pass
    return 0


if __name__ == "__main__":
    try:
        main(
            sys.argv[1:],
            stdin=sys.stdin.buffer,
            bridge_home=Path(__file__).resolve().parent,
            spawn=spawn_detached,
        )
    except Exception:  # noqa: BLE001 - a hook must never break the session
        pass
    sys.exit(0)
