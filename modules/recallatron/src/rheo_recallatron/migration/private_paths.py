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

from psycopg.conninfo import conninfo_to_dict

PRIVATE_ROOT_VARIABLE = "RHEO_MIGRATION_PRIVATE_ROOT"

#: Where the public-checkout lookup starts: this module's own directory, never the
#: working directory (see :func:`_public_checkouts`).
_MODULE_DIR = Path(__file__).resolve().parent

PUBLIC_CHECKOUT_UNKNOWN = "public_checkout_unknown"
PRIVATE_ROOT_UNSET = "private_root_unset"
PRIVATE_ROOT_NOT_ABSOLUTE = "private_root_not_absolute"
PRIVATE_ROOT_MISSING = "private_root_missing"
PRIVATE_ROOT_OVERLAPS_CHECKOUT = "private_root_overlaps_checkout"
PRIVATE_ROOT_NOT_IGNORED = "private_root_not_ignored"
OUTPUT_IS_SYMLINK = "output_is_symlink"
OUTPUT_ESCAPES_ROOT = "output_escapes_root"
DSN_NOT_LOOPBACK = "dsn_not_loopback"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SERVICE_ENVIRONMENT = ("PGSERVICE", "PGSERVICEFILE")
_HOST_ENVIRONMENT = ("PGHOST", "PGHOSTADDR")


class PrivateOutputRefusal(Exception):
    """Raised when a candidate private-data output path or DSN fails its guard."""

    def __init__(self, state: str, detail: str) -> None:
        super().__init__(f"{state}: {detail}")
        self.state = state
        self.detail = detail


def _git_path(start: Path, *args: str) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(start), "rev-parse", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _public_checkouts(start: Path) -> tuple[Path, ...]:
    """The public checkout(s) ``start`` belongs to, independent of the working
    directory: the work tree containing ``start`` itself, plus that repository's
    main work tree (the parent of its common git directory), which differs when
    ``start`` sits in a linked ``git worktree``.

    The lookup starts from this module's own file, never from the process's cwd,
    so the overlap rule gives the same answer wherever a tool is launched from.
    (``find_checkout_root`` is not used here: from this module's path it would
    stop at the nearest ``pyproject.toml``, which is the module's own package
    directory, not the checkout.) Fails closed: when ``start`` is not inside any
    git work tree, the checkout cannot be located and every private root is
    refused rather than waved through.
    """
    toplevel = _git_path(start, "--show-toplevel")
    common_dir = _git_path(start, "--path-format=absolute", "--git-common-dir")
    if toplevel is None or common_dir is None:
        raise PrivateOutputRefusal(
            PUBLIC_CHECKOUT_UNKNOWN,
            f"cannot locate the public checkout from {start}; refusing every "
            "private root rather than guessing",
        )
    checkouts = [toplevel]
    main_worktree = common_dir.parent
    if main_worktree != toplevel:
        checkouts.append(main_worktree)
    return tuple(checkouts)


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

    for checkout in _public_checkouts(_MODULE_DIR):
        if resolved.is_relative_to(checkout) or checkout.is_relative_to(resolved):
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
    if candidate.name in ("", ".", ".."):
        # ``root/sub/..`` would join as ``<resolved root/sub>/..``, which
        # ``os.path.commonpath`` does not normalize, so it would pass the
        # containment check below while naming a path outside the root.
        raise PrivateOutputRefusal(
            OUTPUT_ESCAPES_ROOT,
            f"refusing an output path with no file name of its own: {candidate}",
        )
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
    if common != str(root) or resolved_candidate == root:
        raise PrivateOutputRefusal(
            OUTPUT_ESCAPES_ROOT,
            f"output path escapes the private root {root}: {resolved_candidate}",
        )

    resolved_parent.mkdir(parents=True, exist_ok=True)
    return resolved_candidate


def require_loopback_dsn(dsn: str) -> str:
    """Refuse ``dsn`` unless every host it names is a loopback spelling.

    libpq's own connection-string rules let a ``host`` or ``hostaddr`` query
    parameter override or supplement the URI authority (``hostaddr`` in particular
    is the literal address libpq connects to, taking precedence over ``host`` for
    the actual TCP connection), and either can carry a comma-separated multi-host
    list. Checking only the URI authority (as a plain ``urlsplit`` would) misses
    all of that: ``postgresql://u@localhost/db?host=db.example.com`` and
    ``postgresql://u@localhost/db?hostaddr=192.0.2.1`` both read as ``localhost``
    under a bare authority check while actually connecting elsewhere. Parsing with
    ``psycopg.conninfo.conninfo_to_dict`` (libpq's own parsing rules, not a
    reimplementation) surfaces the real ``host``/``hostaddr`` values libpq would
    use, whichever of the URI or key/value DSN forms was given.

    libpq also fills in what a DSN leaves out from the environment and from a
    connection service file, neither of which the DSN text shows. So a ``service``
    parameter, or ``PGSERVICE`` / ``PGSERVICEFILE`` in the environment, is refused
    outright (a service entry can name any host), and ``PGHOST`` / ``PGHOSTADDR``
    in the environment are refused unless every host they name is loopback
    (``PGHOSTADDR`` in particular overrides the address even when the DSN names a
    loopback ``host``). Returns ``dsn`` as-is.
    """
    try:
        params = conninfo_to_dict(dsn)
    except Exception as error:  # a DSN libpq itself can't parse is never loopback
        # A fixed message: psycopg's parse error can quote DSN fragments (a
        # password among them), so its text stays only on the chained cause.
        raise PrivateOutputRefusal(
            DSN_NOT_LOOPBACK, "refusing an unparseable DSN"
        ) from error

    if params.get("service"):
        raise PrivateOutputRefusal(
            DSN_NOT_LOOPBACK, "refusing a DSN that names a connection service"
        )
    for variable in _SERVICE_ENVIRONMENT:
        if variable in os.environ:
            raise PrivateOutputRefusal(
                DSN_NOT_LOOPBACK,
                f"refusing a DSN while {variable} is set: a service entry can "
                "supply any host",
            )
    for variable in _HOST_ENVIRONMENT:
        for part in os.environ.get(variable, "").split(","):
            if part.strip() and part.strip().strip("[]") not in _LOOPBACK_HOSTS:
                raise PrivateOutputRefusal(
                    DSN_NOT_LOOPBACK,
                    f"refusing a DSN while {variable} names a non-loopback host",
                )

    hosts = [
        part.strip("[]")
        for key in ("host", "hostaddr")
        for part in str(params.get(key) or "").split(",")
        if part
    ]
    if not hosts:
        # No host/hostaddr at all: refuse rather than guess, matching the
        # previous authority-only check's behavior for a DSN with no host.
        raise PrivateOutputRefusal(
            DSN_NOT_LOOPBACK, "refusing a DSN with no host or hostaddr"
        )

    for host in hosts:
        if host not in _LOOPBACK_HOSTS:
            raise PrivateOutputRefusal(
                DSN_NOT_LOOPBACK, f"refusing a non-loopback DSN host: {host!r}"
            )
    return dsn
