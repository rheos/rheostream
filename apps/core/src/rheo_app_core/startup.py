"""The ``core`` process's startup sequence, run once from the FastAPI ``lifespan``.

In order: settings → the production/https invariant → data root → the
``secret://env/*`` reference check → ensure the control database and run the
``control`` chain → the identity-provider sync → migrate active workspaces serially
→ build the operation registry → load the modules ``RHEO_MODULES`` names.
"Ensure the control database" treats psycopg's ``DuplicateDatabase`` as success
(``PostgresBackend.ensure_database``), mirroring provisioning's "already exists is a
retry": the advisory lock covers the migration chain, not the ``CREATE DATABASE``
before it, and at ``make demo`` this lifespan and the host-side ``rheo migrate``
start within seconds of each other against one cluster. A workspace whose chain fails
is marked ``unavailable`` by the orchestrator and startup completes; a control-chain
failure raises, because without the control plane nothing is routable.

Module loading comes last because a module registers against the registry the step
before it builds. With ``RHEO_MODULES`` unset — a fresh clone, ``make demo``, and
every image ``make build`` produces — it loads nothing, so that line is a no-op
there by design rather than by luck. See ``rheo_core.modules.loader``.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from rheo_core.identity import sync_providers
from rheo_core.migrations.orchestrator import (
    MigrationResult,
    migrate_active_workspaces,
    migrate_control,
)
from rheo_core.modules import load_modules
from rheo_core.operations import register_core_operations
from rheo_core.routing import SCHEME_KEY
from rheo_core.secrets import check_env_references
from rheo_core.settings import PROFILE_KEY, resolve
from rheo_core.storage.data_root import (
    find_checkout_root,
    resolve_data_root,
    validate_data_root,
)
from rheo_core.storage.postgres import get_backend

logger = logging.getLogger("rheo_app_core.startup")


def _check_production_scheme(profile: str, scheme: str) -> None:
    """Refuse startup when a production profile resolved ``routing.scheme =
    "http"``, naming both values. This process's startup-time companion to
    ``cookies.py``'s own per-request Secure-dropping rule (07's): that rule
    decides, on every request, whether to drop ``Secure`` for a local-http demo;
    this one runs once, at boot, and is the invariant 07 could not implement from
    ``packages/core/`` because it needs both the resolved profile and the routing
    scheme together. Mirrors this file's own env-reference check below: a small
    function, called early, that raises loudly rather than letting a
    misconfigured production deployment serve session cookies over plain http.
    """
    if profile == "production" and scheme == "http":
        raise RuntimeError(
            f"routing.scheme is {scheme!r} but the resolved profile is "
            f"{profile!r}; a production deployment must serve https"
        )


@dataclass(frozen=True, slots=True)
class StartupReport:
    """What startup did, kept on ``app.state.startup`` for diagnostics."""

    profile: str
    data_root: Path
    env_references: tuple[str, ...]
    control_database: str
    workspaces: tuple[MigrationResult, ...]
    operations: tuple[str, ...]


def run_startup() -> StartupReport:
    """The sequence in the module docstring; blocking, run off the event loop."""
    settings = resolve()
    _check_production_scheme(
        settings.get_str(PROFILE_KEY), settings.get_str(SCHEME_KEY)
    )
    resolution = resolve_data_root()
    root = validate_data_root(
        resolution.path,
        find_checkout_root(),
        explicitly_named=resolution.explicitly_named,
    )
    env_references = check_env_references(settings)
    backend = get_backend()
    control = migrate_control(backend)
    sync_providers(backend, settings)
    workspaces = migrate_active_workspaces(backend)
    operations = tuple(sorted(op.name for op in register_core_operations()))
    modules = load_modules()
    report = StartupReport(
        profile=settings.get_str(PROFILE_KEY),
        data_root=root,
        env_references=env_references,
        control_database=control.database_name,
        workspaces=workspaces,
        operations=operations,
    )
    logger.info(
        "core_startup_complete",
        extra={
            "profile": report.profile,
            "data_root": str(root),
            "control_database": report.control_database,
            "workspaces_migrated": sum(1 for w in workspaces if w.ok),
            "workspaces_unavailable": sum(1 for w in workspaces if not w.ok),
            "operations": len(operations),
            # Not a ``StartupReport`` field: nothing reads the ids, and that
            # dataclass is asserted on by tests outside this run's file map.
            "modules": ",".join(modules),
        },
    )
    return report
