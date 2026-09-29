"""``rheo doctor``: the allowed modules' settings registration, data-root validity,
cluster reachability, the control-plane head,
the connection budget, the reconcile interval, per-workspace state, per-workspace
evidence acceptance failures, and the ``CREATE EXTENSION`` privilege report for
``vector`` and ``pg_trgm``.

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
from datetime import UTC, datetime, timedelta
from typing import Final

from rheo_core.evidence.health import SettlementCounts, settlement_counts_since
from rheo_core.migrations.orchestrator import (
    CONTROL_CHAIN,
    known_revisions,
    recorded_revisions,
)
from rheo_core.modules import ManifestInvalid, register_module_settings
from rheo_core.secrets import SecretRefusal, check_env_references
from rheo_core.settings import PROFILE_KEY, SettingsError, resolve
from rheo_core.storage.backend import StorageRefusal
from rheo_core.storage.control_plane import WorkspaceRow, list_workspaces
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.data_root import DataRootRefusal, resolve_data_root
from rheo_core.storage.postgres import PostgresBackend, get_backend
from sqlalchemy.exc import SQLAlchemyError

from rheo_app_cli.context import data_root

EXTENSIONS: Final = ("vector", "pg_trgm")
RECONCILE_KEY: Final = "work.due_reconcile_seconds"
IDLE_CLOSE_KEY: Final = "storage.pool_idle_close_seconds"
CONFIGURED_PROCESSES: Final = 2
"""The long-running processes that each hold a full engine budget: core and worker."""
BUDGET_HEADROOM_PERCENT: Final = 80
"""The share of ``max_connections`` the long-running processes may claim for ``ok``.

