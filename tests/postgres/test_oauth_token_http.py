"""The OAuth connector's registration and token endpoints on the wire (issue #287):
AC-5's HTTP half, AC-13 and AC-14's wire half, and AC-21's ``/auth/oauth/*`` 404s.

Seams under test: ``POST /auth/oauth/register`` and ``POST /auth/oauth/token`` as a
connector client sees them, through ``rheo_app_core.main.app`` with its real
lifespan (``harness.oauth_client``), with the feature configured the way a
deployment configures it (``RHEO__identity__oauth__*``). The database-state proofs
behind these answers are the service tests'; here the property is the HTTP answer:
status, ``Cache-Control``, and the RFC 6749 s5.2 body.

Codes are seeded through the service's ``begin_authorization`` and ``approve`` (the
browser routes come later), never raw SQL. Every URL comes from the resolved
:class:`~rheo_core.oauth.OAuthSurface`.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.oauth_client import (
    FORM_CONTENT_TYPE,
    OAuthTestClient,
    oauth_client,
)
from harness.registry import register_harness
from rheo_app_core.oauth_routes import MAX_BODY_BYTES
from rheo_core.boundary.context import TOKEN_MALFORMED
from rheo_core.oauth import OAuthSurface, OAuthUnconfigured, oauth_surface
from rheo_core.oauth.service import Approval, approve, begin_authorization, pending
from rheo_core.operations import register_core_operations
from rheo_core.refs import uuid7
from rheo_core.routing import RoutingConfig
from rheo_core.settings import resolve
from rheo_core.storage import oauth_tables as o
from rheo_core.tokens.sets import register_core_tools
from sqlalchemy import func, select

pytestmark = pytest.mark.postgres

BASE_HOST = "example.test"
SHELL_HOST = f"circuit.{BASE_HOST}"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
MODES = ("subdomain", "path")
ERROR_KEYS = {"error", "error_description"}
REGISTRATION_KEYS = {
    "client_id",
    "client_id_issued_at",
    "client_name",
    "redirect_uris",
    "token_endpoint_auth_method",
    "grant_types",
    "response_types",
}


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_core_tools()
    register_harness()


@pytest.fixture
def address() -> str:
    """A fresh client address per test, so the per-source limit is this test's.
    Not an IP literal: the ASGI scope carries it as given."""
    return f"client-{uuid7().hex}"


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("RHEO__routing__mode", mode)
    monkeypatch.setenv("RHEO__routing__scheme", "https")
    monkeypatch.setenv("RHEO__routing__base_host", BASE_HOST)
    monkeypatch.setenv("RHEO__routing__public_host", "")


def _configure(monkeypatch: pytest.MonkeyPatch, **settings: str) -> OAuthSurface:
    """The feature on, as a deployment turns it on. The counted-client cap is raised
    because the control plane is shared with every other test's registrations."""
    monkeypatch.setenv("RHEO__identity__oauth__enabled", "true")
    monkeypatch.setenv("RHEO__identity__providers__github__enabled", "true")
    monkeypatch.setenv("RHEO__identity__providers__github__client_id", "example-client")
    monkeypatch.setenv("RHEO__identity__oauth__max_clients", "1000000")
    for key, value in settings.items():
        monkeypatch.setenv(f"RHEO__identity__oauth__{key}", value)
    resolved = resolve()
    surface = oauth_surface(resolved, RoutingConfig.from_settings(resolved))
    assert isinstance(surface, OAuthSurface), surface
    return surface


@pytest.fixture(params=MODES)
def surface(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> OAuthSurface:
    _set_mode(monkeypatch, request.param)
    return _configure(monkeypatch)


@pytest.fixture
def subdomain_surface(monkeypatch: pytest.MonkeyPatch) -> OAuthSurface:
    _set_mode(monkeypatch, "subdomain")
    return _configure(monkeypatch)


def _no_store(response: object) -> None:
    headers = response.headers  # type: ignore[attr-defined]
    assert headers["cache-control"] == "no-store", headers


def _assert_error(response: object, status: int, error: str) -> None:
    status_code = response.status_code  # type: ignore[attr-defined]
    text = response.text  # type: ignore[attr-defined]
    assert status_code == status, text
    body = response.json()  # type: ignore[attr-defined]
    assert set(body) == ERROR_KEYS, body
    assert body["error"] == error
    assert isinstance(body["error_description"], str) and body["error_description"]
    _no_store(response)


async def _registered(client: OAuthTestClient) -> str:
    response = await client.register(
        {"redirect_uris": [CALLBACK], "client_name": "Example connector"}
    )
    assert response.status_code == 201, response.text
    client_id = response.json()["client_id"]
    assert isinstance(client_id, str)
    return client_id


def _seed_code(
    client: OAuthTestClient, client_id: str, account_id: UUID, workspace_id: UUID
) -> tuple[str, str]:
    """An approved, unredeemed code through the service; returns (code, verifier)."""
    verifier, challenge = client.new_pkce()
    now = datetime.now(UTC)
    nonce = begin_authorization(
        client.surface,
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": client.surface.resource,
            "state": "opaque-state",
        },
        now=now,
    )
    assert isinstance(nonce, str), nonce
    row = pending(nonce, now)
    assert row is not None
    approval = approve(row.id, nonce, account_id, workspace_id, now=now)
    assert isinstance(approval, Approval), approval
    client.record_code(approval.code)
    return approval.code, verifier


