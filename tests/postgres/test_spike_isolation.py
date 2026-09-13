"""AC 7's storage half: a note written in workspace A is invisible in workspace B,
and a reference minted in A resolves ``not_found`` in B.

Seams under test: ``spike.note.list`` through ``dispatch()`` against two separate
workspace databases, and the registered ``spike.note`` resolver through the shipped
``resolve_in`` — the same seam ``tests/postgres/test_cross_workspace.py`` already
pins for ``harness.note``, now on a real distribution's record type.

The rest of AC 7 — the form, the server-side forward to
``POST /auth/session/workspace``, and the driver step that switches a live session
between the two — is C4's and C5's. Nothing here drives an HTTP route.

Both workspaces have the module installed, deliberately: isolation must hold between
two workspaces that both *have* the spike, not merely between one that has it and one
that does not. The second case is ``module_disabled``, a different refusal with a
different cause, and C3 drives it through the route.
"""

from uuid import UUID

import pytest
from conftest import ClusterSession, InstallSpike, MakeWorkspace
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.operations import dispatch
from rheo_core.refs.resolver import NOT_FOUND, RecordHead, Unavailable, resolve_in
from rheo_core.storage.backend import UnitOfWork
from rheo_spike.operations import NOTE_ADD, NOTE_LIST

pytestmark = pytest.mark.postgres


@pytest.fixture
def two_spike_workspaces(
    make_workspace: MakeWorkspace, install_spike: InstallSpike
) -> tuple[UUID, UUID]:
    first, second = make_workspace(), make_workspace()
    install_spike(first)
    install_spike(second)
    return first, second


def _owner(workspace_id: UUID, account_id: UUID) -> WorkspaceContext:
    ctx = context_for_harness(workspace_id, account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _add(ctx: WorkspaceContext, body: str) -> str:
    outcome = dispatch(ctx, NOTE_ADD, {"body": body})
    assert outcome.ok, outcome
    assert outcome.result is not None
    return str(outcome.result.ref)  # type: ignore[attr-defined]


def _bodies(ctx: WorkspaceContext) -> list[str]:
    outcome = dispatch(ctx, NOTE_LIST, {})
    assert outcome.ok, outcome
    assert outcome.result is not None
    return [note.body for note in outcome.result.notes]  # type: ignore[attr-defined]


def test_second_workspace_sees_no_notes_from_the_first(
    two_spike_workspaces: tuple[UUID, UUID], owner_account_id: UUID
) -> None:
    """One account owning both workspaces is the normal case this boundary must hold
    for: what separates them is the context's workspace, not who is acting."""
    a, b = two_spike_workspaces
    a_ctx, b_ctx = _owner(a, owner_account_id), _owner(b, owner_account_id)

    _add(a_ctx, "workspace A's own note")

    assert _bodies(a_ctx) == ["workspace A's own note"]
    assert _bodies(b_ctx) == []


def test_a_reference_minted_in_one_workspace_is_not_found_in_the_other(
    cluster: ClusterSession,
    two_spike_workspaces: tuple[UUID, UUID],
    owner_account_id: UUID,
) -> None:
    """``not_found``, not an error and not a leak: the resolver looks in the caller's
    own database and the row is not there. A reference that never existed gets the
    same answer, indistinguishably — which is the point."""
    a, b = two_spike_workspaces
    a_ctx, b_ctx = _owner(a, owner_account_id), _owner(b, owner_account_id)
    ref = _add(a_ctx, "resolve me in A only")

    for ctx, workspace_id, expected_live in ((a_ctx, a, True), (b_ctx, b, False)):
        row = cluster.registry_row(workspace_id)
        engine = cluster.backend.pools.engine_for(row.database_name)
        with UnitOfWork(engine, row.database_name) as uow:
            head = resolve_in(ref, ctx, uow)
        if expected_live:
            assert isinstance(head, RecordHead), head
            assert head.ref.format() == ref
            assert head.display == "resolve me in A only"
            assert head.revision == 1
        else:
            assert isinstance(head, Unavailable), head
            assert head.reason == NOT_FOUND
            assert head.reference == ref
