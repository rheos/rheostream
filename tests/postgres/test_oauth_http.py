"""The OAuth connector's browser routes on the wire (issue #287): AC-7, AC-8, AC-9,
AC-10's HTTP half and AC-26's HTTP matrix.

Seams under test: ``GET /auth/oauth/authorize`` and ``GET``/``POST
/auth/oauth/consent`` and the consent page as a browser sees them, through
``rheo_app_core.main.app`` with its real lifespan (``harness.oauth_client``), the
existing ``/auth/login`` and ``/auth/callback`` hop driven by
``harness.identity.FixedIdentityProvider`` (overridden through
``auth_routes.resolve_identity_provider`` as the session tests do). Every URL comes
from the resolved :class:`~rheo_core.oauth.OAuthSurface` or ``url_for``.

After every refusal the database is re-read from a fresh connection: the
authorization row is unchanged, no code was issued, no ``access_token`` or
``oauth_grant`` row was created and no second decision event was written. The
routes' clock is pinned through ``oauth_routes._now``.
"""

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx2
import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.identity import FIXED_PROVIDER_ID, FixedIdentityProvider
from harness.oauth_client import (
    CLAUDE_CALLBACK,
    OAuthTestClient,
    consent_fields,
    consent_options,
    oauth_client,
    redirect_query,
)
from harness.registry import register_harness
from rheo_app_core import auth_routes, oauth_routes
from rheo_app_core.main import app
from rheo_contracts import Role
from rheo_core.identity.boundary import ProviderIdentity
from rheo_core.oauth import OAuthSurface, oauth_surface
from rheo_core.oauth.service import grant_days
from rheo_core.operations import register_core_operations
from rheo_core.refs import uuid7
from rheo_core.routing import IDENTITY, RoutingConfig, identity_path, url_for
from rheo_core.sessions.cookies import OAUTH_REQUEST_COOKIE
from rheo_core.settings import resolve
from rheo_core.storage import control_tables
from rheo_core.storage import oauth_tables as o
from rheo_core.storage.control_plane import (
    get_workspace,
    insert_account,
    insert_identity,
    insert_membership_if_absent,
)
from rheo_core.tokens.issue import connector_operations
from rheo_core.tokens.sets import register_core_tools
from sqlalchemy import func, select

pytestmark = pytest.mark.postgres

BASE_HOST = "example.test"
MODES = ("subdomain", "path")
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
FOREIGN_ORIGIN = "https://evil.example.net"


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_core_tools()
    register_harness()


@pytest.fixture
def address() -> str:
    return f"client-{uuid7().hex}"


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("RHEO__routing__mode", mode)
    monkeypatch.setenv("RHEO__routing__scheme", "https")
    monkeypatch.setenv("RHEO__routing__base_host", BASE_HOST)
    monkeypatch.setenv("RHEO__routing__public_host", "")


def _configure(monkeypatch: pytest.MonkeyPatch) -> OAuthSurface:
    monkeypatch.setenv("RHEO__identity__oauth__enabled", "true")
    monkeypatch.setenv("RHEO__identity__providers__github__enabled", "true")
    monkeypatch.setenv("RHEO__identity__providers__github__client_id", "example-client")
    monkeypatch.setenv("RHEO__identity__oauth__max_clients", "1000000")
    resolved = resolve()
    surface = oauth_surface(resolved, RoutingConfig.from_settings(resolved))
    assert isinstance(surface, OAuthSurface), surface
    return surface


def _routing() -> RoutingConfig:
    return RoutingConfig.from_settings(resolve())


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


@pytest.fixture(autouse=True)
def _no_provider_override() -> Iterator[None]:
    yield
    app.dependency_overrides.pop(auth_routes.resolve_identity_provider, None)


def _sign_in_as(identity: ProviderIdentity) -> None:
    """Every ``/auth/login`` and ``/auth/callback`` in this test signs in as
    ``identity`` (a zero-argument lambda, so FastAPI does not introspect the
    class's own parameters)."""
    app.dependency_overrides[auth_routes.resolve_identity_provider] = lambda: (
        FixedIdentityProvider(identity)
    )


