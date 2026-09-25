"""The ``mcp`` surface mounted in the ``core`` process (issue #127), end to end.

Seams under test: ``rheo_app_core.main.app`` with its lifespan running, the
:class:`~rheo_app_core.mcp_mount.McpSurfaceRouter` in front of it, and the gated
façade behind that, in both routing modes.

**The client is pointed at the URL the runtime writes, not at a path typed out
here.** ``rheo_core.runtime.operations`` hands the CLI ``url_for(config, MCP, "/")``,
so each test resolves that same expression under the mode it set and connects the
SDK's own client to it. A mount that answered ``/mcp`` but not the URL a run is
actually given would pass a test that spelled ``/mcp`` and fail every real run;
driving the ``url_for`` output is what closes that gap.

**The lifespan is the real one.** ``async with lifespan(app)`` runs
``run_startup`` and then :func:`~rheo_app_core.mcp_mount.mounted_mcp`, which reads
the routing settings from the environment this module sets with ``monkeypatch``.
That is the path a deployment takes (``RHEO__routing__*`` in its environment), and
it is what proves the mount reads the resolved deployment settings rather than the
package defaults.

**Refusals count seam entries**, as ``test_mcp_transport.py`` does and for its
reason: a 401 or a 421 proves the client was told no, and the counter staying at
zero proves no tool ran.
"""

import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx2
import pytest
import rheo_app_mcp.transport as transport
from conftest import ClusterSession
from harness.registry import (
    NOTE_SCHEDULE,
    add_member,
    enable_harness_module,
    register_harness,
)
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from rheo_app_core.main import app, lifespan, public_app
from rheo_app_core.mcp_mount import STATE_ATTRIBUTE, McpMount
from rheo_contracts import ContextPurpose, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.boundary.context import TOKEN_MALFORMED, TOKEN_REVOKED, TOKEN_WRONG_KIND
from rheo_core.boundary.factories import context_from_token
from rheo_core.operations import (
    AUDIT_LIST,
    OPERATION_GET,
    OPERATION_NOT_PERMITTED,
    dispatch,
    register_core_operations,
)
from rheo_core.operations.core_ops import TOKEN_ISSUE, TOKEN_REVOKE, WORKSPACE_STATUS
from rheo_core.refs.resolver import NOT_FOUND
from rheo_core.routing import MCP, RoutingConfig, url_for
from rheo_core.settings import resolve
from rheo_core.storage.backend import UnitOfWork
from rheo_core.tokens.issue import issue_runtime_token
from rheo_core.tokens.sets import register_core_tools

pytestmark = pytest.mark.postgres

BASE_HOST = "example.test"
MCP_HOST = f"mcp.{BASE_HOST}"
SHELL_HOST = f"circuit.{BASE_HOST}"

CORE_TOOL_NAMES = (
    "audit_list",
    "operations_get",
    "operations_list",
    "workspace_status",
)
"""The four core tools an owner's ``agent_default`` token lists, sorted as the
façade sorts them. ``harness_get_note`` is listed beside them in these tests,
because the fixture enables the harness and the test profile registers its
operation; the assertions set it aside rather than depend on it."""


@pytest.fixture(autouse=True)
def registrations() -> None:
    """The operations, the tools and the harness, all idempotent.

    Startup registers the first two again inside the lifespan; registering them here
    as well is what lets the token below be minted before the lifespan starts, with
    an ``agent_default`` set that already names every core tool's operation.
    """
    register_core_operations()
    register_core_tools()
    register_harness()


@pytest.fixture
def owner(cluster: ClusterSession, workspace: UUID, owner_account_id: UUID) -> Any:
    """An owner context over a workspace whose ``harness`` module is enabled, so
    ``harness.note.schedule`` can mint a real ``core.operation`` record."""
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _mint(
    ctx: WorkspaceContext, *, kind: str = "mcp", set_name: str = "agent_default"
) -> tuple[str, UUID]:
    outcome = dispatch(ctx, TOKEN_ISSUE, {"kind": kind, "set_name": set_name})
    assert outcome.ok, outcome
    assert outcome.result is not None
    return (
        str(outcome.result.value),  # type: ignore[attr-defined]
        outcome.result.token_id,  # type: ignore[attr-defined]
    )


