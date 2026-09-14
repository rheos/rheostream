"""``rheo doctor``: data-root validity, cluster reachability, the control-plane head,
the connection budget, the reconcile interval, per-workspace state, and the
``CREATE EXTENSION`` privilege report for ``vector`` and ``pg_trgm``.

The privilege report is kept in this run per the run spec (Technical Risks item 11):
nothing in 0b installs an extension, but ``storage.template_database`` and this
report ship now so phase 2 meets a documented remedy rather than a surprise. The
catalog reads live on the storage backend (``PostgresBackend.server_version`` /
``max_connections`` / ``extension_report``: read only, never a ``CREATE EXTENSION``);
this module only formats them. An extension is creatable by a superuser, or by a role
with ``CREATE`` on the database when the extension is marked trusted (Postgres 13+).

Each check is one line on stdout (``ok``, ``warn`` or ``FAIL``); the exit code is 1
when any check failed. Every step runs even when an earlier one failed, so one
report shows everything that is wrong.
"""

import argparse
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from rheo_core.migrations.orchestrator import (
    CONTROL_CHAIN,
    known_revisions,
    recorded_revisions,
)
from rheo_core.secrets import SecretRefusal, check_env_references
from rheo_core.settings import PROFILE_KEY, SettingsError, resolve
from rheo_core.storage.backend import StorageRefusal
from rheo_core.storage.control_plane import list_workspaces
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.data_root import DataRootRefusal, resolve_data_root
from rheo_core.storage.postgres import PostgresBackend, get_backend
from sqlalchemy.exc import SQLAlchemyError

from rheo_app_cli.context import data_root

EXTENSIONS: Final = ("vector", "pg_trgm")
RECONCILE_KEY: Final = "work.due_reconcile_seconds"
IDLE_CLOSE_KEY: Final = "storage.pool_idle_close_seconds"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str  # "ok" | "warn" | "FAIL"
    detail: str

    def line(self) -> str:
        return f"{self.level:<4} {self.name}: {self.detail}"


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("doctor", help="diagnose the deployment")
    parser.set_defaults(handler=doctor)


def _check_data_root() -> Check:
    try:
        resolution = resolve_data_root()
        root = data_root()
    except DataRootRefusal as refusal:
        return Check("data root", "FAIL", f"{refusal.state}: {refusal.detail}")
    return Check("data root", "ok", f"{root} ({resolution.source.value})")


def _check_settings() -> Check:
    try:
        settings = resolve()
        checked = check_env_references(settings)
    except (SettingsError, SecretRefusal) as refusal:
        return Check("settings", "FAIL", f"{refusal.state}: {refusal.detail}")
    return Check(
        "settings",
        "ok",
        f"profile {settings.get_str(PROFILE_KEY)}; env references present: "
        f"{', '.join(checked) or 'none'}",
    )


def _check_cluster() -> tuple[Check, PostgresBackend | None]:
    try:
        backend = get_backend()
        version = backend.server_version()
    except (SettingsError, SecretRefusal, ValueError) as refusal:
        return Check("cluster", "FAIL", str(refusal)), None
    except SQLAlchemyError as exc:
        return Check("cluster", "FAIL", f"unreachable: {type(exc).__name__}"), None
    return Check("cluster", "ok", f"reachable; server {version}"), backend


def _check_control_plane(backend: PostgresBackend) -> Check:
    name = f"control plane {backend.control_database}"
    if not backend.database_exists(backend.control_database):
        return Check(name, "FAIL", "database missing; run `rheo migrate`")
    with backend.control_engine.connect() as connection:
        recorded = recorded_revisions(connection, CONTROL_CHAIN)
    ahead = recorded - known_revisions(CONTROL_CHAIN)
    if ahead:
        return Check(name, "FAIL", f"schema_ahead: records {sorted(ahead)}")
    if not recorded:
        return Check(name, "warn", "no revision recorded; run `rheo migrate`")
    return Check(name, "ok", f"at revision {', '.join(sorted(recorded))}")


def _check_connection_budget(backend: PostgresBackend) -> Check:
    """This process's worst-case connection count against the cluster's own ceiling.

    Three tiers, because the honest answer on a correct fresh install is not "ok":
    ``FAIL`` when this one process alone cannot fit, ``warn`` when the cluster cannot
    carry two, ``ok`` otherwise. Two is the number that matters — the core and the
    worker are separate processes, each holding its own engine cache and its own
    control engine against the same ``max_connections``.

    The detail always states the arithmetic and names all three levers, because the
    level on its own tells an operator nothing about which number to move.
    """
    name = "connection budget"
    pools = backend.pools
    try:
        ceiling = backend.max_connections()
    except SQLAlchemyError as exc:
        return Check(name, "FAIL", f"cannot read max_connections: {type(exc).__name__}")
    worst_case = pools.worst_case_connections
    detail = (
        f"{pools.cache_size} * {pools.pool_size} + {pools.reserved_connections} = "
        f"{worst_case} per process; 2 processes = {worst_case * 2}; "
        f"cluster max_connections = {ceiling} — raise max_connections, or lower "
        f"storage.pool_cache_size, or lower storage.pool_max_connections"
    )
    if worst_case > ceiling:
        return Check(name, "FAIL", detail)
    if worst_case * 2 > ceiling:
        return Check(name, "warn", detail)
    return Check(name, "ok", detail)