@dataclass(frozen=True)
class Member:
    account_id: UUID
    display_name: str
    identity: ProviderIdentity
    workspaces: tuple[UUID, ...]


def _account_with_identity(
    cluster: ClusterSession,
) -> tuple[UUID, str, ProviderIdentity]:
    subject = f"member-{uuid7().hex[:12]}"
    identity = ProviderIdentity(
        provider_id=FIXED_PROVIDER_ID,
        subject=subject,
        email=f"{subject}@example.test",
        email_verified=True,
        display_name=subject,
    )
    with cluster.backend.control_engine.begin() as connection:
        account_id = insert_account(connection, display_name=subject).id
        insert_identity(
            connection,
            account_id=account_id,
            provider_id=identity.provider_id,
            provider_subject=identity.subject,
            email=identity.email,
            email_verified=identity.email_verified,
        )
    return account_id, subject, identity


@pytest.fixture
def make_member(cluster: ClusterSession, make_workspace: MakeWorkspace) -> Any:
    def _make(workspaces: int = 1) -> Member:
        account_id, name, identity = _account_with_identity(cluster)
        owned = tuple(make_workspace(owner=account_id) for _ in range(workspaces))
        return Member(account_id, name, identity, owned)

    return _make


@pytest.fixture
def member(make_member: Any) -> Member:
    found: Member = make_member(1)
    return found


async def _registered(client: OAuthTestClient, name: str = "Example connector") -> str:
    response = await client.register(
        {"redirect_uris": [CLAUDE_CALLBACK], "client_name": name}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["client_id"])


# --- database re-reads (fresh connection every time) ---------------------------------


def _nonce_of(response: httpx2.Response) -> str:
    headers = [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{OAUTH_REQUEST_COOKIE}=")
    ]
    assert len(headers) == 1, headers
    return headers[0].split(";", 1)[0].partition("=")[2]


def _row(cluster: ClusterSession, nonce: str) -> dict[str, Any]:
    with cluster.backend.control_engine.connect() as connection:
        found = (
            connection.execute(
                select(o.oauth_authorization).where(
                    o.oauth_authorization.c.request_hash
                    == hashlib.sha256(nonce.encode()).digest()
                )
            )
            .mappings()
            .one()
        )
    return dict(found)


def _issued(
    cluster: ClusterSession, account_id: UUID, client_id: str
) -> tuple[int, int]:
    """``(access_token rows for the account, oauth_grant rows for the client)``."""
    with cluster.backend.control_engine.connect() as connection:
        tokens = connection.execute(
            select(func.count())
            .select_from(control_tables.access_token)
            .where(control_tables.access_token.c.account_id == account_id)
        ).scalar_one()
        grants = connection.execute(
            select(func.count())
            .select_from(o.oauth_grant)
            .where(o.oauth_grant.c.client_id == client_id)
        ).scalar_one()
    return int(tokens), int(grants)


def _events(cluster: ClusterSession, client_id: str) -> list[tuple[str, str]]:
    with cluster.backend.control_engine.connect() as connection:
        rows = connection.execute(
            select(o.oauth_event.c.event, o.oauth_event.c.outcome)
            .where(o.oauth_event.c.client_id == client_id)
            .order_by(o.oauth_event.c.occurred_at, o.oauth_event.c.id)
        ).all()
    return [(str(event), str(outcome)) for event, outcome in rows]


@dataclass(frozen=True)
class Snapshot:
    row: dict[str, Any]
    issued: tuple[int, int]
    events: list[tuple[str, str]]


def _snapshot(
    cluster: ClusterSession, nonce: str, account_id: UUID, client_id: str
) -> Snapshot:
    return Snapshot(
        _row(cluster, nonce),
        _issued(cluster, account_id, client_id),
        _events(cluster, client_id),
    )


def _assert_undecided_and_nothing_issued(snapshot: Snapshot) -> None:
    assert snapshot.row["decided_at"] is None
    assert snapshot.row["code_hash"] is None
    assert snapshot.row["code_expires_at"] is None
    assert snapshot.issued == (0, 0)


