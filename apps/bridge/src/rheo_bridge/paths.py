"""The layout of the bridge's own state directory, ``bridge_home``.

``bridge_home`` is ``~/.rheo-bridge/`` in production, but only the command-line
entry point resolves the user's home. Every function here takes ``bridge_home``
as an explicit argument and never reads ``$HOME``, ``Path.home()``, ``~`` or the
working directory, so tests run against a temporary directory.

``hook.py`` cannot import this module (it is stdlib-only and runs from its
installed copy), so it repeats the three names it needs: ``config.json``,
``spool/`` and ``worker.lock``. Keep the two in step.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


class InsecurePermissionsError(PermissionError):
    """``bridge_home`` or something inside it is readable by another user."""


def config_path(bridge_home: Path) -> Path:
    return bridge_home / "config.json"


def machine_key_path(bridge_home: Path) -> Path:
    return bridge_home / "machine.key"


def token_path(bridge_home: Path) -> Path:
    return bridge_home / "token"


def spool_dir(bridge_home: Path) -> Path:
    return bridge_home / "spool"


def state_path(bridge_home: Path) -> Path:
    return bridge_home / "state.sqlite"


def hook_path(bridge_home: Path) -> Path:
    """The installed copy of the hook script."""
    return bridge_home / "hook.py"


def worker_lock_path(bridge_home: Path) -> Path:
    return bridge_home / "worker.lock"


def ensure_bridge_home(bridge_home: Path) -> Path:
    """Create ``bridge_home`` at 0700 if absent, and tighten it to 0700 if not."""
    bridge_home.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(bridge_home, DIR_MODE)
    return bridge_home


def _is_os_litter(path: Path, info: os.stat_result) -> bool:
    """A regular file the OS drops into any folder it shows (Finder's
    ``.DS_Store``, AppleDouble ``._*``). It holds no bridge data, and failing
    the worker over its mode would stop capture for nothing."""
    name = path.name
    return stat.S_ISREG(info.st_mode) and (name == ".DS_Store" or name.startswith("._"))


def check_private(bridge_home: Path) -> None:
    """Raise unless ``bridge_home`` is 0700 and everything under it is private.

    Directories (``bridge_home`` itself and ``spool/``) must be 0700 and every
    file 0600. Only what exists is checked: a file not created yet is not a
    finding. Symlinks are refused outright, because a link would let the mode
    check pass on the link while the data sits somewhere else. OS litter
    (``.DS_Store``, ``._*``) that is a regular file is ignored.
    """
    problems: list[str] = []
    for path in (bridge_home, *sorted(bridge_home.rglob("*"))):
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if _is_os_litter(path, info):
            continue
        if stat.S_ISLNK(info.st_mode):
            problems.append(f"{path.name}: is a symlink")
        elif stat.S_ISDIR(info.st_mode):
            if mode != DIR_MODE:
                problems.append(f"{path.name}: directory mode {mode:04o}, want 0700")
        elif mode != FILE_MODE:
            problems.append(f"{path.name}: file mode {mode:04o}, want 0600")
    if problems:
        raise InsecurePermissionsError("; ".join(problems))
