"""Workspace provisioning: the state machine of ``storage-and-workspaces.md``
§ Provisioning, invoked in this run by the operator CLI (``rheo workspace create
--owner``, ``rheo workspace repair``) and never by a registered operation
(``core.workspace.create`` as a long-running operation is 0c/phase work).

``PROVISION_STEPS`` is the explicit ordered table of the five step names the machine
walks by name:

1. ``insert_registry_row`` — the ``workspace`` row in state ``provisioning`` with the
   derived ``database_name``, and the owner ``membership`` row, in one transaction.
   The membership is written here and nowhere else.
2. ``create_database`` — ``CREATE DATABASE <name> TEMPLATE <storage.template_database>``
   on the autocommit maintenance connection; "already exists" with a row still in
   ``provisioning`` is a retry and continues.
3. ``migrate_core`` — the ``core`` chain through the orchestrator (which creates the
   ``core`` schema under its lock), then ``workspace_composition`` with the installed
   ``rheo-core`` distribution version and ``CONTRACT_VERSION``.
4. ``write_default_settings`` — the ``explicit_per_workspace`` rows from the key
   registry's package defaults, inserted only where absent.
5. ``activate`` — the row goes ``active``.

``database_name`` is ``ws_`` + the workspace UUID's 32 hex digits, derived here and
stored in ``workspace.database_name``, never accepted from any input.
``state_detail`` records the last completed step name, so ``repair`` resumes after it.
Each step is idempotent. ``provision`` and ``repair`` are the state machine's only two
entry points; the orchestrator additionally marks a workspace ``unavailable`` when its
chain fails, and ``repair`` brings such a workspace back by re-running from step 3.

**Fault-injection seam (B16).** ``after_step(step_name)`` is called after each step
commits. A non-``None`` ``after_step`` is accepted only under ``profile = test``; any
other profile raises ``ValueError`` at the call site, so production code cannot pass
it. Tests raise from the callback, assert ``state_detail`` names the step, and call
``repair``; no private function of this module is monkeypatched anywhere.
"""

import re
from collections.abc import Callable
from importlib import metadata
from typing import Final
from uuid import UUID

from rheo_contracts import CONTRACT_VERSION, Role

from rheo_core.migrations.orchestrator import CORE_CHAIN, run_chain
from rheo_core.settings import REGISTRY, current_profile, encode_text
from rheo_core.storage import control_plane, repositories
from rheo_core.storage.backend import (
    WORKSPACE_EXISTS,
    WORKSPACE_MISSING,
    WORKSPACE_STATE,
    StorageRefusal,
    UnitOfWork,
)
from rheo_core.storage.control_plane import WorkspaceRow
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.postgres import PostgresBackend, get_backend

STEP_INSERT_REGISTRY_ROW: Final = "insert_registry_row"
STEP_CREATE_DATABASE: Final = "create_database"
STEP_MIGRATE_CORE: Final = "migrate_core"
STEP_WRITE_DEFAULT_SETTINGS: Final = "write_default_settings"
STEP_ACTIVATE: Final = "activate"

PROVISION_STEPS: Final[tuple[str, ...]] = (
    STEP_INSERT_REGISTRY_ROW,
    STEP_CREATE_DATABASE,
    STEP_MIGRATE_CORE,
    STEP_WRITE_DEFAULT_SETTINGS,
    STEP_ACTIVATE,
)

DATABASE_NAME_PREFIX: Final = "ws_"
CORE_DISTRIBUTION: Final = "rheo-core"
_SLUG: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62})")

AfterStep = Callable[[str], None]


def database_name_for(workspace_id: UUID) -> str:
    """``ws_`` + the UUID's 32 hex digits. Pure; the only source of that name."""
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a UUID")
    return f"{DATABASE_NAME_PREFIX}{workspace_id.hex}"


def core_version() -> str:
    """The installed ``rheo-core`` distribution version, read from metadata."""
    return metadata.version(CORE_DISTRIBUTION)


def _slug_for(workspace_id: UUID, slug: str | None) -> str:
    if slug is None:
        return str(workspace_id)
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError(
            "a slug is lowercase letters, digits and hyphens, at most 63 characters"
        )
    return slug


def _check_after_step(after_step: AfterStep | None) -> None:
    if after_step is None:
        return
    if not callable(after_step):
        raise TypeError("after_step must be callable")
    profile = current_profile()
    if profile != "test":
        raise ValueError(
            "after_step is the test-profile fault-injection seam; the resolved "
            f"profile is {profile!r}"
        )