def _mint_mcp(ctx: WorkspaceContext) -> str:
    return _mint(ctx)[0]


def _set_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str, *, public_host: str = ""
) -> str:
    """Set the routing topology in the environment and return the runtime's URL."""
    monkeypatch.setenv("RHEO__routing__mode", mode)
    monkeypatch.setenv("RHEO__routing__scheme", "https")
    monkeypatch.setenv("RHEO__routing__base_host", BASE_HOST)
    monkeypatch.setenv("RHEO__routing__public_host", public_host)
    return url_for(RoutingConfig.from_settings(resolve()), MCP, "/")


@pytest.fixture
def path_mode(monkeypatch: pytest.MonkeyPatch) -> str:
    return _set_mode(monkeypatch, "path")


@pytest.fixture
def subdomain_mode(monkeypatch: pytest.MonkeyPatch) -> str:
    return _set_mode(monkeypatch, "subdomain")


class _SeamCounter:
    def __init__(self) -> None:
        self.list_tools = 0
        self.call_tool = 0

    @property
    def total(self) -> int:
        return self.list_tools + self.call_tool


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> _SeamCounter:
    """Count entries into the transport's two seam functions, wrapping the real
    ones (``test_mcp_transport.py``'s fixture, for the same reason)."""
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


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


async def _client(url: str, bearer: str) -> AsyncIterator[Client]:
    """The SDK's client against ``url``, through ``app`` with its lifespan running."""
    async with lifespan(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=_origin(url),
            headers={"Authorization": f"Bearer {bearer}"},
        ) as http_client:
            async with Client(
                streamable_http_client(url, http_client=http_client),
                raise_exceptions=True,
            ) as client:
                yield client


def _list_body() -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def _listed_names(response: httpx2.Response) -> list[str]:
    """Tool names out of a raw ``tools/list`` reply. The mount answers plain JSON
    (``json_response=True``), so the body is the JSON-RPC response itself."""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    return [tool["name"] for tool in response.json()["result"]["tools"]]


_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


async def _raw(
    url: str,
    *,
    bearer: str | None = None,
    host: str | None = None,
    method: str = "POST",
) -> httpx2.Response:
    """One request straight at the wire, with the lifespan running.

    ``host`` overrides the ``Host`` header while the URL (and so the route) stays
    what it is: exactly what a DNS-rebinding page, or a proxy routing a host this
    deployment never configured, would send.
    """
    headers = dict(_MCP_HEADERS)
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    if host is not None:
        headers["Host"] = host
    async with lifespan(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=_origin(url)
        ) as http_client:
            if method == "GET":
                return await http_client.get(url, headers=headers)
            return await http_client.post(url, json=_list_body(), headers=headers)


# --- the URL shape the runtime writes -----------------------------------------------


