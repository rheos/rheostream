"""The data root (FR-10): one operator-configured directory for everything the
deployment writes that is not in Postgres.

Fixed layout under it::

    config/deployment.toml        private deployment settings
    secrets/                      the file secret backend, mode 0700
    workspaces/<workspace_id>/{uploads,exports,runs,runtime}
    logs/
    models/<component>/           model artifacts a local provider caches

Modules receive paths only through ``workspace_dir(ctx, purpose)`` for a purpose from
the closed :class:`Purpose` set, and through :func:`model_cache_dir`, the one
deployment-level accessor (published as ``rheo_core.modules.model_cache_dir``); a
module cannot name a path. The path arithmetic is the pure :func:`workspace_dir_for`,
which tests drive directly because no test may hand-build a ``WorkspaceContext``.
"""

import logging
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from os import environ as _process_environ
from pathlib import Path
from typing import Final
from uuid import UUID

from rheo_contracts import WorkspaceContext

logger = logging.getLogger("rheo_core.storage.data_root")

DATA_ROOT_VARIABLE: Final = "RHEO_DATA_ROOT"
CONTAINER_VARIABLE: Final = "RHEO_IN_CONTAINER"
CONTAINER_DATA_ROOT: Final = Path("/var/lib/rheo-stream")
APP_DIR_NAME: Final = "rheo-stream"
LOCAL_OPT_IN: Final = ".rheo-local"
_CONTAINER_TRUE: Final = frozenset({"1", "true"})

DATA_ROOT_IN_CHECKOUT: Final = "data_root_in_checkout"
DATA_ROOT_SYMLINK_ESCAPES: Final = "data_root_symlink_escapes"
DATA_ROOT_NOT_A_DIRECTORY: Final = "data_root_not_a_directory"


class DataRootRefusal(Exception):
    def __init__(self, state: str, detail: str) -> None:
        super().__init__(f"{state}: {detail}")
        self.state = state
        self.detail = detail


class DataRootSource(StrEnum):
    EXPLICIT = "explicit"
    CONTAINER = "container"
    PLATFORM = "platform"


@dataclass(frozen=True, slots=True)
class DataRootResolution:
    path: Path
    source: DataRootSource

    @property
    def explicitly_named(self) -> bool:
        """True only when ``RHEO_DATA_ROOT`` named the root (branch 1)."""
        return self.source is DataRootSource.EXPLICIT


class Purpose(StrEnum):
    """The closed set of purposes a module may ask a workspace directory for."""

    UPLOADS = "uploads"
    EXPORTS = "exports"
    SCRATCH = "scratch"


# ``runs/`` is core-owned (one directory per CLI runtime operation, removed on
# completion), so a module's scratch space is the fourth fixed subdirectory.
_PURPOSE_SUBDIRS: Final[Mapping[Purpose, str]] = {
    Purpose.UPLOADS: "uploads",
    Purpose.EXPORTS: "exports",
    Purpose.SCRATCH: "runtime",
}
ROOT_SUBDIRS: Final = ("config", "secrets", "workspaces", "logs")

_MODEL_COMPONENT: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
"""One lowercase path segment. No separator can match, and neither can a leading dot,
so ``..`` and a hidden name are refused along with ``a/b``."""


def platform_data_dir(
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    home: Path | None = None,
) -> Path:
    """The platform application-data directory for this app.

    ``~/Library/Application Support/rheo-stream`` on macOS,
    ``%LOCALAPPDATA%\\rheo-stream`` on Windows (``~/AppData/Local`` when the variable
    is unset), and
    ``$XDG_DATA_HOME/rheo-stream`` (default ``~/.local/share``) elsewhere.
    """
    env = _process_environ if environ is None else environ
    platform = sys.platform if system is None else system
    base = Path.home() if home is None else home
    if platform == "darwin":
        return base / "Library" / "Application Support" / APP_DIR_NAME
    if platform.startswith("win"):
        local = env.get("LOCALAPPDATA")
        parent = Path(local) if local else base / "AppData" / "Local"
        return parent / APP_DIR_NAME
    xdg = env.get("XDG_DATA_HOME")
    parent = Path(xdg) if xdg else base / ".local" / "share"
    return parent / APP_DIR_NAME


def resolve_data_root(
    environ: Mapping[str, str] | None = None,
) -> DataRootResolution:
    """Where the data root is, and which branch said so. Reads only; creates nothing.

    In exactly this order: (1) ``RHEO_DATA_ROOT`` if set (an empty value counts as
    unset); (2) ``/var/lib/rheo-stream`` when ``RHEO_IN_CONTAINER=1`` (the Dockerfile
    sets it); (3) the platform application-data directory.

    The container case is checked before the platform case on purpose. The ratified
    sentence (storage-and-workspaces.md § The data root) reads "otherwise the platform
    application-data directory (...); in the container image, ``/var/lib/rheo-stream``",
    and that last clause describes the container context rather than a third fallback:
    the platform directory is always computable, so ordering it second would make the
    container branch dead code and ``ENV RHEO_IN_CONTAINER=1`` a no-op.
    """
    env = _process_environ if environ is None else environ
    named = env.get(DATA_ROOT_VARIABLE, "").strip()
    if named:
        return DataRootResolution(Path(named).expanduser(), DataRootSource.EXPLICIT)
    if env.get(CONTAINER_VARIABLE, "").strip().lower() in _CONTAINER_TRUE:
        return DataRootResolution(CONTAINER_DATA_ROOT, DataRootSource.CONTAINER)
    return DataRootResolution(platform_data_dir(environ=env), DataRootSource.PLATFORM)