# --- registration -------------------------------------------------------------------


async def test_registration_returns_the_effective_public_client(
    surface: OAuthSurface, address: str
) -> None:
    async with oauth_client(surface, client_address=address) as client:
        response = await client.register(
            {
                "redirect_uris": [CALLBACK],
                "client_name": "Example connector",
                "token_endpoint_auth_method": "client_secret_basic",
                "grant_types": ["implicit"],
                "response_types": ["token"],
                "client_secret": "asked-for",
            }
        )
    assert response.status_code == 201, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == REGISTRATION_KEYS
    assert body["redirect_uris"] == [CALLBACK]
    assert body["client_name"] == "Example connector"
    assert body["token_endpoint_auth_method"] == "none"
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    assert body["response_types"] == ["code"]


@pytest.mark.parametrize(
    "redirect_uris",
    [
        ["https://claude.ai/api/mcp/auth_callback/"],
        ["https://other.example.com/api/mcp/auth_callback"],
        ["http://claude.ai/api/mcp/auth_callback"],
        [],
    ],
)
async def test_registration_refuses_a_redirect_uri_off_the_allowlist(
    subdomain_surface: OAuthSurface, address: str, redirect_uris: list[str]
) -> None:
    async with oauth_client(subdomain_surface, client_address=address) as client:
        response = await client.register({"redirect_uris": redirect_uris})
    _assert_error(response, 400, "invalid_redirect_uri")


@pytest.mark.parametrize("body", [b"[]", b"not json", b'{"redirect_uris": ', b"\xff"])
async def test_registration_refuses_a_body_that_is_not_a_json_object(
    subdomain_surface: OAuthSurface, address: str, body: bytes
) -> None:
    async with oauth_client(subdomain_surface, client_address=address) as client:
        response = await client.register_raw(body)
    _assert_error(response, 400, "invalid_client_metadata")