def _registry_row(backend: PostgresBackend, workspace_id: UUID) -> WorkspaceRow:
    with backend.control_engine.connect() as connection:
        row = control_plane.get_workspace(connection, workspace_id)
    if row is None:
        raise StorageRefusal(WORKSPACE_MISSING, f"workspace {workspace_id} has no row")
    return row


_RECORD_STEP_EXPECTED_STATES: Final = frozenset(
    {WorkspaceState.PROVISIONING, WorkspaceState.UNAVAILABLE}
)
_ACTIVATE_EXPECTED_STATES: Final = frozenset({WorkspaceState.PROVISIONING})


def _record_step(backend: PostgresBackend, workspace_id: UUID, step: str) -> None:
    """Commit ``state_detail = step`` (state stays ``provisioning``).

    Guarded by ``expected_states`` (issue #23): the normal walk finds the row
    ``provisioning``, and ``repair()`` resuming from ``unavailable`` (``:314-315``)
    finds it there instead — both proceed. A lagging walker that finds the row
    already ``active`` (another walker got there first) is refused
    ``workspace_state`` rather than silently stamping ``provisioning`` back over it.
    """
    with backend.control_engine.begin() as connection:
        control_plane.set_workspace_state(
            connection,
            workspace_id,
            state=WorkspaceState.PROVISIONING,
            state_detail=step,
            expected_states=_RECORD_STEP_EXPECTED_STATES,
        )


def _step_insert_registry_row(
    backend: PostgresBackend,
    workspace_id: UUID,
    *,
    owner_account_id: UUID | None,
    slug: str | None,
) -> None:
    if owner_account_id is None or slug is None:
        raise ValueError("step 1 needs the owner account and the slug")
    with backend.control_engine.begin() as connection:
        row, inserted = control_plane.insert_workspace_if_absent(
            connection,
            workspace_id=workspace_id,
            slug=slug,
            display_name=slug,
            database_name=database_name_for(workspace_id),
            state_detail=STEP_INSERT_REGISTRY_ROW,
        )
        if row.state is not WorkspaceState.PROVISIONING:
            raise StorageRefusal(
                WORKSPACE_EXISTS,
                f"workspace {workspace_id} already exists in state {row.state.value}",
            )
        if inserted:
            control_plane.insert_membership_if_absent(
                connection,
                account_id=owner_account_id,
                workspace_id=workspace_id,
                role=Role.OWNER,
            )
            return
        # A retry of a crashed create: the owner row was committed with the
        # workspace row, so the caller must be that owner. Inserting here instead
        # would let a retry with a different account add a second owner.
        existing = control_plane.get_membership(
            connection, account_id=owner_account_id, workspace_id=workspace_id
        )
        if existing is None or existing.role is not Role.OWNER:
            raise StorageRefusal(
                WORKSPACE_EXISTS,
                f"workspace {workspace_id} is being provisioned for a different owner",
            )


def _step_create_database(backend: PostgresBackend, workspace_id: UUID) -> None:
    row = _registry_row(backend, workspace_id)
    created = backend.ensure_database(
        row.database_name, template=backend.template_database
    )
    if not created:
        # Already exists: a retry only while the row is still provisioning.
        if (
            _registry_row(backend, workspace_id).state
            is not WorkspaceState.PROVISIONING
        ):
            raise StorageRefusal(
                WORKSPACE_EXISTS,
                f"database {row.database_name!r} exists and the workspace is not "
                "being provisioned",
            )
    _record_step(backend, workspace_id, STEP_CREATE_DATABASE)


def _step_migrate_core(backend: PostgresBackend, workspace_id: UUID) -> None:
    row = _registry_row(backend, workspace_id)
    engine = backend.pools.engine_for(row.database_name, pin=True)
    with UnitOfWork(engine, row.database_name, pool=backend.pools) as uow:
        # The orchestrator creates the ``core`` schema itself, under its lock.
        run_chain(uow.connection, CORE_CHAIN, expected_database=row.database_name)
        repositories.write_composition(
            uow.connection,
            core_version=core_version(),
            core_contract_version=CONTRACT_VERSION,
        )
        uow.commit()
    _record_step(backend, workspace_id, STEP_MIGRATE_CORE)


