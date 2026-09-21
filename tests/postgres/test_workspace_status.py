"""B8 (criterion 10): ``core.workspace.status`` through the registry.

Under an operator context from ``context_for_operator`` and under owner and member
contexts from ``context_for_harness``, the operation returns ``core_version`` equal
to the installed ``rheo-core`` distribution version, ``core_contract_version = 1``,
and ``modules = []``. A statement-capturing SQLAlchemy connection event on the
workspace engine asserts the handler reads ``core.workspace_composition``,
``core.module_state`` and ``core.module_schema_version``, and no table named
``alembic_version%``. The session-context half (C7a, 0b2) is this file's own
addition below: a third case, under a real ``context_from_session`` context.
"""

from importlib import metadata
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import (
    chain_head,
    install_and_enable_module,
    loaded_probe_modules,
)
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
from rheo_core.storage.repositories import list_module_schema_versions
from rheo_recallatron import MANIFEST as RECALLATRON_MANIFEST
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


def test_status_reports_the_latest_applied_schema_version_of_a_real_module(
    monkeypatch: pytest.MonkeyPatch, cluster: ClusterSession, workspace: UUID
) -> None:
    """AC 1's version half, over a chain with more than one revision.

    ``test_status_lists_an_enabled_module_with_no_applied_schema_step`` above pins the
    null case, and ``tests/postgres/test_module_lifecycle.py`` pins the two operations
    that get a workspace from ``absent`` to a reported module. What neither could pin
    until now is ``_workspace_status``'s **latest wins** rule: it folds every
    ``core.module_schema_version`` row for a module into one reported value, and while
    Recallatron's chain had a single revision the fold had nothing to choose between —
    first, last and only were the same string, so an implementation reporting the
    *earliest* applied step passed.

    With ``0002_memory_records`` the workspace records two steps and the two answers
    differ, so this asserts the reported version is the chain's **head** while the
    recorded history holds strictly more than that one row.

    Both values are derived: the package version off the manifest (an installed
    distribution's own metadata, so a status reporting a literal reds), the schema
    version off the chain's scripts. A literal here would be right today and silently
    wrong at the next revision.
    """
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext)
    assert _status_under(cluster, ctx, workspace).modules == []

    with loaded_probe_modules(monkeypatch, RECALLATRON_MANIFEST.module_id):
        head = chain_head(RECALLATRON_MANIFEST)
        install_and_enable_module(
            cluster.backend, ctx, workspace, RECALLATRON_MANIFEST.module_id
        )

    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with engine.connect() as connection:
        applied = [
            version.schema_version
            for version in list_module_schema_versions(connection)
            if version.module_id == RECALLATRON_MANIFEST.module_id
        ]
    assert len(applied) > 1, applied
    assert applied[-1] == head

    after = context_for_operator(workspace)
    assert isinstance(after, WorkspaceContext)
    assert [
        module.model_dump()
        for module in _status_under(cluster, after, workspace).modules
    ] == [
        {
            "module_id": RECALLATRON_MANIFEST.module_id,
            "package_version": RECALLATRON_MANIFEST.package_version,
            "state": "enabled",
            "schema_version": head,
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
