"""Read-only compatibility checks against the module code loaded by this process."""

from alembic.script import ScriptDirectory
from packaging.specifiers import SpecifierSet
from sqlalchemy import Connection, text

from rheo_core.migrations.orchestrator import build_config, recorded_revisions
from rheo_core.modules.loader import loaded_manifests
from rheo_core.operations.refusals import OperationRefused
from rheo_core.storage.repositories import ModuleStateRow, list_module_states

# RHEOMODL: distinct from lifecycle/migration locks. Try-locks avoid a lock-order
# cycle when a handler holds the lifecycle lock and opens an export snapshot on a
# second connection. Normal executions can share this transaction-scoped lock.
MODULE_EXECUTION_LOCK = 0x5248454F4D4F444C


def lock_module_execution(conn: Connection, *, upgrade: bool = False) -> None:
    function = (
        "pg_try_advisory_xact_lock" if upgrade else "pg_try_advisory_xact_lock_shared"
    )
    acquired: bool = conn.execute(
        text(f"SELECT {function}(:key)"), {"key": MODULE_EXECUTION_LOCK}
    ).scalar_one()
    if not acquired:
        raise OperationRefused(
            "upgrade_busy" if upgrade else "module_unavailable",
            "module execution or upgrade is in progress; retry after it finishes",
        )


def expected_heads(module_id: str) -> frozenset[str]:
    return frozenset(ScriptDirectory.from_config(build_config(module_id)).get_heads())


def readiness_problem(
    conn: Connection,
    state: ModuleStateRow,
    *,
    _seen: frozenset[str] = frozenset(),
    _states: dict[str, ModuleStateRow] | None = None,
) -> str | None:
    """A mismatch is projected as unavailable; lifecycle state is never overwritten."""
    if state.module_id in _seen:
        return None
    seen = _seen | {state.module_id}
    manifest = loaded_manifests().get(state.module_id)
    if manifest is None:
        return "module_not_loaded"
    if state.package_version != manifest.package_version:
        return "package_version_mismatch"
    if recorded_revisions(conn, state.module_id) != expected_heads(state.module_id):
        return "schema_version_mismatch"
    states = (
        _states
        if _states is not None
        else {row.module_id: row for row in list_module_states(conn)}
    )
    for dependency in manifest.dependencies:
        present = states.get(dependency.module_id)
        if present is None or present.state == "removed":
            if dependency.optional:
                continue
            return "dependency_unavailable"
        if not SpecifierSet(dependency.version_range).contains(
            present.package_version
        ) or readiness_problem(conn, present, _seen=seen, _states=states):
            return "dependency_unavailable"
    return None


def require_ready(conn: Connection, module_id: str) -> None:
    # Non-module core/harness registrations retain their existing authorization.
    if module_id not in loaded_manifests():
        return
    lock_module_execution(conn)
    state = next(
        (s for s in list_module_states(conn) if s.module_id == module_id), None
    )
    if state is None or readiness_problem(conn, state) is not None:
        raise OperationRefused(
            "module_unavailable", "module upgrade or matching deployment required"
        )
