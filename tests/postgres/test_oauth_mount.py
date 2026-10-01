"""The OAuth connector's half of the ``mcp`` surface (issue #287): AC-1, AC-2, AC-3,
AC-21's ``mcp``-host half and edge case 12.

Seams under test: ``rheo_app_core.main.app`` with its real lifespan, so
:class:`~rheo_app_core.mcp_mount.McpSurfaceRouter` and the bearer gate behind it, in
both routing modes, with the feature configured through the environment a deployment
uses (``RHEO__identity__oauth__*``) and left unconfigured.

**Expected URLs are never typed out here.** Each test resolves ``oauth_surface`` from
the same settings and routing the lifespan reads, and asserts the wire against that
object's URLs; the metadata paths are the paths of those URLs. A mount that served a
hand-written path, or a gate that pointed at one, would disagree with the surface in
one of the two modes.
"""

import contextlib
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx2
import pytest
from conftest import ClusterSession
from harness.registry import register_harness
from rheo_app_core.main import app, lifespan
from rheo_contracts import WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.boundary.context import TOKEN_EXPIRED, TOKEN_MALFORMED, TOKEN_REVOKED
from rheo_core.oauth import OAuthSurface, OAuthUnconfigured, oauth_surface
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.core_ops import TOKEN_ISSUE, TOKEN_REVOKE, WORKSPACE_STATUS
from rheo_core.operations.tool_facade import TOOL_NOT_FOUND
from rheo_core.routing import MCP, RoutingConfig, url_for
from rheo_core.settings import resolve
from rheo_core.storage import control_tables as t
from rheo_core.storage.control_plane import (
    insert_access_token,
    insert_access_token_operations,
)
from rheo_core.tokens.format import mint
from rheo_core.tokens.sets import register_core_tools
from sqlalchemy import update

pytestmark = pytest.mark.postgres

BASE_HOST = "example.test"
SHELL_HOST = f"circuit.{BASE_HOST}"
MODES = ("subdomain", "path")
_FAR_PAST = datetime(2000, 1, 1, tzinfo=UTC)
_FAR_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)

AS_DOCUMENT_KEYS = frozenset(
    {
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "response_types_supported",
        "grant_types_supported",
        "code_challenge_methods_supported",
        "token_endpoint_auth_methods_supported",
        "authorization_response_iss_parameter_supported",
    }
)
"""FR 4's field set, spelled out so the served document is compared to the
requirement, not to the method that builds it."""

_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_core_tools()
    register_harness()


@pytest.fixture
def operator_ctx(workspace: UUID) -> WorkspaceContext:
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> str:
    """The routing topology in the environment; returns the runtime's MCP URL."""
    monkeypatch.setenv("RHEO__routing__mode", mode)
    monkeypatch.setenv("RHEO__routing__scheme", "https")
    monkeypatch.setenv("RHEO__routing__base_host", BASE_HOST)
    monkeypatch.setenv("RHEO__routing__public_host", "")
    return url_for(RoutingConfig.from_settings(resolve()), MCP, "/")


def _configure(monkeypatch: pytest.MonkeyPatch) -> OAuthSurface:
    """Turn the feature on the way a deployment does, and return its surface."""
    monkeypatch.setenv("RHEO__identity__oauth__enabled", "true")
    monkeypatch.setenv("RHEO__identity__providers__github__enabled", "true")
    monkeypatch.setenv("RHEO__identity__providers__github__client_id", "example-client")
    settings = resolve()
    surface = oauth_surface(settings, RoutingConfig.from_settings(settings))
    assert isinstance(surface, OAuthSurface), surface
    return surface


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _path(url: str) -> str:
    return urlsplit(url).path


@contextlib.asynccontextmanager
async def _wire(url: str) -> AsyncIterator[httpx2.AsyncClient]:
    """An HTTP client at ``url``'s origin, through ``app`` with its lifespan running."""
    async with lifespan(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=_origin(url)
        ) as client:
            yield client


