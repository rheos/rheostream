"""C8: the bearer ``api`` surface's envelope and status mapping
(``POST /api/v1/operations/{name}``).

Drives ``apps/core/src/rheo_app_core/api_routes.py`` in-process over
``httpx.ASGITransport`` (the pattern ``tests/test_healthz.py`` and
``tests/postgres/test_sessions.py`` already establish): no Authorization
header and a malformed one both refuse ``token_malformed`` (401); a valid
token succeeds (200); ``role_not_permitted``/``operation_not_permitted`` (403);
``not_found`` (404); ``input_invalid`` (422); ``operation_id`` is ``null`` for
every operation shipped in release one and carries the minted id for the one
``long_running`` declaration in the tree. Also B2's HTTP channels (plan.md's
own line for this chunk): a reserved field naming another workspace,
presented through the query string
and the JSON body alike, is silently dropped and the dispatch lands in the
token's own (authenticated) workspace -- not a distinct acceptance-criteria
row in ``00-index.md``'s matrix (which lists B2 as fully claimed by 0b1), but
named explicitly by both ``plan.md``'s C8 line ("B2's HTTP JSON-body and
query-string channels (the api surface)") and ``spec.md``'s own B2 bullet
("the HTTP channels are C8"); proved here in favour of the canonical
documents over the matrix's silence, per ``00-index.md``'s own rule that a
canonical document wins a disagreement.
"""

from uuid import UUID

import httpx
import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.records import get_note
from harness.registry import (
    NOTE_SCHEDULE,
    NOTE_WRITE,
    enable_harness_module,
    register_harness,
)
from rheo_app_core.main import public_app
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.operations import OPERATION_GET, dispatch, register_core_operations
from rheo_core.operations.core_ops import TOKEN_ISSUE
from rheo_core.storage import control_tables as t
from rheo_core.storage.backend import UnitOfWork
from sqlalchemy import inspect, update

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_harness()


def _owner_ctx(workspace: UUID, owner_account_id: UUID) -> WorkspaceContext:
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _mint(
    ctx: WorkspaceContext, *, kind: str = "cli", set_name: str = "cli_full"
) -> str:
    outcome = dispatch(ctx, TOKEN_ISSUE, {"kind": kind, "set_name": set_name})
    assert outcome.ok, outcome
    assert outcome.result is not None
    return outcome.result.value  # type: ignore[attr-defined]


async def _post(
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json: object = None,
    params: object = None,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=public_app), base_url="http://test"
    ) as client:
        return await client.post(path, headers=headers or {}, json=json, params=params)


# --- token refusals: 401 -----------------------------------------------------


async def test_no_authorization_header_refused_401() -> None:
    resp = await _post("/api/v1/operations/core.workspace.status")
    assert resp.status_code == 401
    body = resp.json()
    assert body["state"] == "token_malformed"
    assert body["operation_id"] is None
    assert body["error"]["error_code"] == "token_malformed"


