"""Immutable file publication beneath an existing, trusted secret-store root.

Open every directory without following symlinks and retain its descriptor. Write
and fsync a private staging file before an atomic, no-replace hard link publishes
it. The final directory fsync makes publication durable before success returns.
Discard walks the same way and unlinks one regular file; it never replaces a value.
The configured data root and its ancestors belong to the operator; processes with
the same OS identity remain outside this protection boundary.
"""

import os
import secrets
import stat
from contextlib import ExitStack
from pathlib import Path

from rheo_core.secrets.refs import (
    SECRET_PERMISSIONS,
    SECRET_REF_MALFORMED,
    SECRET_WRITE_FAILED,
    SecretRefusal,
    is_slug_path,
)


def _directory(stack: ExitStack, path: str | Path, parent: int | None = None) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    stack.callback(os.close, fd)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SecretRefusal(
            SECRET_PERMISSIONS,
            "secret directories must be owned by this user with mode 0700",
        )
    return fd


def create_file_secret(root: Path, ref_id: str, raw: bytes) -> None:
    """Refuse unsafe paths and existing names, with content-free failures.

    Failure before publication leaves no value at the requested reference. An I/O
    failure after publication can leave a complete value at that reference: do
    not remove or overwrite it on retry. A killed process can leave an unreferenced
    staging file, which is private and cannot be addressed as a SecretRef.
    """
    if not is_slug_path(ref_id):
        raise SecretRefusal(SECRET_REF_MALFORMED, "file secret id is not a slug path")
    if not isinstance(raw, bytes):
        raise TypeError("file secrets must be bytes")
    try:
        with ExitStack() as stack:
            parent = _directory(stack, root)
            parts = ref_id.split("/")
            for part in parts[:-1]:
                try:
                    os.mkdir(part, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = _directory(stack, part, parent)
                # Also sync existing entries: another creator may just have made
                # the directory and not yet persisted its name.
                os.fsync(parent)
                parent = child
            temporary = ".rheo-secret-" + secrets.token_hex(16)
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(
                        temporary,
                        parts[-1],
                        src_dir_fd=parent,
                        dst_dir_fd=parent,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    raise SecretRefusal(
                        "secret_exists", "secret reference already exists"
                    ) from None
            finally:
                os.unlink(temporary, dir_fd=parent)
            os.fsync(parent)
    except OSError:
        raise SecretRefusal(
            "secret_write_failed", "could not safely persist the new secret"
        ) from None


def discard_file_secret(root: Path, ref_id: str) -> None:
    """Remove one published value, refusing unsafe paths with content-free failures.

    The same descriptor walk as creation, without creating anything: a symlinked or
    non-directory component, or a directory that is not this user's 0700, is
    refused. A leaf that is not a regular file is refused and left in place. A
    missing leaf or directory is already discarded, so it succeeds without change.
    The parent directory is fsynced after the unlink so removal is durable.
    """
    if not is_slug_path(ref_id):
        raise SecretRefusal(SECRET_REF_MALFORMED, "file secret id is not a slug path")
    parts = ref_id.split("/")
    leaf = parts[-1]
    with ExitStack() as stack:
        try:
            parent = _directory(stack, root)
            for part in parts[:-1]:
                parent = _directory(stack, part, parent)
            info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise SecretRefusal(
                SECRET_PERMISSIONS, "secret path is not a private directory"
            ) from None
        if not stat.S_ISREG(info.st_mode):
            raise SecretRefusal(
                SECRET_PERMISSIONS, "secret reference is not a regular file"
            )
        try:
            try:
                os.unlink(leaf, dir_fd=parent)
            except FileNotFoundError:
                return
            os.fsync(parent)
        except OSError:
            raise SecretRefusal(
                SECRET_WRITE_FAILED, "could not safely discard the secret"
            ) from None