def _check_reconcile_interval() -> Check:
    """The due-work index's reconcile floor against the pool's idle window.

    AC 18's second half. ``tests/test_settings.py`` asserts the same relation on the
    *packaged* defaults; this reads the **resolved** settings, because both keys are
    deployment-scope and an operator overriding either through ``RHEO__work__…`` or
    ``RHEO__storage__…`` breaks a relation a defaults-only assertion would never see.

    ``warn`` rather than ``FAIL``: the deployment still works, it just stops reclaiming
    engines by idleness, which is a tuning fault and not a broken install. The detail
    names both keys and both current values either way, because the level on its own
    does not tell an operator which number to move.
    """
    name = "reconcile interval"
    try:
        settings = resolve()
        reconcile = settings.get_int(RECONCILE_KEY)
        idle_close = settings.get_int(IDLE_CLOSE_KEY)
    except SettingsError as refusal:
        # Only ``SettingsError``: ``resolve()`` has no secret-reference step, so the
        # ``SecretRefusal`` ``_check_settings`` also catches cannot reach here.
        return Check(name, "FAIL", f"{refusal.state}: {refusal.detail}")
    detail = f"{RECONCILE_KEY} = {reconcile}; {IDLE_CLOSE_KEY} = {idle_close}"
    if reconcile <= idle_close:
        return Check(
            name,
            "warn",
            f"{detail} — a reconcile pass re-touches every cached engine inside the "
            f"idle window, so nothing is reclaimed by idleness; raise "
            f"{RECONCILE_KEY} above {IDLE_CLOSE_KEY}",
        )
    return Check(name, "ok", detail)


def _check_workspaces(backend: PostgresBackend) -> Iterator[Check]:
    try:
        with backend.control_engine.connect() as connection:
            rows = list_workspaces(connection)
    except SQLAlchemyError as exc:
        yield Check("workspaces", "FAIL", f"cannot list: {type(exc).__name__}")
        return
    if not rows:
        yield Check("workspaces", "ok", "none")
        return
    for row in rows:
        level = "ok" if row.state is WorkspaceState.ACTIVE else "warn"
        detail = row.state.value
        if row.state_detail and row.state is not WorkspaceState.ACTIVE:
            detail += f" ({row.state_detail})"
        yield Check(f"workspace {row.id} ({row.slug})", level, detail)


def _check_extensions(backend: PostgresBackend) -> Iterator[Check]:
    try:
        report = backend.extension_report(EXTENSIONS)
    except SQLAlchemyError as exc:
        yield Check("extensions", "FAIL", f"cannot read catalogs: {type(exc).__name__}")
        return
    for extension in report.extensions:
        if extension.default_version is None:
            yield Check(
                f"extension {extension.name}",
                "warn",
                "not available on this cluster (phase 2 needs it installed)",
            )
            continue
        may_create = report.may_create(extension)
        how = (
            "superuser"
            if report.role_is_superuser
            else ("trusted + CREATE on the database" if may_create else "no")
        )
        yield Check(
            f"extension {extension.name}",
            "ok" if may_create else "warn",
            f"available {extension.default_version}; installed in maintenance db: "
            f"{extension.installed_version or 'no'}; trusted: "
            f"{'yes' if extension.trusted else 'no'}; role may CREATE EXTENSION in "
            f"{report.template_database}: {how}",
        )


def doctor(args: argparse.Namespace) -> int:
    checks: list[Check] = [_check_data_root(), _check_settings()]
    cluster, backend = _check_cluster()
    checks.append(cluster)
    if backend is not None:
        try:
            checks.append(_check_control_plane(backend))
        except (SQLAlchemyError, StorageRefusal) as exc:
            checks.append(
                Check("control plane", "FAIL", f"{type(exc).__name__}: {exc}")
            )
        checks.append(_check_connection_budget(backend))
        checks.append(_check_reconcile_interval())
        checks.extend(_check_workspaces(backend))
        checks.extend(_check_extensions(backend))
    for check in checks:
        print(check.line())
    return 1 if any(check.level == "FAIL" for check in checks) else 0
