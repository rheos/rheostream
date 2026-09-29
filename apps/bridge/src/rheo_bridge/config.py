"""The bridge's local configuration, ``bridge_home/config.json``.

The file is a flat JSON object. ``hook.py`` reads it with a bare ``json.load``
(it is stdlib-only and cannot import this module), so the key names below are
the contract between the two: the hook reads ``install_salt`` and
``worker_argv`` and nothing else.

The three local limits bound what the bridge reads from transcripts and how long
it keeps unsent work; they are not the server's ``automatic_memory.*`` budgets,
which still govern every ingest. The card's fourth limit, 64 units per job, is
the server's claim batching and is deliberately not a key here.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from rheo_bridge.paths import FILE_MODE, config_path

DEFAULT_MAX_BYTES = 1_048_576
DEFAULT_MAX_RECORDS = 1000
DEFAULT_MAX_PENDING_HOURS = 24
DEFAULT_SETTLE_SECONDS = 5


class ConfigError(ValueError):
    """``config.json`` exists but is not a valid bridge configuration."""


@dataclass(frozen=True)
class Config:
    api_url: str
    enrollment_id: str
    token_expires_at: str
    # The realpath of the directory whose sessions are captured.
    enrolled_dir: str
    # The drain entry point of the checkout venv that ran ``init``; not
    # necessarily inside ``enrolled_dir``.
    worker_argv: list[str]
    # Hashes the session id in the hook only; never an HMAC key.
    install_salt: str
    max_bytes: int = DEFAULT_MAX_BYTES
    max_records: int = DEFAULT_MAX_RECORDS
    max_pending_hours: int = DEFAULT_MAX_PENDING_HOURS
    settle_seconds: int = DEFAULT_SETTLE_SECONDS


_STRING_KEYS = (
    "api_url",
    "enrollment_id",
    "token_expires_at",
    "enrolled_dir",
    "install_salt",
)
_INT_KEYS = ("max_bytes", "max_records", "max_pending_hours", "settle_seconds")


def _from_mapping(raw: object) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config.json is not a JSON object")
    values: dict[str, Any] = {}
    for key in _STRING_KEYS:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"config.json: {key} must be a non-empty string")
        values[key] = value
    argv = raw.get("worker_argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(part, str) and part for part in argv)
    ):
        raise ConfigError(
            "config.json: worker_argv must be a non-empty list of strings"
        )
    values["worker_argv"] = list(argv)
    for key in _INT_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        # bool is an int subclass; a JSON true is not a limit.
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"config.json: {key} must be a positive integer")
        values[key] = value
    return Config(**values)


def load(bridge_home: Path) -> Config | None:
    """Return the configuration, or ``None`` when the bridge is not set up."""
    path = config_path(bridge_home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError("config.json is not valid JSON") from exc
    return _from_mapping(raw)


def save(bridge_home: Path, config: Config) -> None:
    """Write ``config.json`` atomically at 0600."""
    document = asdict(config)
    path = config_path(bridge_home)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, indent=2)
            handle.write("\n")
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