async def test_registration_past_the_per_source_limit_is_429(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    _set_mode(monkeypatch, "subdomain")
    surface = _configure(monkeypatch, registrations_per_source_per_hour="2")
    async with oauth_client(surface, client_address=address) as client:
        first = await _registered(client)
        second = await _registered(client)
        refused = await client.register({"redirect_uris": [CALLBACK]})
        async with oauth_client(surface, client_address=f"{address}-other") as other:
            elsewhere = await other.register({"redirect_uris": [CALLBACK]})
    assert first != second
    _assert_error(refused, 429, "temporarily_unavailable")
    assert elsewhere.status_code == 201, elsewhere.text


# --- the token endpoint's own refusals ----------------------------------------------


@pytest.mark.parametrize(
    "content_type", ["application/json", "multipart/form-data; boundary=x", ""]
)
async def test_token_requires_a_form_content_type(
    subdomain_surface: OAuthSurface, address: str, content_type: str
) -> None:
    async with oauth_client(subdomain_surface, client_address=address) as client:
        response = await client.token(
            body=b"grant_type=refresh_token&refresh_token=x&client_id=y",
            content_type=content_type,
        )
    _assert_error(response, 400, "invalid_request")


async def test_token_accepts_a_charset_parameter_on_the_form_type(
    subdomain_surface: OAuthSurface, address: str
) -> None:
    """The media type is compared, not the whole header: an unknown refresh token
    then gets the service's ``invalid_grant``, not the route's ``invalid_request``."""
    async with oauth_client(subdomain_surface, client_address=address) as client:
        response = await client.token(
            body=b"grant_type=refresh_token&refresh_token=x&client_id=y",
            content_type=f"{FORM_CONTENT_TYPE}; charset=UTF-8",
        )
    _assert_error(response, 400, "invalid_grant")


@pytest.mark.parametrize(
    "body",
    [
        b"grant_type=refresh_token&refresh_token=x&refresh_token=y&client_id=c",
        b"grant_type=refresh_token&grant_type=refresh_token&refresh_token=x",
        b"grant_type",
        b"grant_type=refresh_token&&refresh_token=x",
        b"",
        b"refresh_token=x&client_id=c",
        b"grant_type=refresh_token&refresh_token=%FF",
    ],
)
async def test_token_refuses_duplicated_or_malformed_parameters(
    subdomain_surface: OAuthSurface, address: str, body: bytes
) -> None:
    async with oauth_client(subdomain_surface, client_address=address) as client:
        response = await client.token(body=body)
    _assert_error(response, 400, "invalid_request")


@pytest.mark.parametrize("grant_type", ["password", "client_credentials", ""])
async def test_token_refuses_an_unsupported_grant_type(
    subdomain_surface: OAuthSurface, address: str, grant_type: str
) -> None:
    async with oauth_client(subdomain_surface, client_address=address) as client:
        response = await client.token({"grant_type": grant_type})
    _assert_error(response, 400, "unsupported_grant_type")


async def test_unknown_code_is_invalid_grant(
    subdomain_surface: OAuthSurface, address: str
) -> None:
    async with oauth_client(subdomain_surface, client_address=address) as client:
        client_id = await _registered(client)
        response = await client.redeem(
            "not-a-code", client_id, CALLBACK, client.new_pkce()[0]
        )
    _assert_error(response, 400, "invalid_grant")


async def test_another_client_redeeming_the_code_is_401_invalid_client(
    subdomain_surface: OAuthSurface,
    address: str,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    async with oauth_client(subdomain_surface, client_address=address) as client:
        client_id = await _registered(client)
        other_id = await _registered(client)
        code, verifier = _seed_code(client, client_id, owner_account_id, workspace)
        response = await client.redeem(code, other_id, CALLBACK, verifier)
    _assert_error(response, 401, "invalid_client")


async def test_a_code_in_the_query_string_is_never_read(
    subdomain_surface: OAuthSurface,
    address: str,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """The query is ignored, so the request is the empty body's ``invalid_request``
    and the code is not burned: the same code still redeems from a body."""
    async with oauth_client(subdomain_surface, client_address=address) as client:
        client_id = await _registered(client)
        code, verifier = _seed_code(client, client_id, owner_account_id, workspace)
        query = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "redirect_uri": CALLBACK,
                "code_verifier": verifier,
            }
        )
        in_query = await client.token(
            body=b"", url=f"{subdomain_surface.token_endpoint}?{query}"
        )
        in_body = await client.redeem(code, client_id, CALLBACK, verifier)
    _assert_error(in_query, 400, "invalid_request")
    assert in_body.status_code == 200, in_body.text


# --- the loop: register, redeem, call, refresh --------------------------------------


async def test_register_redeem_call_refresh_over_http(
    surface: OAuthSurface, address: str, workspace: UUID, owner_account_id: UUID
) -> None:
    async with oauth_client(surface, client_address=address) as client:
        client_id = await _registered(client)
        code, verifier = _seed_code(client, client_id, owner_account_id, workspace)
        redeemed = await client.redeem(code, client_id, CALLBACK, verifier)
        assert redeemed.status_code == 200, redeemed.text
        first = redeemed.json()
        listed = await client.mcp_call(first["access_token"])
        refreshed = await client.refresh(first["refresh_token"], client_id)
        assert refreshed.status_code == 200, refreshed.text
        second = refreshed.json()
        superseded = await client.mcp_call(first["access_token"])
        current = await client.mcp_call(second["access_token"])
        replayed = await client.redeem(code, client_id, CALLBACK, verifier)

    _no_store(redeemed)
    _no_store(refreshed)
    for body in (first, second):
        assert set(body) == {
            "access_token",
            "token_type",
            "expires_in",
            "refresh_token",
        }
        assert body["token_type"] == "Bearer"
        assert body["access_token"].startswith("rheo_mcp_")
        assert isinstance(body["expires_in"], int) and body["expires_in"] > 0
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]

    assert listed.status_code == 200, listed.text
    names = [tool["name"] for tool in listed.json()["result"]["tools"]]
    assert "workspace_status" in names

    assert superseded.status_code == 401
    assert superseded.json()["state"] == TOKEN_MALFORMED
    assert (
        superseded.headers["www-authenticate"]
        == f'Bearer resource_metadata="{surface.protected_resource_metadata_url}"'
    )
    assert current.status_code == 200, current.text

    _assert_error(replayed, 400, "invalid_grant")


# --- 404 unless configured and on the identity host ---------------------------------