# --- response assertions --------------------------------------------------------------


def _assert_page_headers(response: httpx2.Response) -> None:
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("text/html")


def _assert_error_page(response: httpx2.Response, status: int, state: str) -> None:
    assert response.status_code == status, response.text
    assert "location" not in response.headers
    _assert_page_headers(response)
    assert f'<body data-state="{state}">' in response.text
    assert f"<small>Error code: {state}</small>" in response.text


def _assert_redirect_error(
    response: httpx2.Response,
    surface: OAuthSurface,
    error: str,
    state: str | None = "opaque-state",
) -> None:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"{CLAUDE_CALLBACK}?")
    query = redirect_query(response)
    expected = {"error": error, "iss": surface.issuer}
    if state is not None:
        expected["state"] = state
    assert query == expected


async def _fresh_consent(
    client: OAuthTestClient, member: Member, client_id: str
) -> tuple[httpx2.Response, str]:
    """One authorization, followed to the consent page; returns (page, nonce)."""
    _sign_in_as(member.identity)
    authorized = await client.authorize(client.authorize_params(client_id))
    assert authorized.status_code == 303, authorized.text
    nonce = _nonce_of(authorized)
    page = await client.follow_chain(authorized)
    assert page.status_code == 200, page.text
    return page, nonce


def _posted(page: httpx2.Response) -> dict[str, str]:
    """The page's ``request_id`` and ``account_id``, as the browser posts them."""
    fields = consent_fields(page.text)
    return {"request_id": fields["request_id"], "account_id": fields["account_id"]}


def _pin_clock(monkeypatch: pytest.MonkeyPatch, at: datetime) -> None:
    monkeypatch.setattr(oauth_routes, "_now", lambda: at)


def _replay_cookie(client: OAuthTestClient, nonce: str) -> None:
    """Put ``nonce`` back in the browser's jar (replacing a cleared value)."""
    host = client.identity_origin.split("://", 1)[1]
    client.http.cookies.set(OAUTH_REQUEST_COOKIE, nonce, domain=host)


# --- AC-7: refusals before and after the redirect URI is trusted ---------------------


async def test_an_unknown_client_or_unregistered_redirect_uri_is_a_page(
    surface: OAuthSurface, address: str
) -> None:
    async with oauth_client(surface, client_address=address) as client:
        client_id = await _registered(client)
        unknown = await client.authorize(client.authorize_params("no-such-client"))
        wrong_uri = await client.authorize(
            client.authorize_params(
                client_id, redirect_uri="https://other.example.com/callback"
            )
        )
    _assert_error_page(unknown, 400, "invalid_client")
    _assert_error_page(wrong_uri, 400, "invalid_redirect_uri")
    for response in (unknown, wrong_uri):
        assert OAUTH_REQUEST_COOKIE not in response.headers.get("set-cookie", "")


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"code_challenge_method": None}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge": None}, "invalid_request"),
        ({"resource": None}, "invalid_target"),
        ({"resource": "https://other.example.com/"}, "invalid_target"),
    ],
)
async def test_a_bad_pkce_or_resource_redirects_with_error_state_and_iss(
    surface: OAuthSurface,
    address: str,
    overrides: dict[str, str | None],
    error: str,
) -> None:
    async with oauth_client(surface, client_address=address) as client:
        client_id = await _registered(client)
        params = client.authorize_params(client_id)
        for key, value in overrides.items():
            if value is None:
                params.pop(key)
            else:
                params[key] = value
        response = await client.authorize(params)
    _assert_redirect_error(response, surface, error)
    assert response.headers["cache-control"] == "no-store"


async def test_a_duplicated_parameter_is_a_page(
    subdomain_surface: OAuthSurface, address: str
) -> None:
    async with oauth_client(subdomain_surface, client_address=address) as client:
        client_id = await _registered(client)
        params = list(client.authorize_params(client_id).items())
        params.append(("redirect_uri", "https://other.example.com/callback"))
        response = await client.http.get(
            subdomain_surface.authorization_endpoint, params=params
        )
    _assert_error_page(response, 400, "invalid_request")


