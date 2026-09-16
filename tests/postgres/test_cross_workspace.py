"""B4 (criterion 7, HTTP API and MCP facade): a ``harness.note`` reference
minted in workspace A is ``not_found`` under a workspace-B token, on both
bearer surfaces, and succeeds under an A token, on both surfaces.

0b1 ships only the resolver seam and claims nothing of criterion 7; the HTTP
and MCP halves are this chunk's.

The fixture registers the tools as well as the operations, because the
``agent_default`` token minted below expands to the operations named by the
**registered** tools and run 0c3 made that a registry rather than a module-level
tuple. Without the call the set is empty and ``core.token.issue`` refuses
``set_empty`` before either surface is reached.
"""

from uuid import UUID

import httpx
import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.registry import (
    NOTE_GET,
    NOTE_WRITE,
    enable_harness_module,
    register_harness,
)
from rheo_app_core.main import public_app
from rheo_app_mcp import session as mcp_session
from rheo_app_mcp import tools as mcp_tools
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.core_ops import TOKEN_ISSUE
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import WorkspaceRow
from rheo_core.tokens.sets import register_core_tools

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_core_tools()
    register_harness()


@pytest.fixture
def two_workspaces(
    cluster: ClusterSession, make_workspace: MakeWorkspace
) -> tuple[WorkspaceRow, WorkspaceRow]:
    a, b = (
        cluster.registry_row(make_workspace()),
        cluster.registry_row(make_workspace()),
    )
    for row in (a, b):
        engine = cluster.backend.pools.engine_for(row.database_name)
        with UnitOfWork(engine, row.database_name) as uow:
            enable_harness_module(uow.connection)
            uow.commit()
    return a, b


def _owner_ctx(workspace_id: UUID, account_id: UUID) -> WorkspaceContext:
    ctx = context_for_harness(workspace_id, account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _mint_mcp_token(ctx: WorkspaceContext) -> str:
    """An ``mcp``-kind, ``agent_default`` token -- valid on both bearer
    surfaces (``api`` accepts ``cli``/``mcp``; ``mcp`` accepts ``mcp``/
    ``runtime``)."""
    outcome = dispatch(ctx, TOKEN_ISSUE, {"kind": "mcp", "set_name": "agent_default"})
    assert outcome.ok, outcome
    assert outcome.result is not None
    return outcome.result.value  # type: ignore[attr-defined]


def _write_note(ctx: WorkspaceContext, body: str) -> str:
    outcome = dispatch(ctx, NOTE_WRITE, {"body": body})
    assert outcome.ok, outcome
    assert outcome.result is not None
    return outcome.result.ref  # type: ignore[attr-defined]


async def _post_note_get(value: str, ref: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=public_app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/api/v1/operations/{NOTE_GET}",
            headers={"Authorization": f"Bearer {value}"},
            json={"ref": ref},
        )


async def test_cross_workspace_reference_refused_not_found_both_surfaces(
    cluster: ClusterSession,
    two_workspaces: tuple[WorkspaceRow, WorkspaceRow],
    owner_account_id: UUID,
) -> None:
    a, b = two_workspaces
    ref = _write_note(_owner_ctx(a.id, owner_account_id), "workspace A's own note")

    # `owner_account_id` owns both A and B (make_workspace's default owner);
    # what makes this a "workspace-B token" is the token's own workspace_id
    # (b.id), read from the access_token row at presentation -- not which
    # account holds it. One account owning several workspaces is the normal
    # case this boundary must still hold for.
    b_ctx = _owner_ctx(b.id, owner_account_id)
    b_value = _mint_mcp_token(b_ctx)

    # api surface
    resp = await _post_note_get(b_value, ref)
    assert resp.status_code == 404
    assert resp.json()["state"] == "not_found"

    # MCP call_tool seam
    resolved_b = mcp_session.resolve_context(b_value)
    assert isinstance(resolved_b, WorkspaceContext)
    outcome = mcp_tools.call_tool(resolved_b, "harness_get_note", {"ref": ref})
    assert outcome.state == "not_found"


async def test_same_reference_under_an_a_token_succeeds_both_surfaces(
    cluster: ClusterSession,
    two_workspaces: tuple[WorkspaceRow, WorkspaceRow],
    owner_account_id: UUID,
) -> None:
    a, _b = two_workspaces
    a_ctx = _owner_ctx(a.id, owner_account_id)
    ref = _write_note(a_ctx, "workspace A's own note, read back")
    a_value = _mint_mcp_token(a_ctx)

    resp = await _post_note_get(a_value, ref)
    assert resp.status_code == 200
    assert resp.json()["result"]["ref"] == ref

    resolved_a = mcp_session.resolve_context(a_value)
    assert isinstance(resolved_a, WorkspaceContext)
    outcome = mcp_tools.call_tool(resolved_a, "harness_get_note", {"ref": ref})
    assert outcome.ok, outcome
    assert outcome.result is not None
    assert outcome.result.ref == ref  # type: ignore[attr-defined]
