"""The laptop's machine key and the digests derived from it (spec A5).

Nothing that leaves the laptop names a session, a message, a path or a project:
records and gaps carry HMACs under ``machine.key``, and each request carries the
two fingerprints below. Losing ``machine.key`` means a re-enrolled machine mints
new keys, so a replay under the new key is not recognised as already held.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import tempfile
from pathlib import Path

from rheo_bridge.paths import FILE_MODE, machine_key_path

MACHINE_KEY_BYTES = 32


class MachineKeyError(ValueError):
    """``machine.key`` exists but cannot be used (a symlink, or the wrong size)."""


def _read_machine_key(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if os.path.islink(path):
            raise MachineKeyError(f"{path.name} is a symlink; refusing it") from exc
        raise
    with os.fdopen(fd, "rb") as handle:
        key = handle.read(MACHINE_KEY_BYTES + 1)
    if len(key) != MACHINE_KEY_BYTES:
        raise MachineKeyError(
            f"{path.name} holds {len(key)} bytes, want {MACHINE_KEY_BYTES}"
        )
    return key


def load_or_create_machine_key(bridge_home: Path) -> bytes:
    """Return ``machine.key``, creating it once (32 random bytes, 0600).

    Creation is atomic: the key is written and fsynced to a private temp file,
    then hard-linked into place. ``link`` fails if anything (a file or a
    symlink) already holds the name, so a crash can leave only a stray temp
    file, never a short ``machine.key``, and two racing creators agree on one
    key. A symlinked ``machine.key`` is refused, never read through.
    """
    path = machine_key_path(bridge_home)
    if os.path.lexists(path):
        return _read_machine_key(path)
    fd, tmp_name = tempfile.mkstemp(prefix=".machine.key.", dir=bridge_home)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(secrets.token_bytes(MACHINE_KEY_BYTES))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, FILE_MODE)
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            pass  # another creator won the race; use its key
    finally:
        os.unlink(tmp_name)
    return _read_machine_key(path)


def machine_fingerprint(machine_key: bytes) -> str:
    return hashlib.sha256(("rheo-machine:" + machine_key.hex()).encode()).hexdigest()


def project_fingerprint(enrolled_dir_realpath: str) -> str:
    return hashlib.sha256(
        ("rheo-project:" + enrolled_dir_realpath).encode("utf-8")
    ).hexdigest()


def hmac_key_for(machine_key: bytes, *parts: str) -> str:
    """``HMAC-SHA256(machine_key, ":".join(parts))`` as 64 hex characters.

    ``hmac_key_for(key, session_id, uuid)`` is spec A5's
    ``HMAC(machine key, sessionId + ":" + uuid)``.
    """
    message = ":".join(parts).encode("utf-8")
    return hmac.new(machine_key, message, hashlib.sha256).hexdigest()