def test_the_runtime_url_has_the_documented_shape_in_each_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``url_for(MCP, "/")`` is what these tests connect to, so pin what it is:
    ``/mcp/`` on the one host in path mode, ``/`` on the ``mcp`` host in subdomain
    mode, and the public port carried when ``public_host`` names one."""
    assert _set_mode(monkeypatch, "path") == f"https://{BASE_HOST}/mcp/"
    assert _set_mode(monkeypatch, "subdomain") == f"https://{MCP_HOST}/"
    assert (
        _set_mode(monkeypatch, "subdomain", public_host=f"{BASE_HOST}:8443")
        == f"https://{MCP_HOST}:8443/"
    )


# --- the round trip, both modes -----------------------------------------------------


@pytest.mark.parametrize("mode", ["path", "subdomain"])
async def test_owner_lists_the_core_tools_and_calls_two_of_them(
    monkeypatch: pytest.MonkeyPatch,
    owner: WorkspaceContext,
    seam: _SeamCounter,
    mode: str,
) -> None:
    """A real minted ``mcp`` token, the runtime's own URL, the real lifespan.

    ``tools/list`` returns the four core tools (the harness tool is listed too,
    because this workspace enables the harness and ``agent_default`` carries its
    operation). ``workspace_status`` and ``operations_get`` are then called and each
    payload is compared with an independent dispatch of the same operation, so the
    claim is "the mounted surface returns what the operation returns", not a shape.
    The record ``operations_get`` reads is a real one, minted by a ``long_running``
    harness dispatch before the lifespan starts.
    """
    url = _set_mode(monkeypatch, mode)
    scheduled = dispatch(owner, NOTE_SCHEDULE, {"body": "a record to read over MCP"})
    assert scheduled.operation_id is not None, scheduled
    operation_id = str(scheduled.operation_id)
    value = _mint_mcp(owner)

    async for client in _client(url, value):
        listed = await client.list_tools()
        status_result = await client.call_tool("workspace_status", {})
        get_result = await client.call_tool(
            "operations_get", {"operation_id": operation_id}
        )
        missing = await client.call_tool(
            "operations_get", {"operation_id": str(uuid.uuid4())}
        )

    names = [tool.name for tool in listed.tools]
    assert [name for name in names if name != "harness_get_note"] == list(
        CORE_TOOL_NAMES
    )
    assert seam.list_tools >= 1

    assert status_result.is_error is False
    status_payload = status_result.structured_content
    assert isinstance(status_payload, dict)
    direct_status = dispatch(owner, WORKSPACE_STATUS, {})
    assert direct_status.result is not None
    assert status_payload["result"] == direct_status.result.model_dump(mode="json")

    assert get_result.is_error is False
    get_payload = get_result.structured_content
    assert isinstance(get_payload, dict)
    assert get_payload["state"] == "succeeded"
    direct_get = dispatch(owner, OPERATION_GET, {"operation_id": operation_id})
    assert direct_get.result is not None
    assert get_payload["result"] == direct_get.result.model_dump(mode="json")
    assert get_payload["result"]["name"] == NOTE_SCHEDULE

    assert missing.is_error is True
    assert isinstance(missing.structured_content, dict)
    assert missing.structured_content["state"] == NOT_FOUND
    assert seam.call_tool == 3


async def test_a_member_is_not_listed_audit_list(
    path_mode: str, cluster: ClusterSession, workspace: UUID, seam: _SeamCounter
) -> None:
    """``audit_list`` is owner/operator only, and the listing filters on role, so a
    member's model is never offered it; the two operation reads are member-visible.

    The member is a real second membership (``add_member``), and the token is
    minted from that member's own context, so its account role is ``member``.
    """
    member_id = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="member-one"
    )
    member = context_for_harness(workspace, member_id, Role.MEMBER)
    assert isinstance(member, WorkspaceContext), member
    value = _mint_mcp(member)

    async for client in _client(path_mode, value):
        listed = await client.list_tools()
        refused = await client.call_tool("audit_list", {})
    names = {tool.name for tool in listed.tools}
    assert {"operations_get", "operations_list", "workspace_status"} <= names
    assert "audit_list" not in names
    assert refused.is_error is True
    assert isinstance(refused.structured_content, dict)
    assert refused.structured_content["state"] == NOT_FOUND


async def test_audit_list_refuses_the_owner_only_opt_ins(
    path_mode: str, owner: WorkspaceContext
) -> None:
    """The tool declares ``limit`` alone, so a model cannot ask for the telemetry or
    deletion-ledger collections even on an owner's token; ``limit`` works."""
    value = _mint_mcp(owner)
    async for client in _client(path_mode, value):
        ok = await client.call_tool("audit_list", {"limit": 5})
        refused = await client.call_tool("audit_list", {"include_deletions": True})
    assert ok.is_error is False, ok.structured_content
    assert isinstance(ok.structured_content, dict)
    assert ok.structured_content["result"]["deletions"] is None
    assert ok.structured_content["result"]["tool_telemetry"] is None
    assert refused.is_error is True
    assert isinstance(refused.structured_content, dict)
    assert refused.structured_content["state"] == "input_invalid"


# --- both endpoint spellings in path mode -------------------------------------------


