"""The MCP façade over its real streamable-HTTP transport (AC 13).

Seams under test: ``tools/list`` and ``tools/call`` across the wire, and the
bearer gate in front of them.

**Driven by the SDK's own client over the SDK's own server, with no process.**
``httpx2.ASGITransport`` carries real HTTP bytes from ``mcp.client`` into the
Starlette application ``build_mcp_app()`` returns — the same no-process technique
``tests/test_healthz.py`` uses against ``app``. So the JSON-RPC envelope, the
initialize handshake and the content negotiation are all exercised (the server
is stateless, so no ``Mcp-Session-Id`` is issued), and nothing is stubbed between
the client's ``list_tools()`` and ``rheo_core.operations.dispatch``. What is *not*
exercised is a socket and a uvicorn worker, which is the part a test cannot own
anyway.

**Both registration helpers are called in this module's own fixture.** The tool
set is empty until ``register_core_tools()`` runs and the operation set is empty
until ``register_core_operations()`` does; with either missing, every assertion
below about a tool list would pass while listing nothing. Both are idempotent,
so calling them here as well as in ``test_tokens.py`` and at startup is expected.

**The refusal tests count seam entries rather than trusting a status code.** A
401 proves the client was told no; it does not prove no tool ran. The ``seam``
fixture wraps the two functions ``transport.py`` calls — the seam itself, not a
private helper — and the assertion is that the counter is still zero. It wraps
rather than replaces, so the success tests assert on real results while the same
counter proves it increments, which is what stops "still zero" being the answer a
dead counter would give either way. The row-count snapshot beside it covers the
other half: not merely that no tool ran, but that the attempt left the databases
as it found them.
"""

import hashlib
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx2
import mcp.types as types
import pytest
import rheo_app_mcp.transport as transport
from conftest import ClusterSession
from harness.registry import NOTE_GET, register_harness
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from rheo_app_mcp.transport import (
    CONTEXT_MISSING,
    MCP_PATH,
    NO_BEARER_DETAIL,
    build_mcp_app,
)
from rheo_contracts import WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.boundary.context import (
    TOKEN_EXPIRED,
    TOKEN_MALFORMED,
    TOKEN_REVOKED,
    TOKEN_WRONG_KIND,
)
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.core_ops import TOKEN_ISSUE, TOKEN_REVOKE, WORKSPACE_STATUS
from rheo_core.storage import control_tables as t
from rheo_core.storage import core_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import (
    WorkspaceRow,
    insert_access_token,
    insert_access_token_operations,
)
from rheo_core.tokens.format import mint
from rheo_core.tokens.sets import register_core_tools
from sqlalchemy import func, select, update

pytestmark = pytest.mark.postgres

BASE_URL = "http://127.0.0.1:8100"
"""A loopback authority with a port, because ``streamable_http_app`` turns a
``127.0.0.1`` host into DNS-rebinding protection that checks the ``Host`` header
against ``127.0.0.1:*``. Naming a port here keeps the test inside the same
protection production runs with, rather than switching it off to get green."""

ENDPOINT = f"{BASE_URL}{MCP_PATH}"
_FAR_PAST = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def registrations() -> None:
    """The operations and the tools, both — see the module docstring."""
    register_core_operations()
    register_core_tools()
    register_harness()


@pytest.fixture
def operator_ctx(workspace: UUID) -> WorkspaceContext:
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _issue(
    ctx: WorkspaceContext,
    *,
    kind: str,
    account_id: UUID,
    set_name: str = "agent_default",
) -> tuple[str, UUID, frozenset[str]]:
    """Dispatch ``core.token.issue`` the real way.

    Returns the token's expanded operation set alongside its value and id, because
    a listing assertion has to be able to say *why* a tool is absent: excluded by
    the context filter, or never in the token's scope to begin with.
    """
    outcome = dispatch(
        ctx,
        TOKEN_ISSUE,
        {"kind": kind, "set_name": set_name, "account_id": str(account_id)},
    )
    assert outcome.ok, outcome
    result = outcome.result
    assert result is not None
    return result.value, result.token_id, frozenset(result.operations)  # type: ignore[attr-defined]


