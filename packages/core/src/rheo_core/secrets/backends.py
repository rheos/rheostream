"""The two release-one backends: one file per id under ``<data_root>/secrets/``, and
the environment. Both return raw bytes; the store wraps them in ``SecretValue``.

The ``secrets/`` directory itself (mode 0700) is created by the data-root layout,
``rheo_core.storage.data_root.ensure_layout``, which is its single owner; the file
backend requires that root before creating scoped values. A managed secret manager
would be a third backend a deployment adds later; nothing in release one needs one.
"""

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from rheo_core.secrets.refs import (
    SECRET_MISSING,
    SECRET_PERMISSIONS,
    SECRET_REF_MALFORMED,
    SecretRefusal,
    is_slug_path,
)
from rheo_core.secrets.write import create_file_secret

# Any bit outside owner read/write: group, other, or owner-execute.
_BEYOND_OWNER_RW = 0o177


class FileBackend:
    """``<root>/<id>``, read verbatim (write files with ``printf '%s'``, not ``echo``).

    ``read`` validates the id as a slug path itself (lowercase slug segments only: no
    ``..``, no empty segment, no absolute path), so ``root / id`` cannot leave the root
    even for a caller that did not go through ``SecretRef``. A file whose mode is
    looser than 0600 is ``secret_permissions``; a missing one is ``secret_missing``
    naming the path.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def create(self, ref_id: str, raw: bytes) -> None:
        """Create once, privately and atomically; never overwrite a reference."""
        create_file_secret(self.root, ref_id, raw)

    def read(self, ref_id: str) -> bytes:
        if not is_slug_path(ref_id):
            raise SecretRefusal(
                SECRET_REF_MALFORMED,
                "file secret id is not a slug path, so it may not name a path",
            )
        path = self.root / ref_id
        try:
            info = path.stat()
        except FileNotFoundError:
            raise SecretRefusal(
                SECRET_MISSING, f"secret file {path} does not exist"
            ) from None
        if not stat.S_ISREG(info.st_mode):
            raise SecretRefusal(
                SECRET_MISSING, f"secret file {path} is not a regular file"
            )
        mode = stat.S_IMODE(info.st_mode)
        if mode & _BEYOND_OWNER_RW:
            raise SecretRefusal(
                SECRET_PERMISSIONS,
                f"secret file {path} has mode {mode:04o}; it must be 0600 or stricter",
            )
        return path.read_bytes()


class EnvBackend:
    """The named environment variable, UTF-8 encoded; missing is ``secret_missing``."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ: Mapping[str, str] = os.environ if environ is None else environ

    def has(self, name: str) -> bool:
        return name in self._environ

    def read(self, name: str) -> bytes:
        value = self._environ.get(name)
        if value is None:
            raise SecretRefusal(
                SECRET_MISSING, f"environment variable {name} is not set"
            )
        return value.encode("utf-8")
