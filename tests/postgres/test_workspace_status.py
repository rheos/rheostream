"""B8 (criterion 10): ``core.workspace.status`` through the registry.

Under an operator context from ``context_for_operator`` and under owner and member
contexts from ``context_for_harness``, the operation returns ``core_version`` equal
to the installed ``rheo-core`` distribution version, ``core_contract_version = 1``,
and ``modules = []``. A statement-capturing SQLAlchemy connection event on the
workspace engine asserts the handler reads ``core.workspace_composition``,
``core.module_state`` and ``core.module_schema_version``, and no table named
``alembic_version%``. The session-context half (C7a, 0b2) is this file's own
addition below: a third case, under a real ``context_from_session`` context.

**Nothing in this file may name a module distribution.** ``make absence-proof``
re-runs every phase-one demonstrator in a checkout with ``modules/recallatron``
deleted, and four of this file's cases are demonstrators, so a module-level
``import rheo_recallatron`` anywhere here fails *collection of the whole file* and
takes those four down with it — reported as ``ModuleNotFoundError`` rather than as
anything to do with the case that wanted the module. That is exactly what a
real-module version case written here cost: it now lives at
``tests/postgres/test_module_lifecycle.py::test_status_reports_the_latest_applied_schema_version_of_a_real_module``,
in a file the proof does not collect, because its subject needs a migration chain
with more than one revision and no fixture module has one.
``tests/test_module_import_graph.py`` holds this file out of its declared
Recallatron set and asserts the absence proof's import surface never names the
module, so the import coming back reds in the fast suite instead of only in CI.
"""

from importlib import metadata
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.registry import add_member, enable_harness_module
from rheo_contracts import CONTRACT_VERSION, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.factories import context_from_session
from rheo_core.operations import (
    WORKSPACE_STATUS,
    WorkspaceStatus,
    dispatch,
    register_core_operations,
)
from rheo_core.sessions import create_session, mint_host_secret
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import set_active_workspace
from sqlalchemy import Engine, event

_HOST = "shell.example.test"

pytestmark = pytest.mark.postgres

_COMPOSITION_TABLES = ("workspace_composition", "module_state", "module_schema_version")


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


def _capturing(engine: Engine) -> tuple[list[str], object]:
    captured: list[str] = []

    def before_cursor_execute(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        captured.append(statement)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    return captured, before_cursor_execute


def _status_under(
    cluster: ClusterSession, ctx: WorkspaceContext, workspace: UUID
) -> WorkspaceStatus:
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    captured, listener = _capturing(engine)
    try:
        outcome = dispatch(ctx, WORKSPACE_STATUS, {})
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert outcome.ok, outcome
    assert isinstance(outcome.result, WorkspaceStatus)
    reads = [s for s in captured if s.lstrip().upper().startswith("SELECT")]
    for table in _COMPOSITION_TABLES:
        assert any(f"core.{table}" in s for s in reads), (table, reads)
    assert not any("alembic_version" in s.lower() for s in captured), captured
    return outcome.result


def _assert_status(status: WorkspaceStatus) -> None:
    assert status.core_version == metadata.version("rheo-core")
    assert status.core_contract_version == CONTRACT_VERSION == 1
    assert status.modules == []


def test_status_under_an_operator_context(
    cluster: ClusterSession, workspace: UUID
) -> None:
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext)
    _assert_status(_status_under(cluster, ctx, workspace))


def test_status_under_owner_and_member_harness_contexts(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    owner = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(owner, WorkspaceContext)
    _assert_status(_status_under(cluster, owner, workspace))
    member_id = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="member-two"
    )
    member = context_for_harness(workspace, member_id, Role.MEMBER)
    assert isinstance(member, WorkspaceContext)
    _assert_status(_status_under(cluster, member, workspace))


def test_status_lists_an_enabled_module_with_no_applied_schema_step(
    cluster: ClusterSession, workspace: UUID
) -> None:
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext)
    assert ctx.enabled_modules == frozenset({"harness"})
    status = _status_under(cluster, ctx, workspace)
    assert [m.model_dump() for m in status.modules] == [
        {
            "module_id": "harness",
            "package_version": "0",
            "state": "enabled",
            "schema_version": None,
        }
    ]


def test_status_under_a_real_session_context(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """The session-context half (C7a): the same operation, driven by a real
    ``context_from_session`` context rather than the harness scaffolding above."""
    session_row = create_session(owner_account_id)
    secret = mint_host_secret(session_row.id, _HOST)
    with cluster.backend.control_engine.begin() as connection:
        set_active_workspace(connection, session_row.id, workspace)
    ctx = context_from_session(secret, _HOST)
    assert isinstance(ctx, WorkspaceContext)
    _assert_status(_status_under(cluster, ctx, workspace))


def test_status_ignores_extra_payload_keys(
    cluster: ClusterSession, workspace: UUID
) -> None:
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext)
    outcome = dispatch(ctx, WORKSPACE_STATUS, {"workspace_id": "not-consulted"})
    assert outcome.ok and isinstance(outcome.result, WorkspaceStatus)
    assert outcome.result.core_contract_version == 1