def _snapshot(cluster: ClusterSession, workspace_row: WorkspaceRow) -> dict[str, int]:
    """Row counts for every control-plane and workspace table (as ``test_tokens``)."""
    counts: dict[str, int] = {}
    with cluster.backend.control_engine.connect() as connection:
        for table in t.CONTROL_TABLES:
            counts[f"control.{table.name}"] = connection.execute(
                select(func.count()).select_from(table)
            ).scalar_one()
    engine = cluster.backend.pools.engine_for(workspace_row.database_name)
    with UnitOfWork(engine, workspace_row.database_name) as uow:
        for table in core_tables.CORE_TABLES:
            counts[f"core.{table.name}"] = uow.connection.execute(
                select(func.count()).select_from(table)
            ).scalar_one()
    return counts


class _SeamCounter:
    """How many times the transport entered ``list_tools``/``call_tool``."""

    def __init__(self) -> None:
        self.list_tools = 0
        self.call_tool = 0

    @property
    def total(self) -> int:
        return self.list_tools + self.call_tool


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> _SeamCounter:
    """Count entries into the two functions ``transport.py`` dispatches through.

    Wrapped, never replaced: each wrapper calls the real function, so a test that
    also asserts on the result is asserting on real behaviour.
    """
    counter = _SeamCounter()
    real_list = transport.list_tools
    real_call = transport.call_tool

    def counting_list(ctx: WorkspaceContext) -> Any:
        counter.list_tools += 1
        return real_list(ctx)

    def counting_call(
        ctx: WorkspaceContext,
        name: str,
        arguments: Mapping[str, object],
        *,
        consumers: Any = None,
    ) -> Any:
        counter.call_tool += 1
        return real_call(ctx, name, arguments, consumers=consumers)

    monkeypatch.setattr(transport, "list_tools", counting_list)
    monkeypatch.setattr(transport, "call_tool", counting_call)
    return counter


@pytest.fixture
def app() -> Iterator[Any]:
    """One built application per test.

    Built here and entered in ``_client``/``_post_raw``: an ASGI transport never
    sends a lifespan event, and ``streamable_http_app``'s lifespan is what starts
    the SDK's session manager, so each helper opens
    ``router.lifespan_context(app)`` around its own traffic rather than leaving a
    session manager running across tests.

    ``consumers=None`` because this file drives the two core tools, both of which
    name ``read`` operations that publish nothing. The argument has no default on
    purpose — the composition root has to name its own registry — so "none here"
    is stated rather than inherited.
    """
    yield build_mcp_app(consumers=None)


async def _client(app: Any, bearer: str | None) -> AsyncIterator[Client]:
    headers = {} if bearer is None else {"Authorization": f"Bearer {bearer}"}
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=BASE_URL,
            headers=headers,
        ) as http_client:
            async with Client(
                streamable_http_client(ENDPOINT, http_client=http_client),
                raise_exceptions=True,
            ) as client:
                yield client


async def _post_raw(app: Any, bearer: str | None) -> httpx2.Response:
    """One unauthenticated-or-bad-token ``tools/list`` POST, straight at the wire.

    Deliberately not the SDK client: with a bad token the gate answers the very
    first request, so there is no session to initialise and the client would only
    report that its handshake failed. The raw POST shows the status and the state.
    """
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE_URL
        ) as http_client:
            return await http_client.post(
                MCP_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers=headers,
            )


# --- AC 13: the round trips ---------------------------------------------------


async def test_tool_list_round_trip_returns_the_visible_tools(
    app: Any, operator_ctx: WorkspaceContext, owner_account_id: UUID, seam: _SeamCounter
) -> None:
    """``tools/list`` over the wire returns exactly what this context may call.

    The four core tools (issue #127 registered ``operations_get``,
    ``operations_list`` and ``audit_list`` beside ``workspace_status``; the token's
    account is an owner, so ``audit_list``'s owner/operator restriction admits it),
    and that is the filtering claim, not an accident of registration: this token's
    ``agent_default`` set carries ``harness.note.get`` as well, and
    ``harness_get_note`` is absent because the ``harness`` module is not enabled in
    a freshly provisioned workspace. **Both halves are asserted, and
    the second is the load-bearing one** — without it, a listing that had stopped
    filtering entirely and a token that never carried the operation would look
    identical from here.
    """
    value, _, operations = _issue(operator_ctx, kind="mcp", account_id=owner_account_id)
    assert NOTE_GET in operations, (
        "the token must carry it for its absence to mean anything"
    )
    async for client in _client(app, value):
        listed = await client.list_tools()
    names = [tool.name for tool in listed.tools]
    assert names == [
        "audit_list",
        "operations_get",
        "operations_list",
        "workspace_status",
    ]
    assert "harness_get_note" not in names
    assert seam.list_tools >= 1
    schema = listed.tools[names.index("workspace_status")].input_schema
    assert schema["type"] == "object"


