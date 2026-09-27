"""Private-output allowlist guard (FR 18, AC 17).

Every ``[REAL]`` migration-groundwork step must refuse to write real predecessor
content anywhere but the one operator-configured private root, and must refuse to
reach anything but a loopback (local test cluster) database host.

:func:`require_private_output` is an **allowlist of one root**, not an ignore check.
The parent workspace (``rheo-stream-workspace/``) is not itself a git repository, so
"refuse inside a work tree unless ignored" would silently pass any path sitting
outside the public checkout entirely — the checkout is the only tree such a check
could see. Instead, the one root named by ``RHEO_MIGRATION_PRIVATE_ROOT`` is checked
directly: it must exist, it must not overlap the public checkout, and when it does
sit inside some git work tree, that work tree must itself ignore it.

:func:`require_loopback_dsn` is the pure DSN-host half of the scratch-cluster guard
Phase 6a's ``scratch_guard.py`` composes. It lives here so the loopback check has one
home and one test, rather than being reimplemented wherever a real DSN needs the same
refusal.
"""

import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from rheo_core.storage.data_root import find_checkout_root

PRIVATE_ROOT_VARIABLE = "RHEO_MIGRATION_PRIVATE_ROOT"

PRIVATE_ROOT_UNSET = "private_root_unset"
PRIVATE_ROOT_NOT_ABSOLUTE = "private_root_not_absolute"
PRIVATE_ROOT_MISSING = "private_root_missing"
PRIVATE_ROOT_OVERLAPS_CHECKOUT = "private_root_overlaps_checkout"
PRIVATE_ROOT_NOT_IGNORED = "private_root_not_ignored"
OUTPUT_IS_SYMLINK = "output_is_symlink"
OUTPUT_ESCAPES_ROOT = "output_escapes_root"
DSN_NOT_LOOPBACK = "dsn_not_loopback"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class PrivateOutputRefusal(Exception):
    """Raised when a candidate private-data output path or DSN fails its guard."""

    def __init__(self, state: str, detail: str) -> None:
        super().__init__(f"{state}: {detail}")
        self.state = state
        self.detail = detail


def _resolved_private_root() -> Path:
    raw = os.environ.get(PRIVATE_ROOT_VARIABLE)
    if not raw:
        raise PrivateOutputRefusal(
            PRIVATE_ROOT_UNSET,
            f"{PRIVATE_ROOT_VARIABLE} is not set; refusing a private-data output",
        )
    root = Path(raw)
    if not root.is_absolute():
        raise PrivateOutputRefusal(
            PRIVATE_ROOT_NOT_ABSOLUTE,
            f"{PRIVATE_ROOT_VARIABLE} must be an absolute path, got {raw!r}",
        )
    try:
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise PrivateOutputRefusal(
            PRIVATE_ROOT_MISSING,
            f"{PRIVATE_ROOT_VARIABLE} does not exist: {root}",
        ) from error

    checkout = find_checkout_root().resolve()
    if (
        resolved == checkout
        or resolved.is_relative_to(checkout)
        or checkout.is_relative_to(resolved)
    ):
        raise PrivateOutputRefusal(
            PRIVATE_ROOT_OVERLAPS_CHECKOUT,
            f"{PRIVATE_ROOT_VARIABLE} must not be inside or contain the public "
            f"checkout {checkout}: {resolved}",
        )

    show_toplevel = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if show_toplevel.returncode == 0:
        ignored = subprocess.run(
            ["git", "-C", str(resolved), "check-ignore", "-q", str(resolved)],
            capture_output=True,
        )
        if ignored.returncode != 0:
            raise PrivateOutputRefusal(
                PRIVATE_ROOT_NOT_IGNORED,
                f"{PRIVATE_ROOT_VARIABLE} is inside a git work tree at "
                f"{show_toplevel.stdout.strip()} but is not itself git-ignored: "
                f"{resolved}",
            )
    return resolved


def require_private_output(path: os.PathLike[str] | str) -> Path:
    """Refuse ``path`` unless it lands inside the one allowlisted private root.

    Checked in order: the private root (see :func:`_resolved_private_root`); that
    ``path`` itself is not a symlink; and that ``path``'s resolved parent, joined
    with its own name, is inside the private root. Creates the parent directories
    only once every check has passed, and returns the resolved candidate path
    (the file itself is not created).
    """
    root = _resolved_private_root()

    candidate = Path(path)
    if candidate.is_symlink():
        raise PrivateOutputRefusal(
            OUTPUT_IS_SYMLINK, f"refusing a symlinked output path: {candidate}"
        )

    resolved_parent = candidate.parent.resolve(strict=False)
    resolved_candidate = resolved_parent / candidate.name
    try:
        common = os.path.commonpath([str(resolved_candidate), str(root)])
    except ValueError:
        common = None
    if common != str(root):
        raise PrivateOutputRefusal(
            OUTPUT_ESCAPES_ROOT,
            f"output path escapes the private root {root}: {resolved_candidate}",
        )

    resolved_parent.mkdir(parents=True, exist_ok=True)
    return resolved_candidate


def require_loopback_dsn(dsn: str) -> str:
    """Refuse ``dsn`` unless its host is a loopback spelling. Returns ``dsn`` as-is."""
    host = urlsplit(dsn).hostname
    if host not in _LOOPBACK_HOSTS:
        raise PrivateOutputRefusal(
            DSN_NOT_LOOPBACK, f"refusing a non-loopback DSN host: {host!r}"
        )
    return dsn