async def test_path_mode_serves_the_surface_path_without_a_trailing_slash(
    path_mode: str, owner: WorkspaceContext
) -> None:
    """``/mcp`` and ``/mcp/`` both reach the one SDK route, with no redirect."""
    assert path_mode.endswith("/")
    bare = path_mode.rstrip("/")
    value = _mint_mcp(owner)
    async for client in _client(bare, value):
        listed = await client.list_tools()
    assert "workspace_status" in [tool.name for tool in listed.tools]


# --- refused before any tool runs ---------------------------------------------------


@pytest.mark.parametrize("mode", ["path", "subdomain"])
@pytest.mark.parametrize("bearer", [None, "not-a-real-token"])
async def test_no_or_bad_bearer_is_refused_at_the_mounted_gate(
    monkeypatch: pytest.MonkeyPatch,
    seam: _SeamCounter,
    mode: str,
    bearer: str | None,
) -> None:
    url = _set_mode(monkeypatch, mode)
    response = await _raw(url, bearer=bearer)
    assert response.status_code == 401, response.text
    assert response.json()["state"] == TOKEN_MALFORMED
    assert seam.total == 0


async def test_path_mode_refuses_a_host_outside_the_allow_list(
    path_mode: str, owner: WorkspaceContext, seam: _SeamCounter
) -> None:
    """The SDK's DNS-rebinding protection, configured from the routing settings: a
    valid token presented with a ``Host`` this deployment does not serve is 421,
    and no tool runs. ``Host: example.test`` (the allowed name) is the control."""
    value = _mint_mcp(owner)
    refused = await _raw(path_mode, bearer=value, host="attacker.example.org")
    assert refused.status_code == 421, refused.text
    assert seam.total == 0

    with_port = await _raw(path_mode, bearer=value, host=f"{BASE_HOST}:8443")
    assert with_port.status_code == 421, (
        "no wildcard port is admitted; a port must be named in routing.public_host"
    )
    assert seam.total == 0


async def test_path_mode_admits_the_public_host_port_variant(
    monkeypatch: pytest.MonkeyPatch, owner: WorkspaceContext
) -> None:
    """With ``public_host = example.test:8443`` the ``url_for`` URL carries the port,
    and the allow-list admits that exact authority."""
    url = _set_mode(monkeypatch, "path", public_host=f"{BASE_HOST}:8443")
    assert url == f"https://{BASE_HOST}:8443/mcp/"
    value = _mint_mcp(owner)
    async for client in _client(url, value):
        listed = await client.list_tools()
    assert "workspace_status" in [tool.name for tool in listed.tools]


async def test_a_foreign_origin_is_refused(
    path_mode: str, owner: WorkspaceContext, seam: _SeamCounter
) -> None:
    """The origin half of the allow-list: a browser page on another origin is 403."""
    value = _mint_mcp(owner)
    headers = dict(_MCP_HEADERS)
    headers["Authorization"] = f"Bearer {value}"
    headers["Origin"] = "https://attacker.example.org"
    async with lifespan(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=_origin(path_mode)
        ) as http_client:
            response = await http_client.post(
                path_mode, json=_list_body(), headers=headers
            )
    assert response.status_code == 403, response.text
    assert seam.total == 0


# --- subdomain mode: only the mcp host reaches MCP ----------------------------------


async def test_subdomain_mode_other_hosts_never_reach_mcp(
    subdomain_mode: str, owner: WorkspaceContext, seam: _SeamCounter
) -> None:
    """``/mcp`` and ``/`` on the shell host are FastAPI's, not the façade's: a valid
    token gets FastAPI's 404 (or the method-not-allowed a route there answers), and
    the seam is never entered. The bearer gate's 401 would mean MCP was reached."""
    value = _mint_mcp(owner)
    for path in ("/mcp", "/mcp/", "/"):
        response = await _raw(
            f"https://{SHELL_HOST}{path}", bearer=value, host=SHELL_HOST
        )
        assert response.status_code in (404, 405), (path, response.text)
        assert response.status_code != 401
    assert seam.total == 0


