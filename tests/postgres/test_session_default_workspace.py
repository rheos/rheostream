"""Issue #157: a new session starts in the account's default workspace.

Before this fix every session was created with ``active_workspace_id = NULL``, so a
fresh web sign-in refused ``workspace_unselected`` on every request and the shell's
switcher (which only renders for a signed-in session) could never be reached.

``create_session`` now picks, in the same transaction as the insert: the workspace
an earlier session of the account was most recently used in, if the membership still
exists and the workspace is ``active``; else the oldest membership whose workspace is
``active``; else nothing, and the session stays unselected.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from rheo_contracts import WorkspaceContext
from rheo_core.boundary.context import WORKSPACE_UNSELECTED, Refusal
from rheo_core.boundary.factories import context_from_session
from rheo_core.sessions import create_session, mint_host_secret, switch_workspace
from rheo_core.storage import control_tables
from rheo_core.storage.control_plane import (
    get_session,
    insert_account,
    set_workspace_state,
    touch_session,
)
from rheo_core.storage.control_tables import WorkspaceState
from sqlalchemy import delete

pytestmark = pytest.mark.postgres

HOST = "shell.example.test"


def _active_workspace(cluster: ClusterSession, session_id: UUID) -> UUID | None:
    with cluster.backend.control_engine.connect() as connection:
        row = get_session(connection, session_id)
    assert row is not None
    return row.active_workspace_id


def _last_used_at(cluster: ClusterSession, session_id: UUID, at: datetime) -> None:
    """Pin a session's ``last_seen_at`` so "most recently used" is unambiguous."""
    with cluster.backend.control_engine.begin() as connection:
        touch_session(
            connection, session_id, last_seen_at=at, expires_at=at + timedelta(days=1)
        )


def _mark_unavailable(cluster: ClusterSession, workspace_id: UUID) -> None:
    with cluster.backend.control_engine.begin() as connection:
        set_workspace_state(
            connection,
            workspace_id,
            state=WorkspaceState.UNAVAILABLE,
            state_detail="marked unavailable by the test",
        )


def _remove_membership(
    cluster: ClusterSession, account_id: UUID, workspace_id: UUID
) -> None:
    """Test setup only: there is no membership-removal repository yet."""
    membership = control_tables.membership
    with cluster.backend.control_engine.begin() as connection:
        connection.execute(
            delete(membership).where(
                membership.c.account_id == account_id,
                membership.c.workspace_id == workspace_id,
            )
        )


def _used_in(
    cluster: ClusterSession, account_id: UUID, workspace_id: UUID, at: datetime
) -> UUID:
    """An earlier session of ``account_id``, pointed at ``workspace_id`` and last
    seen at ``at``."""
    row = create_session(account_id)
    assert switch_workspace(row.id, workspace_id) is None
    _last_used_at(cluster, row.id, at)
    return row.id


def test_single_membership_is_selected_and_the_session_builds_a_context(
    cluster: ClusterSession, owner_account_id: UUID, workspace: UUID
) -> None:
    row = create_session(owner_account_id)
    assert row.active_workspace_id == workspace
    assert row.active_workspace_changed_at is not None
    assert _active_workspace(cluster, row.id) == workspace
    ctx = context_from_session(mint_host_secret(row.id, HOST), HOST)
    assert isinstance(ctx, WorkspaceContext)
    assert ctx.workspace_id == workspace


def test_multiple_memberships_with_no_history_pick_the_oldest(
    cluster: ClusterSession,
    owner_account_id: UUID,
    workspace: UUID,
    make_workspace: MakeWorkspace,
) -> None:
    make_workspace()
    assert create_session(owner_account_id).active_workspace_id == workspace


def test_multiple_memberships_last_used_wins(
    cluster: ClusterSession,
    owner_account_id: UUID,
    workspace: UUID,
    make_workspace: MakeWorkspace,
) -> None:
    second = make_workspace()
    now = datetime.now(UTC)
    _used_in(cluster, owner_account_id, workspace, now - timedelta(hours=2))
    _used_in(cluster, owner_account_id, second, now - timedelta(hours=1))
    assert create_session(owner_account_id).active_workspace_id == second