@pytest.fixture
def unconfigured(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[OAuthSurface]:
    """The surface the feature would advertise, with the feature then left off."""
    _set_mode(monkeypatch, request.param)
    with monkeypatch.context() as on:
        surface = _configure(on)
    settings = resolve()
    off = oauth_surface(settings, RoutingConfig.from_settings(settings))
    assert isinstance(off, OAuthUnconfigured), off
    yield surface


@pytest.mark.parametrize("unconfigured", MODES, indirect=True)
async def test_unconfigured_routes_are_404(
    unconfigured: OAuthSurface, address: str
) -> None:
    async with oauth_client(unconfigured, client_address=address) as client:
        registered = await client.register({"redirect_uris": [CALLBACK]})
        token = await client.token({"grant_type": "refresh_token"})
    assert registered.status_code == 404, registered.text
    assert token.status_code == 404, token.text


@pytest.mark.parametrize("host", [SHELL_HOST, f"mcp.{BASE_HOST}"])
async def test_routes_on_another_host_are_404(
    subdomain_surface: OAuthSurface, address: str, host: str
) -> None:
    def elsewhere(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{host}{parts.path}"

    async with oauth_client(subdomain_surface, client_address=address) as client:
        registered = await client.http.post(
            elsewhere(subdomain_surface.registration_endpoint),
            json={"redirect_uris": [CALLBACK]},
        )
        token = await client.token(
            {"grant_type": "refresh_token"},
            url=elsewhere(subdomain_surface.token_endpoint),
        )
    assert registered.status_code == 404, registered.text
    assert token.status_code == 404, token.text


# --- bounded bodies -----------------------------------------------------------------


async def _chunked(body: bytes) -> AsyncIterator[bytes]:
    """``body`` as a stream with no ``Content-Length`` (chunked on the wire)."""
    for start in range(0, len(body), 4096):
        yield body[start : start + 4096]


def _refused_registrations(cluster: ClusterSession) -> int:
    with cluster.backend.control_engine.connect() as connection:
        return connection.execute(
            select(func.count())
            .select_from(o.oauth_event)
            .where(
                o.oauth_event.c.event == "client_registered",
                o.oauth_event.c.outcome == "refused",
                o.oauth_event.c.client_id.is_(None),
            )
        ).scalar_one()


def _oversized_form() -> bytes:
    body = b"grant_type=refresh_token&client_id=c&refresh_token="
    return body + b"x" * (MAX_BODY_BYTES + 1 - len(body))


@pytest.mark.parametrize("declared", [True, False])
async def test_token_refuses_a_body_over_the_limit(
    subdomain_surface: OAuthSurface, address: str, declared: bool
) -> None:
    body = _oversized_form()
    assert len(body) == MAX_BODY_BYTES + 1
    async with oauth_client(subdomain_surface, client_address=address) as client:
        response = await client.http.post(
            subdomain_surface.token_endpoint,
            content=body if declared else _chunked(body),
            headers={"Content-Type": FORM_CONTENT_TYPE},
        )
    assert ("content-length" in response.request.headers) is declared
    _assert_error(response, 400, "invalid_request")


async def test_token_reads_a_body_at_the_limit(
    subdomain_surface: OAuthSurface, address: str
) -> None:
    """Exactly ``MAX_BODY_BYTES`` is parsed: the unknown refresh token then gets
    the service's ``invalid_grant``, not the size refusal."""
    body = _oversized_form()[:-1]
    async with oauth_client(subdomain_surface, client_address=address) as client:
        response = await client.http.post(
            subdomain_surface.token_endpoint,
            content=_chunked(body),
            headers={"Content-Type": FORM_CONTENT_TYPE},
        )
    _assert_error(response, 400, "invalid_grant")


@pytest.mark.parametrize("declared", [True, False])
async def test_registration_refuses_a_body_over_the_limit_and_records_it(
    subdomain_surface: OAuthSurface,
    cluster: ClusterSession,
    address: str,
    declared: bool,
) -> None:
    prefix = b'{"redirect_uris": ["' + CALLBACK.encode() + b'"], "client_name": "'
    body = prefix + b"x" * (MAX_BODY_BYTES - len(prefix)) + b'"}'
    assert len(body) > MAX_BODY_BYTES
    before = _refused_registrations(cluster)
    async with oauth_client(subdomain_surface, client_address=address) as client:
        response = await client.http.post(
            subdomain_surface.registration_endpoint,
            content=body if declared else _chunked(body),
            headers={"Content-Type": "application/json"},
        )
    _assert_error(response, 400, "invalid_client_metadata")
    assert _refused_registrations(cluster) == before + 1