async def test_subdomain_mode_mcp_host_serves_mcp_and_nothing_else(
    subdomain_mode: str, seam: _SeamCounter
) -> None:
    """On the ``mcp`` host every path but ``/`` is a 404 answered by the router,
    ``/auth/*``, ``/api/*`` and ``/healthz`` included: that host is not an
    application host and carries no session or HTTP API."""
    for path in ("/auth/login", "/api/v1/operations/core.workspace.status", "/healthz"):
        response = await _raw(f"https://{MCP_HOST}{path}", host=MCP_HOST)
        assert response.status_code == 404, (path, response.text)
    assert seam.total == 0


async def test_mount_is_published_only_while_the_lifespan_runs(
    path_mode: str,
) -> None:
    """Outside a lifespan there is no mount and the router passes everything to
    FastAPI, which is what keeps ``tests/test_healthz.py`` database-free."""
    assert getattr(app.state, STATE_ATTRIBUTE, None) is None
    async with lifespan(app):
        mount = getattr(app.state, STATE_ATTRIBUTE, None)
        assert isinstance(mount, McpMount)
        assert mount.host is None and mount.path == "/mcp"
    assert getattr(app.state, STATE_ATTRIBUTE, None) is None
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=_origin(path_mode)
    ) as http_client:
        response = await http_client.post(
            path_mode, json=_list_body(), headers=_MCP_HEADERS
        )
    assert response.status_code == 404


@pytest.fixture(autouse=True)
def _no_mount_leaks() -> Iterator[None]:
    """A failed test must not leave a mount on ``app.state`` for the next one."""
    yield
    setattr(app.state, STATE_ATTRIBUTE, None)


# --- stateless: no session to steal ------------------------------------------------


async def test_no_session_id_is_issued_and_anothers_gains_nothing(
    path_mode: str,
    cluster: ClusterSession,
    workspace: UUID,
    owner: WorkspaceContext,
    seam: _SeamCounter,
) -> None:
    """The façade is stateless, so the SDK issues no ``Mcp-Session-Id`` and a session
    id presented by another client binds nothing.

    The owner initializes and gets no id back. A member then presents the owner's
    id, or a made-up one when none was issued, with the member's own token: the
    reply is the member's own listing, no ``audit_list``, so carrying another
    client's id conferred none of that client's authority. ``DELETE`` with that id
    is 405: there is no session to terminate.
    """
    owner_value = _mint_mcp(owner)
    member_id = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="member-two"
    )
    member = context_for_harness(workspace, member_id, Role.MEMBER)
    assert isinstance(member, WorkspaceContext), member
    member_value = _mint_mcp(member)
    fallback = uuid.uuid4().hex
    initialize = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    async with lifespan(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=_origin(path_mode)
        ) as http_client:
            opened = await http_client.post(
                path_mode,
                json=initialize,
                headers={**_MCP_HEADERS, "Authorization": f"Bearer {owner_value}"},
            )
            # The id the owner's client was given, if any, is what an attacker holding
            # another client's session would present; a stateless server gives none,
            # so a made-up one stands in for it.
            stolen = opened.headers.get("mcp-session-id", fallback)
            owner_list = await http_client.post(
                path_mode,
                json=_list_body(),
                headers={**_MCP_HEADERS, "Authorization": f"Bearer {owner_value}"},
            )
            as_member = await http_client.post(
                path_mode,
                json=_list_body(),
                headers={
                    **_MCP_HEADERS,
                    "Authorization": f"Bearer {member_value}",
                    "Mcp-Session-Id": stolen,
                },
            )
            deleted = await http_client.delete(
                path_mode,
                headers={
                    **_MCP_HEADERS,
                    "Authorization": f"Bearer {member_value}",
                    "Mcp-Session-Id": stolen,
                },
            )
    assert opened.status_code == 200, opened.text
    assert "mcp-session-id" not in opened.headers
    assert "audit_list" in _listed_names(owner_list)
    assert "mcp-session-id" not in as_member.headers
    member_names = _listed_names(as_member)
    assert "audit_list" not in member_names
    assert "workspace_status" in member_names
    assert deleted.status_code == 405
    assert deleted.headers["allow"] == "POST"


async def test_a_get_stream_is_not_offered(
    path_mode: str, owner: WorkspaceContext
) -> None:
    """No ``GET`` event stream: 405, so no long-lived stream holds shutdown open."""
    response = await _raw(path_mode, bearer=_mint_mcp(owner), method="GET")
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


