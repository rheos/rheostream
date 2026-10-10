"""Explicit, audited operator upgrades for declared non-destructive revisions.

No automatic migrations, lifecycle activation, downgrade, or destructive approval
shortcut. A failed transaction leaves the prior schema/package intact; readiness
then refuses only the incompatible module until an operator retries or rolls back.
"""

from alembic.script import ScriptDirectory
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field
from rheo_contracts import CONTRACT_VERSION, WorkspaceContext
from sqlalchemy import text, update

from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
from rheo_core.migrations.module_chain import run_module_chain
from rheo_core.migrations.orchestrator import build_config, recorded_revisions
from rheo_core.modules.loader import loaded_manifests
from rheo_core.modules.readiness import (
    expected_heads,
    lock_module_execution,
    readiness_problem,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import get_workspace
from rheo_core.storage.core_tables import module_state
from rheo_core.storage.postgres import get_backend
from rheo_core.storage.provisioning import core_version
from rheo_core.storage.repositories import list_module_states, lock_module_state


class UpgradeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    module_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    target_version: str
    target_schema: str


class Upgraded(BaseModel):
    module_id: str
    package_version: str
    schema_version: str
    changed: bool


def upgrade(ctx: WorkspaceContext, uow: UnitOfWork, m: UpgradeInput) -> Upgraded:
    manifest = loaded_manifests().get(m.module_id)
    if manifest is None:
        raise OperationRefused("module_unavailable", "module is not loaded")
    heads = expected_heads(m.module_id)
    if manifest.package_version != m.target_version or heads != {m.target_schema}:
        raise OperationRefused(
            "upgrade_target_changed", "target differs from loaded code"
        )
    if CONTRACT_VERSION not in manifest.core_contract_versions:
        raise OperationRefused(
            "contract_version_mismatch", "incompatible core contract"
        )
    lock_workspace_lifecycle(uow.connection)
    lock_module_execution(uow.connection, upgrade=True)
    state = lock_module_state(uow.connection, module_id=m.module_id)
    if state is None or state.state not in {"installed", "enabled", "disabled"}:
        raise OperationRefused("module_state_invalid", "module is not installed")
    if Version(state.package_version) > Version(manifest.package_version):
        raise OperationRefused(
            "downgrade_refused", "restore the matching backup and image"
        )
    states = {s.module_id: s for s in list_module_states(uow.connection)}
    for dependency in manifest.dependencies:
        present = states.get(dependency.module_id)
        if present is None or present.state == "removed":
            if dependency.optional:
                continue
            raise OperationRefused("dependency_missing", "required dependency absent")
        if readiness_problem(uow.connection, present):
            raise OperationRefused("dependency_unavailable", "upgrade dependency first")
        if not SpecifierSet(dependency.version_range).contains(present.package_version):
            raise OperationRefused(
                "dependency_version", "installed dependency incompatible"
            )
    # Protect installed dependents too: upgrading a dependency must not strand them.
    for other in loaded_manifests().values():
        if other.module_id not in states or states[other.module_id].state == "removed":
            continue
        for dependency in other.dependencies:
            if dependency.module_id == m.module_id and not SpecifierSet(
                dependency.version_range
            ).contains(manifest.package_version):
                raise OperationRefused(
                    "dependency_version", "installed dependent incompatible"
                )
    extensions: set[str] = set(
        uow.connection.execute(text("SELECT extname FROM pg_extension")).scalars()
    )
    if set(manifest.storage.required_extensions) - extensions:
        raise OperationRefused(
            "extension_missing", "provision required extensions before upgrading"
        )
    before = recorded_revisions(uow.connection, m.module_id)
    script = ScriptDirectory.from_config(build_config(m.module_id))
    if not before or before - {r.revision for r in script.walk_revisions()}:
        raise OperationRefused("schema_unknown", "recorded schema is absent or unknown")
    pending = tuple(script.iterate_revisions(tuple(heads), tuple(before)))
    if any(
        (
            getattr(r.module, "non_destructive_upgrade", None) is not True
            or getattr(r.module, "destructive", False) is not False
        )
        for r in pending
    ):
        raise OperationRefused(
            "upgrade_requires_review",
            "pending migrations are not declared non-destructive; no migration ran",
        )
    changed = bool(pending) or state.package_version != manifest.package_version
    if changed:
        with get_backend().control_engine.connect() as conn:
            workspace = get_workspace(conn, ctx.workspace_id)
        if workspace is None:
            raise OperationRefused("workspace_unavailable", "workspace is unavailable")
        run_module_chain(
            uow.connection,
            manifest,
            expected_database=workspace.database_name,
            core_version=core_version(),
        )
        for check in manifest.health_checks:
            check(uow)
        uow.connection.execute(
            update(module_state)
            .where(module_state.c.module_id == m.module_id)
            .values(package_version=manifest.package_version)
        )
    return Upgraded(
        module_id=m.module_id,
        package_version=manifest.package_version,
        schema_version=m.target_schema,
        changed=changed,
    )