async def test_tool_call_round_trip_dispatches_the_operation(
    app: Any, operator_ctx: WorkspaceContext, owner_account_id: UUID, seam: _SeamCounter
) -> None:
    """``tools/call`` over the wire reaches ``dispatch`` and returns its output.

    The payload is compared against a second, independent dispatch of the same
    operation on an operator context rather than against a shape typed out here:
    the claim is that the transport returns what the operation returned, and a
    literal would only prove the transport returns a literal.
    """
    value, _, _ = _issue(operator_ctx, kind="mcp", account_id=owner_account_id)
    async for client in _client(app, value):
        result = await client.call_tool("workspace_status", {})
    assert result.is_error is False
    payload = result.structured_content
    assert isinstance(payload, dict)
    assert payload["state"] == "succeeded"

    direct = dispatch(operator_ctx, WORKSPACE_STATUS, {})
    assert direct.ok, direct
    assert direct.result is not None
    assert payload["result"] == direct.result.model_dump(mode="json")
    assert seam.call_tool == 1

    text = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert text == payload


async def test_unknown_tool_name_is_not_found_over_the_wire(
    app: Any, operator_ctx: WorkspaceContext, owner_account_id: UUID
) -> None:
    """A name outside this context's listing comes back as an error result, not a
    protocol error: the call was well formed and the façade answered it."""
    value, _, _ = _issue(operator_ctx, kind="mcp", account_id=owner_account_id)
    async for client in _client(app, value):
        result = await client.call_tool("harness_get_note", {"ref": "x"})
    assert result.is_error is True
    payload = result.structured_content
    assert isinstance(payload, dict)
    assert payload["state"] == "not_found"


# --- AC 13 / criterion 9: refused at the transport, with no tool run ----------


async def test_missing_bearer_is_refused_before_any_tool_runs(
    app: Any, seam: _SeamCounter
) -> None:
    """No ``Authorization`` header at all, answered exactly as the HTTP API
    answers the same condition (``api_routes.py``: ``TOKEN_MALFORMED``, that
    detail text, 401). Both are asserted, so this surface cannot drift into a
    private refusal vocabulary without a test noticing."""
    response = await _post_raw(app, None)
    assert response.status_code == 401
    body = response.json()
    assert body["state"] == TOKEN_MALFORMED
    assert body["detail"] == NO_BEARER_DETAIL
    assert seam.total == 0


@pytest.mark.parametrize(
    ("bad_token", "expected_state"),
    [("not-a-real-token", TOKEN_MALFORMED), ("rheo_mcp_" + "a" * 43, TOKEN_MALFORMED)],
)
async def test_malformed_bearer_is_refused_before_any_tool_runs(
    app: Any, seam: _SeamCounter, bad_token: str, expected_state: str
) -> None:
    """Two shapes of malformed: one that is not a token at all, and one that looks
    like a well-formed ``mcp`` value and matches no row."""
    response = await _post_raw(app, bad_token)
    assert response.status_code == 401
    assert response.json()["state"] == expected_state
    assert seam.total == 0


async def test_cli_token_is_refused_wrong_kind_before_any_tool_runs(
    app: Any,
    cluster: ClusterSession,
    workspace: UUID,
    operator_ctx: WorkspaceContext,
    owner_account_id: UUID,
    seam: _SeamCounter,
) -> None:
    """A perfectly valid ``cli`` token is still the wrong kind for this surface,
    and the snapshot shows the attempt changed nothing."""
    value, _, _ = _issue(
        operator_ctx, kind="cli", account_id=owner_account_id, set_name="read_only"
    )
    row = cluster.registry_row(workspace)
    before = _snapshot(cluster, row)
    response = await _post_raw(app, value)
    assert response.status_code == 401
    assert response.json()["state"] == TOKEN_WRONG_KIND
    assert seam.total == 0
    assert _snapshot(cluster, row) == before


async def test_expired_token_is_refused_before_any_tool_runs(
    app: Any,
    cluster: ClusterSession,
    workspace: UUID,
    operator_ctx: WorkspaceContext,
    owner_account_id: UUID,
    seam: _SeamCounter,
) -> None:
    value, token_id, _ = _issue(operator_ctx, kind="mcp", account_id=owner_account_id)
    with cluster.backend.control_engine.begin() as connection:
        connection.execute(
            update(t.access_token)
            .where(t.access_token.c.id == token_id)
            .values(expires_at=_FAR_PAST)
        )
    row = cluster.registry_row(workspace)
    before = _snapshot(cluster, row)
    response = await _post_raw(app, value)
    assert response.status_code == 401
    assert response.json()["state"] == TOKEN_EXPIRED
    assert seam.total == 0
    assert _snapshot(cluster, row) == before


