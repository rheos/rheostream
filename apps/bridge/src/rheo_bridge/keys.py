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
from pathlib import Path

from rheo_bridge.paths import FILE_MODE, machine_key_path

MACHINE_KEY_BYTES = 32


def load_or_create_machine_key(bridge_home: Path) -> bytes:
    """Return ``machine.key``, creating it once (32 random bytes, 0600)."""
    path = machine_key_path(bridge_home)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    except FileExistsError:
        key = path.read_bytes()
        if len(key) != MACHINE_KEY_BYTES:
            raise ValueError(
                f"machine.key holds {len(key)} bytes, want {MACHINE_KEY_BYTES}"
            ) from None
        return key
    key = secrets.token_bytes(MACHINE_KEY_BYTES)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    return key


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