async def test_wrong_scheme_authorization_refused_401() -> None:
    resp = await _post(
        "/api/v1/operations/core.workspace.status",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert resp.status_code == 401
    assert resp.json()["state"] == "token_malformed"


async def test_garbage_bearer_refused_401() -> None:
    resp = await _post(
        "/api/v1/operations/core.workspace.status",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401
    assert resp.json()["state"] == "token_malformed"


# --- success: 200, and a null operation_id for every shipped operation ------


async def test_valid_token_succeeds_200_with_null_operation_id(
    workspace: UUID, owner_account_id: UUID
) -> None:
    value = _mint(_owner_ctx(workspace, owner_account_id))
    resp = await _post(
        "/api/v1/operations/core.workspace.status",
        headers={"Authorization": f"Bearer {value}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "succeeded"
    assert body["operation_id"] is None
    assert "modules" in body["result"]


# --- role_not_permitted / operation_not_permitted: 403 -----------------------


async def test_role_not_permitted_403(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """A token's operation_set is a snapshot, frozen at issuance; the role
    the registry checks it against is read fresh, at every dispatch. Mint an
    owner's ``cli_full`` token (its snapshot carries ``core.settings.set``,
    owner-only) and then demote the same account to ``member`` -- the
    snapshot is unchanged, but the role check now refuses it, distinct from
    ``operation_not_permitted`` (which fires when the operation is not even
    in the snapshot, proved separately below).
    """
    value = _mint(_owner_ctx(workspace, owner_account_id))
    with cluster.backend.control_engine.begin() as connection:
        connection.execute(
            update(t.membership)
            .where(
                t.membership.c.account_id == owner_account_id,
                t.membership.c.workspace_id == workspace,
            )
            .values(role=Role.MEMBER.value)
        )
    resp = await _post(
        "/api/v1/operations/core.settings.set",
        headers={"Authorization": f"Bearer {value}"},
        json={"key": "identity.token_max_days.cli", "value": 45},
    )
    assert resp.status_code == 403
    assert resp.json()["state"] == "role_not_permitted"


async def test_operation_not_permitted_403(
    workspace: UUID, owner_account_id: UUID
) -> None:
    value = _mint(_owner_ctx(workspace, owner_account_id), set_name="read_only")
    resp = await _post(
        "/api/v1/operations/core.settings.set",
        headers={"Authorization": f"Bearer {value}"},
        json={"key": "identity.token_max_days.cli", "value": 45},
    )
    assert resp.status_code == 403
    assert resp.json()["state"] == "operation_not_permitted"


# --- not_found: 404 -----------------------------------------------------------


async def test_not_found_404(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    value = _mint(_owner_ctx(workspace, owner_account_id), set_name="cli_full")
    made_up_ref = "harness.note:00000000-0000-7000-8000-000000000000"
    resp = await _post(
        "/api/v1/operations/harness.note.get",
        headers={"Authorization": f"Bearer {value}"},
        json={"ref": made_up_ref},
    )
    assert resp.status_code == 404
    assert resp.json()["state"] == "not_found"


# --- input_invalid: 422 -------------------------------------------------------


async def test_input_invalid_422(workspace: UUID, owner_account_id: UUID) -> None:
    value = _mint(_owner_ctx(workspace, owner_account_id))
    resp = await _post(
        "/api/v1/operations/core.settings.set",
        headers={"Authorization": f"Bearer {value}"},
        json={"key": "identity.token_max_days.cli"},  # missing "value"
    )
    assert resp.status_code == 422
    assert resp.json()["state"] == "input_invalid"


# --- AC 20: the envelope's operation_id, on this surface --------------------


async def test_a_long_running_dispatch_carries_its_minted_id_in_the_envelope(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """AC 20, bearer half: a ``long_running`` dispatch answers with the minted id,
    and that id names a record the supported read finds still ``pending``.

    The envelope's ``operation_id`` becoming a nullable uuid is reachable only
    through a ``long_running`` declaration, and ``harness.note.schedule`` is the
    tree's only one — no shipped ``core.*`` operation carries the flag, which is why
    every other test in this file still asserts ``null`` and stays correct.
    """
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    ctx = _owner_ctx(workspace, owner_account_id)
    value = _mint(ctx)

    resp = await _post(
        f"/api/v1/operations/{NOTE_SCHEDULE}",
        headers={"Authorization": f"Bearer {value}"},
        json={"body": "queued over the bearer surface"},
    )

    body = resp.json()
    assert body["state"] == "pending", body
    minted = body["operation_id"]
    assert minted is not None, body
    # Read back through the supported operation, never the table (AC 19). No worker
    # has visited this workspace, so the record is still where the dispatcher left
    # it.
    read = dispatch(ctx, OPERATION_GET, {"operation_id": minted})
    assert read.ok, read
    assert read.result is not None
    assert read.result.state == "pending"  # type: ignore[attr-defined]
    assert read.result.name == NOTE_SCHEDULE  # type: ignore[attr-defined]

    # 202 Accepted, and the handler's own output carried rather than dropped. A
    # ``pending`` outcome is success-shaped: it has a ``result`` and no ``error``, so
    # the listener takes the result branch through ``api_routes.carries_result``
    # rather than through ``OperationOutcome.ok``, which is ``succeeded``-only and
    # would send this response — a *successful* long-running dispatch — down the
    # error path at 400 with neither key set.
    assert resp.status_code == 202, resp.text
    assert "error" not in body, body
    assert body["result"]["operation_id"] == minted, body
    assert body["result"]["job_id"] is not None, body


# --- B2's HTTP channels: reserved fields are ignored, either way ------------


async def test_reserved_field_in_query_string_and_body_is_ignored(
    cluster: ClusterSession,
    make_workspace: MakeWorkspace,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    other = make_workspace()
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    value = _mint(_owner_ctx(workspace, owner_account_id), set_name="cli_full")
    resp = await _post(
        f"/api/v1/operations/{NOTE_WRITE}",
        headers={"Authorization": f"Bearer {value}"},
        params={"workspace_id": str(other), "database": "not-consulted"},
        json={"body": "written through the http api surface", "dsn": "ignored-too"},
    )
    assert resp.status_code == 200, resp.json()
    ref = resp.json()["result"]["ref"]
    note_id = UUID(ref.rsplit(":", 1)[1])
    with UnitOfWork(engine, row.database_name) as uow:
        note = get_note(uow.connection, note_id)
    assert note is not None and note.body == "written through the http api surface"
    other_row = cluster.registry_row(other)
    other_engine = cluster.backend.pools.engine_for(other_row.database_name)
    with UnitOfWork(other_engine, other_row.database_name) as other_uow:
        # The other workspace never had harness.note.write dispatched against
        # it, so the table itself may not exist there yet -- that absence is
        # exactly the proof the write did not land in it.
        has_table = inspect(other_uow.connection).has_table("note", schema="harness")
        assert not has_table or get_note(other_uow.connection, note_id) is None