The rest is for connections the two-process figure does not count: an operator's
``rheo`` command (this one included, and a migration or import run) is a third
process with its own engines, ``psql`` sessions, and the superuser-reserved slots."""
ACCEPTANCE_WINDOW: Final = timedelta(hours=24)
"""How far back the evidence-acceptance check counts settlements (#221)."""
ACCEPTANCE_FAILED_FAIL_AT: Final = 5
"""``gap/acceptance_failed`` settlements in one workspace, within
:data:`ACCEPTANCE_WINDOW`, at which the check fails rather than warns: one full run of
five attempts per unit, five units lost in a day."""


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


def _check_module_settings() -> Check:
    """The early module settings registration (#108), as ``doctor``'s first check.

    ``run_command`` skips its own registration for ``doctor``
    (``SELF_REGISTERING_COMMANDS``), so this runs it instead, ahead of every check
    that resolves settings in full: an allowed module's ``RHEO__<module>__*``
    variables are then declared keys, not strays. A refusal becomes this check's
    ``FAIL`` line with the same state and detail ``run_command`` prints for every
    other command (``main.register_allowed_module_settings``), and the report goes
    on. ``Exception`` because importing an entry point runs that module's own import
    code, which can raise anything.
    """
    name = "module settings"
    try:
        registered = register_module_settings()
    except SettingsError as refusal:
        return Check(name, "FAIL", f"{refusal.state}: {refusal.detail}")
    except ManifestInvalid as invalid:
        return Check(name, "FAIL", f"module_invalid: {invalid}")
    except Exception as failure:
        return Check(
            name, "FAIL", f"module_load_failed: {type(failure).__name__}: {failure}"
        )
    return Check(name, "ok", f"registered: {', '.join(registered) or 'none'}")


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
    """This process's pooled connection count against the cluster's own ceiling.

    Three tiers: ``FAIL`` when this one process alone cannot fit, ``warn`` when two
    of them take more than :data:`BUDGET_HEADROOM_PERCENT` of the cluster, ``ok``
    otherwise. Two is the number that matters — the core and the worker are separate
    processes, each holding its own engine cache and its own control engine against
    the same ``max_connections`` — and the headroom is for the operator commands and
    sessions that come and go beside them (issue #162).

    The detail always states the arithmetic and names all three levers, because the
    level on its own tells an operator nothing about which number to move.

    The reservation includes the control engine and one serialized maintenance
    connection (issue #62). Busy engines are not evicted, so ``held now`` can
    briefly exceed the configured budget until those connections return.
    """
    name = "connection budget"
    pools = backend.pools
    try:
        ceiling = backend.max_connections()
    except SQLAlchemyError as exc:
        return Check(name, "FAIL", f"cannot read max_connections: {type(exc).__name__}")
    pooled = pools.pooled_connections
    held = pools.held_connections
    process = max(pooled, held)
    target = ceiling * BUDGET_HEADROOM_PERCENT // 100
    configured = pooled * CONFIGURED_PROCESSES
    effective = process * CONFIGURED_PROCESSES
    detail = (
        f"{pools.cache_size} * {pools.pool_size} + {pools.reserved_connections} = "
        f"{pooled} per process from the engine pools (control plus one serialized "
        f"maintenance connection); {CONFIGURED_PROCESSES} processes configured = "
        f"{configured}{'' if effective == configured else f', effective {effective}'}; "
        f"cluster max_connections = {ceiling}, target <= {target} "
        f"({BUDGET_HEADROOM_PERCENT}%, the rest for operator commands); "
        f"held now = {held}. "
        f"Busy engines are not evicted, so the cache may briefly exceed "
        f"cache_size until those connections return. Levers: raise "
        f"max_connections, or lower storage.pool_cache_size, or lower "
        f"storage.pool_max_connections"
    )
    if process > ceiling:
        return Check(name, "FAIL", detail)
    if effective > target:
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


def _acceptance_check(name: str, counts: SettlementCounts | None) -> Check:
    """The level for one workspace's settlement counts (#221).

    ``FAIL`` at :data:`ACCEPTANCE_FAILED_FAIL_AT` failures, or when every unit settled
    in the window failed (the quiet-workspace case: a batch of one, lost); ``warn`` at
    one or more; ``ok`` otherwise. No table is ``ok`` and says so. Counts only: the
    detail never carries evidence text.
    """
    hours = int(ACCEPTANCE_WINDOW.total_seconds() // 3600)
    if counts is None:
        return Check(name, "ok", "not applicable: no evidence table in this workspace")
    failed, settled = counts.acceptance_failed, counts.settled
    detail = (
        f"{failed} of {settled} evidence units settled in the last {hours} h ended "
        f"gap/acceptance_failed (fail at {ACCEPTANCE_FAILED_FAIL_AT} or when all do)"
    )
    if failed == 0:
        return Check(name, "ok", detail)
    if failed >= ACCEPTANCE_FAILED_FAIL_AT or failed == settled:
        return Check(name, "FAIL", f"{detail}; the evidence of each is lost")
    return Check(name, "warn", f"{detail}; the evidence of each is lost")


def _check_workspace_acceptance(
    backend: PostgresBackend, row: WorkspaceRow, *, now: datetime
) -> Check:
    """One active workspace's ``gap/acceptance_failed`` settlements in the window.

    A drain that contains an acceptance fault succeeds, so this is where a unit lost
    to one shows up (#221). Read-only, on the workspace's own database.
    """
    name = f"evidence acceptance {row.id} ({row.slug})"
    try:
        with backend.pools.acquire(row.database_name) as engine:
            with engine.connect() as connection:
                counts = settlement_counts_since(
                    connection, since=now - ACCEPTANCE_WINDOW
                )
    except SQLAlchemyError as exc:
        return Check(name, "FAIL", f"cannot read: {type(exc).__name__}")
    return _acceptance_check(name, counts)


def _check_evidence_acceptance(
    backend: PostgresBackend, *, now: datetime | None = None
) -> Iterator[Check]:
    """:func:`_check_workspace_acceptance` for every active workspace; one ``ok``
    line when there is none. Other states are skipped: their database may be absent
    or half-built, and the workspace-state line already reports them."""
    at = datetime.now(UTC) if now is None else now
    try:
        with backend.control_engine.connect() as connection:
            rows = list_workspaces(connection, state=WorkspaceState.ACTIVE)
    except SQLAlchemyError as exc:
        yield Check("evidence acceptance", "FAIL", f"cannot list: {type(exc).__name__}")
        return
    if not rows:
        yield Check("evidence acceptance", "ok", "not applicable: no active workspace")
        return
    for row in rows:
        yield _check_workspace_acceptance(backend, row, now=at)


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
    # Registration first: every later check that resolves settings needs it (#108).
    checks: list[Check] = [
        _check_module_settings(),
        _check_data_root(),
        _check_settings(),
    ]
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
        checks.extend(_check_evidence_acceptance(backend))
        checks.extend(_check_extensions(backend))
    for check in checks:
        print(check.line())
    return 1 if any(check.level == "FAIL" for check in checks) else 0