# --- AC-8: no session goes through the existing login -------------------------------


async def test_no_session_goes_through_login_and_lands_on_consent(
    surface: OAuthSurface, address: str, member: Member
) -> None:
    config = _routing()
    _sign_in_as(member.identity)
    async with oauth_client(surface, client_address=address) as client:
        client_id = await _registered(client)
        authorized = await client.authorize(client.authorize_params(client_id))
        assert authorized.status_code == 303
        # A same-host path with no query: the parameters stay server-side.
        assert authorized.headers["location"] == identity_path(config, "/oauth/consent")
        cookie = [
            header
            for header in authorized.headers.get_list("set-cookie")
            if header.startswith(f"{OAUTH_REQUEST_COOKIE}=")
        ][0]
        assert "Max-Age=600" in cookie
        assert "HttpOnly" in cookie

        to_login = await client.follow(authorized)
        assert to_login.status_code == 302
        login_location = to_login.headers["location"]
        assert login_location.split("?", 1)[0] == identity_path(config, "/login")
        assert redirect_query(to_login) == {
            "return": url_for(config, IDENTITY, "/oauth/consent")
        }

        page = await client.follow_chain(to_login)
    assert page.status_code == 200, page.text
    assert client.callback_urls == [url_for(config, IDENTITY, "/callback")]
    assert 'data-state="consent"' in page.text