async def test_revoked_token_is_refused_before_any_tool_runs(
    app: Any,
    cluster: ClusterSession,
    workspace: UUID,
    operator_ctx: WorkspaceContext,
    owner_account_id: UUID,
    seam: _SeamCounter,
) -> None:
    """Revoked is the fifth state ``runtime-and-mcp.md`` names for this surface,
    and the one a session most plausibly meets mid-life: the token was good when
    the client connected. Revoked through the real ``core.token.revoke`` dispatch
    rather than by writing ``revoked_at``, so the refusal is reached the way a
    revocation actually arrives."""
    value, token_id, _ = _issue(operator_ctx, kind="mcp", account_id=owner_account_id)
    revoked = dispatch(operator_ctx, TOKEN_REVOKE, {"token_id": str(token_id)})
    assert revoked.ok, revoked
    row = cluster.registry_row(workspace)
    before = _snapshot(cluster, row)
    response = await _post_raw(app, value)
    assert response.status_code == 401
    assert response.json()["state"] == TOKEN_REVOKED
    assert seam.total == 0
    assert _snapshot(cluster, row) == before


async def test_scope_invalid_token_is_refused_before_any_tool_runs(
    app: Any,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    seam: _SeamCounter,
) -> None:
    """A snapshot row carrying a non-token-issuable operation — a row ``issue()``
    can never write — is refused at presentation, so the transport never gets a
    context to hand a tool."""
    value, raw = mint("mcp")
    with cluster.backend.control_engine.begin() as connection:
        row_inserted = insert_access_token(
            connection,
            account_id=owner_account_id,
            workspace_id=workspace,
            kind="mcp",
            issued_from="operator",
            token_hash=hashlib.sha256(raw).digest(),
            set_name=None,
            purpose=None,
            expires_at=datetime(2999, 1, 1, tzinfo=UTC),
        )
        insert_access_token_operations(
            connection,
            token_id=row_inserted.id,
            operation_names=["core.approval.approve"],
        )
    response = await _post_raw(app, value)
    assert response.status_code == 401
    assert response.json()["state"] == "token_scope_invalid"
    assert seam.total == 0


# --- the second guard, which the counter assertions above silently rely on ----


async def test_handlers_refuse_a_request_that_bypassed_the_gate() -> None:
    """``_context_of`` raises when nothing put a context in the request.

    **Why this test exists and why it is not redundant with the five above.** Each
    of those asserts ``seam.total == 0`` after a refused bearer. That assertion is
    true for two different reasons at once — the gate answered before the SDK was
    reached, *and* the handlers would have refused anyway — and from outside it
    cannot tell which. So the counter alone cannot distinguish "the gate is in
    front" from "the gate is gone and the second guard caught it", which is
    exactly the regression the counter was meant to catch.

    This drives the two handlers with a request carrying no gate-written state,
    which is the shape a future mounting that bypassed the gate would produce, and
    pins that they raise rather than serve. Nothing about it is ungated in
    production: the gate is still the only entry point, which is why this is
    reached by calling the handlers rather than over the wire.
    """
    ungated = SimpleNamespace(request=SimpleNamespace(state=SimpleNamespace()))
    # ``_on_call_tool`` takes the consumer registry as a keyword with no default
    # (``build_server`` closes over the one the composition root handed it), so the
    # bare handler is bound here to keep the pair callable through one loop. The
    # value is irrelevant: this test's whole claim is that the handler raises before
    # it reaches a tool.
    for handler, params in (
        (transport._on_list_tools, None),
        (
            partial(transport._on_call_tool, consumers=None),
            types.CallToolRequestParams(name="workspace_status"),
        ),
    ):
        with pytest.raises(RuntimeError) as raised:
            await handler(ungated, params)  # type: ignore[arg-type]
        assert CONTEXT_MISSING in str(raised.value)

    # And the same for a request object the transport never attached one to at all
    # (``ctx.request`` is ``None`` for a non-HTTP transport), so neither path
    # defaults its way to a context.
    with pytest.raises(RuntimeError) as raised_none:
        await transport._on_list_tools(SimpleNamespace(request=None), None)  # type: ignore[arg-type]
    assert CONTEXT_MISSING in str(raised_none.value)