# --- a runtime run's token, and a token revoked mid-use ----------------------------


async def test_a_runtime_token_calls_a_tool_through_the_mount(
    path_mode: str,
    owner: WorkspaceContext,
    workspace: UUID,
    owner_account_id: UUID,
    seam: _SeamCounter,
) -> None:
    """The issue's acceptance: a runtime run can call a tool end to end. The token is
    minted by ``issue_runtime_token``, the function the runtime itself calls, with the
    snapshot a run naming ``workspace_status`` and ``operations_get`` would get, and
    the client connects to ``url_for(MCP, "/")``, which is the URL the run is given.
    The listing is exactly the snapshot's tools: a run sees what it asked for."""
    _, value = issue_runtime_token(
        account_id=owner_account_id,
        workspace_id=workspace,
        purpose=ContextPurpose.INTERNAL_ANALYSIS.value,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        operations=[WORKSPACE_STATUS, OPERATION_GET],
    )
    async for client in _client(path_mode, value):
        listed = await client.list_tools()
        result = await client.call_tool("workspace_status", {})
    assert sorted(tool.name for tool in listed.tools) == [
        "operations_get",
        "workspace_status",
    ]
    assert result.is_error is False
    assert isinstance(result.structured_content, dict)
    direct = dispatch(owner, WORKSPACE_STATUS, {})
    assert direct.result is not None
    assert result.structured_content["result"] == direct.result.model_dump(mode="json")
    assert seam.call_tool == 1


async def test_a_token_revoked_mid_use_is_refused_on_its_next_request(
    path_mode: str, owner: WorkspaceContext, seam: _SeamCounter
) -> None:
    """Stateless, every request resolves the bearer again, so a revocation lands on
    the very next request of a client that was working a moment before."""
    value, token_id = _mint(owner)
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {value}"}
    async with lifespan(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=_origin(path_mode)
        ) as http_client:
            before = await http_client.post(
                path_mode, json=_list_body(), headers=headers
            )
            revoked = dispatch(owner, TOKEN_REVOKE, {"token_id": str(token_id)})
            assert revoked.ok, revoked
            entered = seam.total
            after = await http_client.post(
                path_mode, json=_list_body(), headers=headers
            )
    assert "workspace_status" in _listed_names(before)
    assert after.status_code == 401, after.text
    assert after.json()["state"] == TOKEN_REVOKED
    assert seam.total == entered


# --- paths and hosts that are not the surface --------------------------------------


async def test_a_longer_path_under_the_prefix_falls_through_to_fastapi(
    path_mode: str, owner: WorkspaceContext, seam: _SeamCounter
) -> None:
    """``/mcp/x`` is not the surface: FastAPI's 404 with or without a bearer. The
    gate's 401 for the bearerless request would mean MCP had been reached."""
    base = path_mode.rstrip("/")
    for bearer in (None, _mint_mcp(owner)):
        response = await _raw(f"{base}/x", bearer=bearer)
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": "Not Found"}
    assert seam.total == 0


@pytest.mark.parametrize("host", [f"{MCP_HOST}.", MCP_HOST.upper(), f"{MCP_HOST}.:443"])
async def test_trailing_dot_and_uppercase_mcp_hosts_are_the_mcp_host(
    subdomain_mode: str, seam: _SeamCounter, host: str
) -> None:
    """The same name spelled with a trailing dot or in capitals is still the ``mcp``
    host: its other paths are the router's 404, not FastAPI's ``/auth`` or ``/api``,
    and ``/`` reaches the bearer gate (401), not FastAPI."""
    for path in ("/auth/login", "/api/v1/operations/core.workspace.status", "/healthz"):
        response = await _raw(f"https://{MCP_HOST}{path}", host=host)
        assert response.status_code == 404, (host, path, response.text)
    gate = await _raw(f"https://{MCP_HOST}/", host=host)
    assert gate.status_code == 401, gate.text
    assert gate.json()["state"] == TOKEN_MALFORMED
    assert seam.total == 0


# --- loopback: any port, as the SDK's own default ----------------------------------