async def test_a_closed_signup_unknown_identity_gets_the_existing_refusal(
    subdomain_surface: OAuthSurface,
    address: str,
    cluster: ClusterSession,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RHEO__identity__allow_signup", "false")
    stranger = ProviderIdentity(
        provider_id=FIXED_PROVIDER_ID,
        subject=f"stranger-{uuid7().hex[:12]}",
        email="stranger@example.test",
        email_verified=True,
        display_name="stranger",
    )
    _sign_in_as(stranger)
    async with oauth_client(subdomain_surface, client_address=address) as client:
        client_id = await _registered(client)
        authorized = await client.authorize(client.authorize_params(client_id))
        nonce = _nonce_of(authorized)
        refused = await client.follow_chain(authorized)
    assert refused.status_code == 403
    assert refused.json() == {"state": "signup_closed"}
    row = _row(cluster, nonce)
    assert row["decided_at"] is None and row["code_hash"] is None
    with cluster.backend.control_engine.connect() as connection:
        grants = connection.execute(
            select(func.count())
            .select_from(o.oauth_grant)
            .where(o.oauth_grant.c.client_id == client_id)
        ).scalar_one()
    assert grants == 0
    assert client.issued.code == []


# --- AC-9: the consent page ----------------------------------------------------------


async def test_the_consent_page_shows_who_what_where_and_is_shown_every_time(
    surface: OAuthSurface, address: str, member: Member, cluster: ClusterSession
) -> None:
    hostile = '<script>alert("x")</script> & Co'
    async with oauth_client(surface, client_address=address) as client:
        client_id = await _registered(client, hostile)
        first, _ = await _fresh_consent(client, member, client_id)
        # A second authorization by the same client, session already held: the
        # page is shown again, not skipped.
        second, _ = await _fresh_consent(client, member, client_id)
    with cluster.backend.control_engine.connect() as connection:
        workspace = get_workspace(connection, member.workspaces[0])
    assert workspace is not None
    for page in (first, second):
        _assert_page_headers(page)
        text = page.text
        assert "<script>" not in text
        assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; Co" in text
        assert "chosen by the app" in text
        assert "<strong>claude.ai</strong>" in text
        assert f"<strong>{member.display_name}</strong>" in text
        assert workspace.display_name in text
        for operation in connector_operations(Role.OWNER):
            assert f"<li>{operation}</li>" in text
        days = grant_days(surface, member.workspaces[0])
        assert f"lasts {days} days" in text
        assert '<form method="post">' in text
        assert '<button type="submit" name="decision" value="allow">' in text
        assert '<button type="submit" name="decision" value="deny">' in text
        fields = consent_fields(text)
        assert fields["workspace_id"] == str(member.workspaces[0])


async def test_deny_redirects_access_denied_and_issues_nothing(
    surface: OAuthSurface, address: str, member: Member, cluster: ClusterSession
) -> None:
    async with oauth_client(surface, client_address=address) as client:
        client_id = await _registered(client)
        page, nonce = await _fresh_consent(client, member, client_id)
        fields = consent_fields(page.text)
        denied = await client.decide(
            "deny", request_id=fields["request_id"], account_id=fields["account_id"]
        )
    _assert_redirect_error(denied, surface, "access_denied")
    assert _nonce_of(denied) == ""
    after = _snapshot(cluster, nonce, member.account_id, client_id)
    assert after.row["decided_at"] is not None
    assert after.row["code_hash"] is None and after.row["code_expires_at"] is None
    assert after.issued == (0, 0)
    assert after.events.count(("authorization_denied", "refused")) == 1


@pytest.mark.parametrize("origin", [FOREIGN_ORIGIN, None])
async def test_allow_with_a_bad_origin_is_refused_and_writes_nothing(
    subdomain_surface: OAuthSurface,
    address: str,
    member: Member,
    cluster: ClusterSession,
    origin: str | None,
) -> None:
    async with oauth_client(subdomain_surface, client_address=address) as client:
        client_id = await _registered(client)
        page, nonce = await _fresh_consent(client, member, client_id)
        before = _snapshot(cluster, nonce, member.account_id, client_id)
        fields = consent_fields(page.text)
        refused = await client.decide(
            "allow",
            request_id=fields["request_id"],
            account_id=fields["account_id"],
            workspace_id=fields["workspace_id"],
            origin=origin,
        )
    assert refused.status_code == 403
    assert refused.json() == {"state": "origin_not_allowed"}
    after = _snapshot(cluster, nonce, member.account_id, client_id)
    assert after == before
    _assert_undecided_and_nothing_issued(after)


async def test_the_consent_routes_are_404_off_the_identity_host(
    subdomain_surface: OAuthSurface, address: str
) -> None:
    async with oauth_client(subdomain_surface, client_address=address) as client:
        shell = await client.http.get(f"https://circuit.{BASE_HOST}/auth/oauth/consent")
    assert shell.status_code == 404
    _assert_page_headers(shell)


async def test_unconfigured_the_browser_routes_are_404(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    _set_mode(monkeypatch, "subdomain")
    surface = _configure(monkeypatch)
    monkeypatch.setenv("RHEO__identity__oauth__enabled", "false")
    async with oauth_client(surface, client_address=address) as client:
        authorize = await client.http.get(surface.authorization_endpoint)
        consent = await client.http.get(client.consent_endpoint)
    assert authorize.status_code == 404 and consent.status_code == 404


# --- end to end: register to exchange -------------------------------------------------


async def test_the_harness_drives_register_to_exchange_end_to_end(
    surface: OAuthSurface, address: str, member: Member, cluster: ClusterSession
) -> None:
    _sign_in_as(member.identity)
    async with oauth_client(surface, client_address=address) as client:
        client_id, token = await client.connect()
        assert token.status_code == 200, token.text
        access = token.json()["access_token"]
        listed = await client.mcp_call(access)
    assert listed.status_code == 200, listed.text
    assert _issued(cluster, member.account_id, client_id) == (1, 1)
    events = _events(cluster, client_id)
    assert ("authorization_granted", "succeeded") in events
    assert ("code_redeemed", "succeeded") in events


# --- AC-10: workspace binding --------------------------------------------------------


async def test_two_workspaces_offer_exactly_those_and_bind_the_chosen_one(
    subdomain_surface: OAuthSurface,
    address: str,
    make_member: Any,
    cluster: ClusterSession,
) -> None:
    owner: Member = make_member(2)
    async with oauth_client(subdomain_surface, client_address=address) as client:
        client_id = await _registered(client)
        page, nonce = await _fresh_consent(client, owner, client_id)
        assert sorted(consent_options(page.text)) == sorted(
            str(workspace) for workspace in owner.workspaces
        )
        assert '<label for="workspace_id">' in page.text
        chosen = owner.workspaces[1]
        allowed = await client.decide(
            "allow",
            **_posted(page),
            workspace_id=str(chosen),
        )
    assert allowed.status_code == 302, allowed.text
    assert "code" in redirect_query(allowed)
    assert _row(cluster, nonce)["workspace_id"] == chosen


async def test_a_workspace_the_account_is_not_a_member_of_is_refused(
    subdomain_surface: OAuthSurface,
    address: str,
    member: Member,
    workspace: UUID,
    cluster: ClusterSession,
) -> None:
    """``workspace`` belongs to the fixture's other owner, not to ``member``."""
    async with oauth_client(subdomain_surface, client_address=address) as client:
        client_id = await _registered(client)
        page, nonce = await _fresh_consent(client, member, client_id)
        before = _snapshot(cluster, nonce, member.account_id, client_id)
        refused = await client.decide(
            "allow",
            **_posted(page),
            workspace_id=str(workspace),
        )
    _assert_error_page(refused, 403, "not_a_member")
    after = _snapshot(cluster, nonce, member.account_id, client_id)
    assert after == before
    _assert_undecided_and_nothing_issued(after)


async def test_an_account_with_no_workspace_is_workspace_unselected(
    subdomain_surface: OAuthSurface,
    address: str,
    make_member: Any,
    cluster: ClusterSession,
) -> None:
    lonely: Member = make_member(0)
    _sign_in_as(lonely.identity)
    async with oauth_client(subdomain_surface, client_address=address) as client:
        client_id = await _registered(client)
        authorized = await client.authorize(client.authorize_params(client_id))
        nonce = _nonce_of(authorized)
        refused = await client.follow_chain(authorized)
        # The row is decided, so the same handle cannot be completed later.
        _replay_cookie(client, nonce)
        again = await client.http.get(client.consent_endpoint)
    _assert_error_page(refused, 403, "workspace_unselected")
    assert _nonce_of(refused) == ""
    _assert_error_page(again, 400, "authorization_expired")
    row = _row(cluster, nonce)
    assert row["decided_at"] is not None
    assert row["code_hash"] is None and row["code_expires_at"] is None
    assert _issued(cluster, lonely.account_id, client_id) == (0, 0)
    events = _events(cluster, client_id)
    assert events.count(("authorization_granted", "refused")) == 1
    assert ("authorization_granted", "succeeded") not in events


# --- AC-26: late, from another browser, twice -----------------------------------------


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_at_expires_at_the_consent_is_authorization_expired(
    surface: OAuthSurface,
    address: str,
    member: Member,
    cluster: ClusterSession,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    async with oauth_client(surface, client_address=address) as client:
        client_id = await _registered(client)
        page, nonce = await _fresh_consent(client, member, client_id)
        fields = consent_fields(page.text)
        before = _snapshot(cluster, nonce, member.account_id, client_id)
        expires_at: datetime = before.row["expires_at"]
        assert expires_at - before.row["created_at"] == timedelta(minutes=10)
        _pin_clock(monkeypatch, expires_at)
        if method == "GET":
            refused = await client.http.get(client.consent_endpoint)
        else:
            refused = await client.decide(
                "allow",
                request_id=fields["request_id"],
                account_id=fields["account_id"],
                workspace_id=fields["workspace_id"],
            )
    _assert_error_page(refused, 400, "authorization_expired")
    after = _snapshot(cluster, nonce, member.account_id, client_id)
    assert after == before
    _assert_undecided_and_nothing_issued(after)


async def test_one_second_before_expires_at_consent_succeeds(
    surface: OAuthSurface,
    address: str,
    member: Member,
    cluster: ClusterSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with oauth_client(surface, client_address=address) as client:
        client_id = await _registered(client)
        _, nonce = await _fresh_consent(client, member, client_id)
        expires_at: datetime = _row(cluster, nonce)["expires_at"]
        _pin_clock(monkeypatch, expires_at - timedelta(seconds=1))
        page = await client.http.get(client.consent_endpoint)
        assert page.status_code == 200, page.text
        fields = consent_fields(page.text)
        allowed = await client.decide(
            "allow",
            request_id=fields["request_id"],
            account_id=fields["account_id"],
            workspace_id=fields["workspace_id"],
        )
    assert allowed.status_code == 302, allowed.text
    assert "code" in redirect_query(allowed)
    assert _row(cluster, nonce)["code_hash"] is not None


async def test_another_browser_without_the_cookie_gets_authorization_expired(
    surface: OAuthSurface, address: str, member: Member, cluster: ClusterSession
) -> None:
    async with oauth_client(surface, client_address=address) as client:
        client_id = await _registered(client)
        page, nonce = await _fresh_consent(client, member, client_id)
        fields = consent_fields(page.text)
        before = _snapshot(cluster, nonce, member.account_id, client_id)

        client.http.cookies.delete(OAUTH_REQUEST_COOKIE)
        no_cookie_get = await client.http.get(client.consent_endpoint)
        no_cookie_post = await client.decide(
            "allow",
            request_id=fields["request_id"],
            account_id=fields["account_id"],
            workspace_id=fields["workspace_id"],
        )
        _replay_cookie(client, "0" * 64)
        unknown_get = await client.http.get(client.consent_endpoint)
        unknown_post = await client.decide(
            "allow",
            request_id=fields["request_id"],
            account_id=fields["account_id"],
            workspace_id=fields["workspace_id"],
        )
    for response in (no_cookie_get, no_cookie_post, unknown_get, unknown_post):
        _assert_error_page(response, 400, "authorization_expired")
    after = _snapshot(cluster, nonce, member.account_id, client_id)
    assert after == before
    _assert_undecided_and_nothing_issued(after)


async def test_a_session_holding_its_own_row_cannot_approve_another_rows_id(
    surface: OAuthSurface, address: str, member: Member, cluster: ClusterSession
) -> None:
    async with oauth_client(surface, client_address=address) as client:
        client_id = await _registered(client)
        first_page, first_nonce = await _fresh_consent(client, member, client_id)
        first_id = consent_fields(first_page.text)["request_id"]
        # The browser starts a second authorization: its cookie now names row two.
        _, second_nonce = await _fresh_consent(client, member, client_id)
        before_first = _snapshot(cluster, first_nonce, member.account_id, client_id)
        before_second = _snapshot(cluster, second_nonce, member.account_id, client_id)
        refused = await client.decide(
            "allow",
            request_id=first_id,
            account_id=str(member.account_id),
            workspace_id=str(member.workspaces[0]),
        )
        random_id = await client.decide(
            "allow",
            request_id=str(uuid7()),
            account_id=str(member.account_id),
            workspace_id=str(member.workspaces[0]),
        )
    _assert_error_page(refused, 400, "authorization_expired")
    _assert_error_page(random_id, 400, "authorization_expired")
    for nonce, before in (
        (first_nonce, before_first),
        (second_nonce, before_second),
    ):
        after = _snapshot(cluster, nonce, member.account_id, client_id)
        assert after == before
        _assert_undecided_and_nothing_issued(after)


async def test_a_denied_request_cannot_be_replayed(
    surface: OAuthSurface, address: str, member: Member, cluster: ClusterSession
) -> None:
    async with oauth_client(surface, client_address=address) as client:
        client_id = await _registered(client)
        page, nonce = await _fresh_consent(client, member, client_id)
        fields = consent_fields(page.text)
        denied = await client.decide(
            "deny", request_id=fields["request_id"], account_id=fields["account_id"]
        )
        assert denied.status_code == 302
        decided = _snapshot(cluster, nonce, member.account_id, client_id)

        _replay_cookie(client, nonce)
        replay_get = await client.http.get(client.consent_endpoint)
        replay_allow = await client.decide(
            "allow",
            request_id=fields["request_id"],
            account_id=fields["account_id"],
            workspace_id=fields["workspace_id"],
        )
        replay_deny = await client.decide(
            "deny", request_id=fields["request_id"], account_id=fields["account_id"]
        )
    for response in (replay_get, replay_allow, replay_deny):
        _assert_error_page(response, 400, "authorization_expired")
    after = _snapshot(cluster, nonce, member.account_id, client_id)
    assert after == decided
    assert after.row["code_hash"] is None
    assert after.issued == (0, 0)
    assert after.events.count(("authorization_denied", "refused")) == 1


async def test_an_approved_request_cannot_be_replayed_and_keeps_its_code(
    surface: OAuthSurface, address: str, member: Member, cluster: ClusterSession
) -> None:
    async with oauth_client(surface, client_address=address) as client:
        client_id = await _registered(client)
        page, nonce = await _fresh_consent(client, member, client_id)
        fields = consent_fields(page.text)
        allowed = await client.decide(
            "allow",
            request_id=fields["request_id"],
            account_id=fields["account_id"],
            workspace_id=fields["workspace_id"],
        )
        assert allowed.status_code == 302, allowed.text
        code = client.code_from(allowed)
        approved = _snapshot(cluster, nonce, member.account_id, client_id)
        assert approved.row["code_hash"] == hashlib.sha256(code.encode()).digest()

        _replay_cookie(client, nonce)
        replay_get = await client.http.get(client.consent_endpoint)
        replay_allow = await client.decide(
            "allow",
            request_id=fields["request_id"],
            account_id=fields["account_id"],
            workspace_id=fields["workspace_id"],
        )
        replay_deny = await client.decide(
            "deny", request_id=fields["request_id"], account_id=fields["account_id"]
        )
    for response in (replay_get, replay_allow, replay_deny):
        _assert_error_page(response, 400, "authorization_expired")
    after = _snapshot(cluster, nonce, member.account_id, client_id)
    assert after == approved
    assert after.events.count(("authorization_granted", "succeeded")) == 1
    assert ("authorization_denied", "refused") not in after.events


async def test_allow_after_the_session_switched_accounts_is_refused(
    surface: OAuthSurface,
    address: str,
    member: Member,
    make_member: Any,
    cluster: ClusterSession,
) -> None:
    """The page was shown to account A; the browser then signs in as B (a member
    of the same workspace) and posts A's page. B must not grant on A's consent,
    and a post with no ``account_id`` is refused the same way."""
    other: Member = make_member(0)
    with cluster.backend.control_engine.begin() as connection:
        insert_membership_if_absent(
            connection,
            account_id=other.account_id,
            workspace_id=member.workspaces[0],
            role=Role.MEMBER,
        )
    config = _routing()
    async with oauth_client(surface, client_address=address) as client:
        client_id = await _registered(client)
        page, nonce = await _fresh_consent(client, member, client_id)
        fields = consent_fields(page.text)
        assert fields["account_id"] == str(member.account_id)
        before = _snapshot(cluster, nonce, member.account_id, client_id)

        _sign_in_as(other.identity)
        login = await client.http.get(
            f"{client.identity_origin}{identity_path(config, '/login')}",
            params={"return": url_for(config, IDENTITY, "/oauth/consent")},
        )
        as_other = await client.follow_chain(login)
        assert as_other.status_code == 200, as_other.text
        assert consent_fields(as_other.text)["account_id"] == str(other.account_id)

        switched = await client.decide(
            "allow",
            request_id=fields["request_id"],
            account_id=fields["account_id"],
            workspace_id=fields["workspace_id"],
        )
        absent = await client.decide(
            "allow",
            request_id=fields["request_id"],
            workspace_id=fields["workspace_id"],
        )
    _assert_error_page(switched, 400, "authorization_expired")
    _assert_error_page(absent, 400, "authorization_expired")
    after = _snapshot(cluster, nonce, member.account_id, client_id)
    assert after == before
    _assert_undecided_and_nothing_issued(after)
    assert _issued(cluster, other.account_id, client_id) == (0, 0)