def test_last_used_is_by_last_seen_not_by_creation(
    cluster: ClusterSession,
    owner_account_id: UUID,
    workspace: UUID,
    make_workspace: MakeWorkspace,
) -> None:
    second = make_workspace()
    now = datetime.now(UTC)
    older = _used_in(cluster, owner_account_id, workspace, now - timedelta(hours=2))
    _used_in(cluster, owner_account_id, second, now - timedelta(hours=1))
    # The first session, created earlier, was the one used most recently.
    _last_used_at(cluster, older, now - timedelta(minutes=5))
    assert create_session(owner_account_id).active_workspace_id == workspace


def test_a_membership_removed_since_last_use_is_skipped(
    cluster: ClusterSession,
    owner_account_id: UUID,
    workspace: UUID,
    make_workspace: MakeWorkspace,
) -> None:
    second = make_workspace()
    third = make_workspace()
    now = datetime.now(UTC)
    _used_in(cluster, owner_account_id, second, now - timedelta(hours=2))
    _used_in(cluster, owner_account_id, third, now - timedelta(hours=1))
    _remove_membership(cluster, owner_account_id, third)
    # The next most recent one still qualifies, ahead of the oldest membership.
    assert create_session(owner_account_id).active_workspace_id == second
    _remove_membership(cluster, owner_account_id, second)
    assert create_session(owner_account_id).active_workspace_id == workspace


def test_a_workspace_gone_unavailable_is_skipped(
    cluster: ClusterSession,
    owner_account_id: UUID,
    workspace: UUID,
    make_workspace: MakeWorkspace,
) -> None:
    second = make_workspace()
    now = datetime.now(UTC)
    _used_in(cluster, owner_account_id, second, now - timedelta(hours=1))
    _mark_unavailable(cluster, second)
    assert create_session(owner_account_id).active_workspace_id == workspace


def test_the_oldest_membership_is_skipped_when_its_workspace_is_unavailable(
    cluster: ClusterSession,
    owner_account_id: UUID,
    workspace: UUID,
    make_workspace: MakeWorkspace,
) -> None:
    second = make_workspace()
    _mark_unavailable(cluster, workspace)
    assert create_session(owner_account_id).active_workspace_id == second


def test_memberships_with_no_active_workspace_stay_unselected(
    cluster: ClusterSession, owner_account_id: UUID, workspace: UUID
) -> None:
    now = datetime.now(UTC)
    _used_in(cluster, owner_account_id, workspace, now - timedelta(hours=1))
    _mark_unavailable(cluster, workspace)
    assert create_session(owner_account_id).active_workspace_id is None


def test_zero_memberships_stay_unselected(cluster: ClusterSession) -> None:
    with cluster.backend.control_engine.begin() as connection:
        account_id = insert_account(connection, display_name="no-workspace-yet").id
    row = create_session(account_id)
    assert row.active_workspace_id is None
    assert row.active_workspace_changed_at is None
    assert _active_workspace(cluster, row.id) is None
    refusal = context_from_session(mint_host_secret(row.id, HOST), HOST)
    assert isinstance(refusal, Refusal)
    assert refusal.state == WORKSPACE_UNSELECTED


def test_another_accounts_history_is_never_used(
    cluster: ClusterSession,
    owner_account_id: UUID,
    workspace: UUID,
    make_workspace: MakeWorkspace,
) -> None:
    """The last-used lookup is scoped to the signing-in account: another account's
    session in a workspace this one is not a member of never leaks in."""
    with cluster.backend.control_engine.begin() as connection:
        other = insert_account(connection, display_name="other-owner").id
    others_workspace = make_workspace(owner=other)
    _used_in(cluster, other, others_workspace, datetime.now(UTC))
    assert create_session(owner_account_id).active_workspace_id == workspace