def find_checkout_root(start: Path | None = None) -> Path:
    """What :func:`validate_data_root` treats as the source checkout when a process
    must find it for itself: the nearest ancestor of ``start`` (default: the working
    directory), inclusive, holding a ``.git`` entry or a ``pyproject.toml``, else
    ``start`` itself. Shared by the ``rheo`` command and the ``core`` startup."""
    here = (Path.cwd() if start is None else Path(start)).resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").is_file():
            return candidate
    return here


def validate_data_root(
    path: Path, checkout_root: Path, *, explicitly_named: bool
) -> Path:
    """Validate the root at startup and create it (and the top-level layout) if absent.

    Refuses a path inside the source checkout unless it is exactly
    ``<checkout>/.rheo-local`` **and** ``explicitly_named`` is true (``.rheo-local/``
    is used only when ``RHEO_DATA_ROOT`` names it explicitly); refuses a symlink whose
    target escapes its parent; refuses a path that exists and is not a directory. Wide
    permissions are a structured warning, not a refusal, because some filesystems
    cannot express owner-only.

    ``explicitly_named`` is a parameter rather than a lookup so this function never
    reads the environment: ``resolve_data_root()`` knows which branch it took and
    passes ``True`` only for branch (1).
    """
    root = Path(path).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    checkout = Path(checkout_root).expanduser().resolve()
    if root.is_symlink():
        target = root.resolve()
        parent = root.parent.resolve()
        if not target.is_relative_to(parent):
            raise DataRootRefusal(
                DATA_ROOT_SYMLINK_ESCAPES,
                f"data root {root} is a symlink to {target}, outside its parent "
                f"{parent}",
            )
    resolved = root.resolve()
    if resolved.is_relative_to(checkout):
        opt_in = resolved == checkout / LOCAL_OPT_IN
        if not (opt_in and explicitly_named):
            raise DataRootRefusal(
                DATA_ROOT_IN_CHECKOUT,
                f"data root {resolved} is inside the source checkout {checkout}; "
                f"only {checkout / LOCAL_OPT_IN} is allowed, and only when "
                f"{DATA_ROOT_VARIABLE} names it explicitly",
            )
    if not root.exists():
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
    elif not root.is_dir():
        raise DataRootRefusal(
            DATA_ROOT_NOT_A_DIRECTORY, f"data root {root} exists and is not a directory"
        )
    mode = stat.S_IMODE(root.stat().st_mode)
    if mode & 0o077:
        logger.warning(
            "data_root_permissions_wide",
            extra={"data_root": str(root), "mode": f"{mode:04o}"},
        )
    ensure_layout(root)
    return root


def ensure_layout(root: Path) -> None:
    """Create the fixed top-level layout under ``root``; ``secrets/`` is mode 0700."""
    for name in ROOT_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    secrets_dir = root / "secrets"
    secrets_dir.chmod(0o700)


def workspace_dir_for(workspace_id: UUID, purpose: Purpose) -> Path:
    """``<data_root>/workspaces/<workspace_id>/<purpose dir>``. Pure: touches nothing.

    ``purpose`` must be one of the three :class:`Purpose` members (a matching string
    is accepted, anything else is a ``ValueError``); ``workspace_id`` must be a
    ``UUID`` (a string is a ``TypeError``), so no caller can smuggle a path segment.
    """
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a UUID")
    subdir = _PURPOSE_SUBDIRS[Purpose(purpose)]
    root = resolve_data_root().path
    return root / "workspaces" / str(workspace_id) / subdir


def model_cache_dir(component: str) -> Path:
    """``<data_root>/models/<component>``. Pure: touches nothing; the caller creates it.

    Deployment-level rather than under ``workspaces/``, because one model artifact
    serves every workspace. ``component`` must be a single lowercase segment
    (``[a-z0-9][a-z0-9._-]{0,63}``) or this is a ``ValueError``, so no caller can
    smuggle a path segment, the discipline :func:`workspace_dir_for` applies to its
    ``UUID``.

    ``models`` is deliberately not in :data:`ROOT_SUBDIRS`: nothing creates it until a
    provider needs it.
    """
    if not isinstance(component, str) or not _MODEL_COMPONENT.fullmatch(component):
        raise ValueError("a model cache component is one lowercase path segment")
    return resolve_data_root().path / "models" / component


def workspace_dir(ctx: WorkspaceContext, purpose: Purpose) -> Path:
    """The workspace directory for ``purpose`` under the context's workspace."""
    return workspace_dir_for(ctx.workspace_id, purpose)


def run_dir_for(workspace_id: UUID, operation_id: UUID) -> Path:
    """``<data_root>/workspaces/<workspace_id>/runs/<operation_id>/``.

    Pure: touches nothing. Core-owned per-run directory, outside the
    :class:`Purpose` set: a module asks for uploads, exports, or scratch, not for
    a run working directory. Both ids must be ``UUID`` values (a string is a
    ``TypeError``), so no caller can smuggle a path segment.
    """
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a UUID")
    if not isinstance(operation_id, UUID):
        raise TypeError("operation_id must be a UUID")
    root = resolve_data_root().path
    return root / "workspaces" / str(workspace_id) / "runs" / str(operation_id)