async def test_a_loopback_deployment_admits_any_loopback_port(
    monkeypatch: pytest.MonkeyPatch, owner: WorkspaceContext, seam: _SeamCounter
) -> None:
    """``base_host = localhost`` (the local demo's value): a client on
    ``localhost:8000`` works without ``public_host`` naming the port, a loopback
    browser origin is admitted, and a foreign ``Host`` or ``Origin`` is still
    refused."""
    monkeypatch.setenv("RHEO__routing__mode", "path")
    monkeypatch.setenv("RHEO__routing__scheme", "http")
    monkeypatch.setenv("RHEO__routing__base_host", "localhost")
    monkeypatch.setenv("RHEO__routing__public_host", "")
    url = "http://localhost:8000/mcp/"
    value = _mint_mcp(owner)
    async for client in _client(url, value):
        listed = await client.list_tools()
    assert "workspace_status" in [tool.name for tool in listed.tools]

    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {value}"}
    async with lifespan(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8000"
        ) as http_client:
            local_origin = await http_client.post(
                url,
                json=_list_body(),
                headers={**headers, "Origin": "http://localhost:3000"},
            )
            entered = seam.total
            foreign_host = await http_client.post(
                url,
                json=_list_body(),
                headers={**headers, "Host": "attacker.example.org:8000"},
            )
            foreign_origin = await http_client.post(
                url,
                json=_list_body(),
                headers={**headers, "Origin": "http://attacker.example.org"},
            )
    assert "workspace_status" in _listed_names(local_origin)
    assert foreign_host.status_code == 421, foreign_host.text
    assert foreign_origin.status_code == 403, foreign_origin.text
    assert seam.total == entered


# --- the audit opt-ins are closed to model-held tokens on every surface ------------


async def test_an_mcp_token_is_refused_by_the_api_surface(
    owner: WorkspaceContext,
) -> None:
    """Issue #130 (Robin's decision): an ``mcp`` token works on the ``mcp`` surface
    only. Over ``POST /api/v1/operations/*`` it is refused at the surface with the
    wrong-kind state, before any operation runs, so a model's token can never read an
    operation's raw output past the tool facade's redaction; this is also what now
    keeps ``core.audit.list``'s two opt-ins off HTTP for it. The owner's ``cli`` token
    is the control and keeps both."""
    mcp_value, _ = _mint(owner)
    cli_value, _ = _mint(owner, kind="cli", set_name="cli_full")
    route = f"/api/v1/operations/{AUDIT_LIST}"
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=public_app), base_url="http://test"
    ) as http_client:
        mcp_headers = {"Authorization": f"Bearer {mcp_value}"}
        refused = [
            await http_client.post(route, headers=mcp_headers, json=payload)
            for payload in (
                {"limit": 5},
                {"include_tool_telemetry": True},
                {"include_deletions": True},
            )
        ]
        control = await http_client.post(
            route,
            headers={"Authorization": f"Bearer {cli_value}"},
            json={"include_tool_telemetry": True, "include_deletions": True},
        )
    for response in refused:
        assert response.status_code == 401, response.text
        body = response.json()
        assert body["state"] == TOKEN_WRONG_KIND
        assert "result" not in body
    assert control.status_code == 200, control.text
    assert control.json()["result"]["deletions"] is not None
    assert control.json()["result"]["tool_telemetry"] is not None


def test_a_runtime_token_cannot_open_the_audit_opt_ins(
    workspace: UUID, owner_account_id: UUID
) -> None:
    """The same rule for the other model-held kind, whose only surface is ``mcp``:
    dispatched on the context the transport's gate would build for it."""
    _, value = issue_runtime_token(
        account_id=owner_account_id,
        workspace_id=workspace,
        purpose=ContextPurpose.INTERNAL_ANALYSIS.value,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        operations=[AUDIT_LIST],
    )
    ctx = context_from_token(value, "mcp")
    assert isinstance(ctx, WorkspaceContext), ctx
    assert dispatch(ctx, AUDIT_LIST, {}).ok
    for flag in ("include_tool_telemetry", "include_deletions"):
        refused = dispatch(ctx, AUDIT_LIST, {flag: True})
        assert refused.state == OPERATION_NOT_PERMITTED, refused