def _step_write_default_settings(backend: PostgresBackend, workspace_id: UUID) -> None:
    row = _registry_row(backend, workspace_id)
    engine = backend.pools.engine_for(row.database_name, pin=True)
    with UnitOfWork(engine, row.database_name, pool=backend.pools) as uow:
        for spec in REGISTRY.explicit_per_workspace():
            repositories.insert_workspace_setting_if_absent(
                uow.connection,
                key=spec.key,
                value=encode_text(spec, spec.default),
                value_type=spec.type,
                updated_by=None,
            )
        uow.commit()
    _record_step(backend, workspace_id, STEP_WRITE_DEFAULT_SETTINGS)


def _step_activate(backend: PostgresBackend, workspace_id: UUID) -> None:
    """By the time either path reaches here, the preceding ``_record_step`` has
    already moved the row to ``provisioning`` (issue #23): one expected state
    covers the normal walk and a repair alike."""
    with backend.control_engine.begin() as connection:
        control_plane.set_workspace_state(
            connection,
            workspace_id,
            state=WorkspaceState.ACTIVE,
            state_detail=STEP_ACTIVATE,
            expected_states=_ACTIVATE_EXPECTED_STATES,
        )


def _walk(
    backend: PostgresBackend,
    workspace_id: UUID,
    steps: tuple[str, ...],
    *,
    after_step: AfterStep | None,
    owner_account_id: UUID | None,
    slug: str | None,
) -> None:
    for step in steps:
        if step == STEP_INSERT_REGISTRY_ROW:
            _step_insert_registry_row(
                backend, workspace_id, owner_account_id=owner_account_id, slug=slug
            )
        elif step == STEP_CREATE_DATABASE:
            _step_create_database(backend, workspace_id)
        elif step == STEP_MIGRATE_CORE:
            _step_migrate_core(backend, workspace_id)
        elif step == STEP_WRITE_DEFAULT_SETTINGS:
            _step_write_default_settings(backend, workspace_id)
        elif step == STEP_ACTIVATE:
            _step_activate(backend, workspace_id)
        else:  # pragma: no cover - PROVISION_STEPS is the closed table
            raise ValueError(f"unknown provisioning step {step!r}")
        if after_step is not None:
            after_step(step)


def provision(
    workspace_id: UUID,
    *,
    owner_account_id: UUID,
    slug: str | None = None,
    after_step: AfterStep | None = None,
) -> None:
    """Walk all five steps for a new workspace (or retry one still provisioning).

    ``workspace_exists`` when the id is already a workspace past provisioning;
    ``account_missing`` when the owner does not exist; ``slug_taken`` when the slug
    belongs to another workspace. ``after_step`` is test-profile only.
    """
    if not isinstance(workspace_id, UUID) or not isinstance(owner_account_id, UUID):
        raise TypeError("workspace_id and owner_account_id must be UUIDs")
    _check_after_step(after_step)
    _walk(
        get_backend(),
        workspace_id,
        PROVISION_STEPS,
        after_step=after_step,
        owner_account_id=owner_account_id,
        slug=_slug_for(workspace_id, slug),
    )


def _remaining_after(state_detail: str | None) -> tuple[str, ...]:
    """The steps after the last completed one; a row exists, so step 1 is done."""
    if state_detail in PROVISION_STEPS:
        return PROVISION_STEPS[PROVISION_STEPS.index(state_detail) + 1 :]
    return PROVISION_STEPS[1:]


def repair(workspace_id: UUID) -> None:
    """Resume a workspace from ``state_detail`` to ``active``.

    ``active`` is a no-op. ``provisioning`` resumes after the recorded step.
    ``unavailable`` (a failed or ahead-of-code chain) re-runs from step 3, since the
    row and the database both exist. ``migrating`` and ``restoring`` are refused as
    ``workspace_state``; an unknown id is ``workspace_missing``.
    """
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a UUID")
    backend = get_backend()
    row = _registry_row(backend, workspace_id)
    if row.state is WorkspaceState.ACTIVE:
        return
    if row.state is WorkspaceState.PROVISIONING:
        remaining = _remaining_after(row.state_detail)
    elif row.state is WorkspaceState.UNAVAILABLE:
        remaining = PROVISION_STEPS[PROVISION_STEPS.index(STEP_MIGRATE_CORE) :]
    else:
        raise StorageRefusal(
            WORKSPACE_STATE,
            f"workspace {workspace_id} is {row.state.value}; repair resumes only "
            "provisioning or unavailable workspaces",
            workspace_state=row.state.value,
        )
    _walk(
        backend,
        workspace_id,
        remaining,
        after_step=None,
        owner_account_id=None,
        slug=None,
    )