async def _post(
    client: httpx2.AsyncClient,
    url: str,
    *,
    bearer: str | None = None,
    body: dict[str, object] | None = None,
) -> httpx2.Response:
    headers = dict(_MCP_HEADERS)
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    payload = body or {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    return await client.post(url, json=payload, headers=headers)


def _pointer(surface: OAuthSurface) -> str:
    return f'Bearer resource_metadata="{surface.protected_resource_metadata_url}"'


def _issue_mcp(ctx: WorkspaceContext, account_id: UUID) -> tuple[str, UUID]:
    outcome = dispatch(
        ctx,
        TOKEN_ISSUE,
        {"kind": "mcp", "set_name": "agent_default", "account_id": str(account_id)},
    )
    assert outcome.ok, outcome
    assert outcome.result is not None
    return outcome.result.value, outcome.result.token_id  # type: ignore[attr-defined]


# --- AC-1: the pointer on the unauthenticated 401 -----------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_unauthenticated_post_carries_the_resource_metadata_pointer(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    url = _set_mode(monkeypatch, mode)
    surface = _configure(monkeypatch)
    async with _wire(url) as client:
        response = await _post(client, url)
    assert response.status_code == 401
    assert response.json()["state"] == TOKEN_MALFORMED
    assert response.headers["www-authenticate"] == _pointer(surface)
    # The pointer names the document this same deployment serves, on the surface's
    # own origin (the mcp host in subdomain mode, the one host in path mode).
    assert _origin(surface.protected_resource_metadata_url) == _origin(url)


# --- AC-2: the three documents and the unchanged neighbours -------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_configured_mount_serves_the_metadata_documents(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    url = _set_mode(monkeypatch, mode)
    surface = _configure(monkeypatch)
    resource_path = _path(surface.protected_resource_metadata_url)
    as_path = _path(surface.authorization_server_metadata_url)
    async with _wire(url) as client:
        resource_docs = [
            await client.get(resource_path),
            await client.get(f"{resource_path}/"),
        ]
        as_doc = await client.get(as_path)
        refused_methods = [
            await client.post(resource_path, json={}),
            await client.post(f"{resource_path}/", json={}),
            await client.post(as_path, json={}),
            await client.delete(as_path),
        ]
        endpoint_get = await client.get(url, headers=_MCP_HEADERS)

    for response in resource_docs:
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert response.headers["cache-control"] == "no-store"
        document = response.json()
        assert document["resource"] == url == surface.resource
        assert document["authorization_servers"] == [surface.issuer]
        assert len(document["authorization_servers"]) == 1

    assert as_doc.status_code == 200, as_doc.text
    assert as_doc.headers["cache-control"] == "no-store"
    served = as_doc.json()
    assert set(served) == AS_DOCUMENT_KEYS
    assert served["issuer"] == surface.issuer
    assert served["authorization_endpoint"] == surface.authorization_endpoint
    assert served["token_endpoint"] == surface.token_endpoint
    assert served["registration_endpoint"] == surface.registration_endpoint
    assert served["response_types_supported"] == ["code"]
    assert set(served["grant_types_supported"]) >= {
        "authorization_code",
        "refresh_token",
    }
    assert served["code_challenge_methods_supported"] == ["S256"]
    assert served["token_endpoint_auth_methods_supported"] == ["none"]
    assert served["authorization_response_iss_parameter_supported"] is True

    for response in refused_methods:
        assert response.status_code == 405, response.text
        assert response.headers["allow"] == "GET"

    assert endpoint_get.status_code == 405
    assert endpoint_get.headers["allow"] == "POST"


async def test_other_mcp_host_paths_stay_404_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the three paths are served: a near miss on the mcp host is the router's
    404, and the documents are not served on another host in subdomain mode."""
    url = _set_mode(monkeypatch, "subdomain")
    surface = _configure(monkeypatch)
    as_path = _path(surface.authorization_server_metadata_url)
    resource_path = _path(surface.protected_resource_metadata_url)
    async with _wire(url) as client:
        near_misses = [
            await client.get(f"{as_path}/"),
            await client.get(f"{resource_path}/mcp"),
            await client.get("/.well-known/openid-configuration"),
            await client.get("/healthz"),
        ]
        other_host = await client.get(
            f"https://{SHELL_HOST}{resource_path}", headers={"Host": SHELL_HOST}
        )
    for response in near_misses:
        assert response.status_code == 404, response.text
    assert other_host.status_code == 404
    assert "resource" not in other_host.text


@pytest.mark.parametrize("mode", MODES)
async def test_unconfigured_mount_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """AC-2's last sentence and AC-21's mcp-host half: the feature off (the package
    default) leaves both well-known paths 404 and the 401 a bare ``Bearer``.

    The probe paths are the ones the configured surface would serve, resolved
    before the feature is switched back off, so the 404 is for the right paths.
    """
    url = _set_mode(monkeypatch, mode)
    with monkeypatch.context() as configured:
        surface = _configure(configured)
    settings = resolve()
    off = oauth_surface(settings, RoutingConfig.from_settings(settings))
    assert isinstance(off, OAuthUnconfigured), off
    probes = [
        _path(surface.protected_resource_metadata_url),
        f"{_path(surface.protected_resource_metadata_url)}/",
        _path(surface.authorization_server_metadata_url),
    ]
    async with _wire(url) as client:
        responses = [await client.get(path) for path in probes]
        refusal = await _post(client, url)
    for response in responses:
        assert response.status_code == 404, response.text
    assert refusal.status_code == 401
    assert refusal.headers["www-authenticate"] == "Bearer"


# --- AC-3: refusals keep their states and gain the pointer --------------------------


async def test_bad_tokens_keep_their_states_and_carry_the_pointer(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    operator_ctx: WorkspaceContext,
    owner_account_id: UUID,
) -> None:
    url = _set_mode(monkeypatch, "subdomain")
    surface = _configure(monkeypatch)
    expired, expired_id = _issue_mcp(operator_ctx, owner_account_id)
    with cluster.backend.control_engine.begin() as connection:
        connection.execute(
            update(t.access_token)
            .where(t.access_token.c.id == expired_id)
            .values(expires_at=_FAR_PAST)
        )
    revoked, revoked_id = _issue_mcp(operator_ctx, owner_account_id)
    assert dispatch(operator_ctx, TOKEN_REVOKE, {"token_id": str(revoked_id)}).ok

    cases = [
        ("not-a-real-token", TOKEN_MALFORMED),
        ("rheo_mcp_" + "a" * 43, TOKEN_MALFORMED),
        (expired, TOKEN_EXPIRED),
        (revoked, TOKEN_REVOKED),
    ]
    async with _wire(url) as client:
        responses = [await _post(client, url, bearer=value) for value, _ in cases]
    for response, (_, state) in zip(responses, cases, strict=True):
        assert response.status_code == 401
        assert response.json()["state"] == state
        assert response.headers["www-authenticate"] == _pointer(surface)


def _token_with(
    cluster: ClusterSession, account_id: UUID, workspace: UUID, operations: list[str]
) -> str:
    """A live ``mcp`` token whose snapshot is exactly ``operations``."""
    value, raw = mint("mcp")
    with cluster.backend.control_engine.begin() as connection:
        row = insert_access_token(
            connection,
            account_id=account_id,
            workspace_id=workspace,
            kind="mcp",
            issued_from="operator",
            token_hash=hashlib.sha256(raw).digest(),
            set_name=None,
            purpose=None,
            expires_at=_FAR_FUTURE,
        )
        insert_access_token_operations(
            connection, token_id=row.id, operation_names=operations
        )
    return value


async def _call(url: str, bearer: str, tool: str) -> httpx2.Response:
    async with _wire(url) as client:
        return await _post(
            client,
            url,
            bearer=bearer,
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": {}},
            },
        )


async def test_a_token_lacking_the_operation_gets_the_unchanged_refusal(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """A valid token whose snapshot lacks ``audit_list``'s operation is answered by
    the façade, not the gate: a 200 result in the facade's ``not_found`` state, the
    same answer with the feature off and on, and no challenge either way."""
    url = _set_mode(monkeypatch, "subdomain")
    value = _token_with(cluster, owner_account_id, workspace, [WORKSPACE_STATUS])
    unconfigured = await _call(url, value, "audit_list")
    _configure(monkeypatch)
    configured = await _call(url, value, "audit_list")
    for response in (unconfigured, configured):
        assert response.status_code == 200, response.text
        assert "www-authenticate" not in response.headers
        result = response.json()["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["state"] == TOOL_NOT_FOUND
    assert configured.json()["result"] == unconfigured.json()["result"]


# --- edge case 12: a valid existing bearer is never challenged ----------------------


@pytest.mark.parametrize("mode", MODES)
async def test_a_valid_bearer_is_served_not_challenged(
    monkeypatch: pytest.MonkeyPatch,
    operator_ctx: WorkspaceContext,
    owner_account_id: UUID,
    mode: str,
) -> None:
    url = _set_mode(monkeypatch, mode)
    _configure(monkeypatch)
    value, _ = _issue_mcp(operator_ctx, owner_account_id)
    async with _wire(url) as client:
        response = await _post(client, url, bearer=value)
    assert response.status_code == 200, response.text
    assert "www-authenticate" not in response.headers
    assert "location" not in response.headers
    names = [tool["name"] for tool in response.json()["result"]["tools"]]
    assert "workspace_status" in names
