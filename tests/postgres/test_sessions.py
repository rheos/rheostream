"""C7b: the public ``/auth/*`` routes and the internal listener (08).

Parametrized over both routing modes from the shared fixture host names
(``example.test`` path mode; ``circuit.example.test``/``auth.example.test``
subdomain mode — the same names ``tests/fixtures/routing/*.json`` use). Drives
the full ``/auth/login`` -> ``/auth/callback`` -> (subdomain: ``/auth/continue``
grant/consume) flow via ``httpx.ASGITransport`` against both FastAPI apps
in-process, following redirects by hand so each ``Set-Cookie`` can be asserted.

This is also the home for every one of B18's cookie/listener sub-clauses and
B10's grant-row-count clauses (``00-index.md``'s acceptance-criteria coverage
matrix), and for the CSRF-grant mechanism coverage carried in from 07's cold
review: a replayed grant, one on the wrong host, an expired one, a nonce
mismatch, and a revocation that takes effect on a different host from the one
that revoked.
"""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

import httpx
import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.identity import FIXED_PROVIDER_ID, FixedIdentityProvider
from harness.modules import WEB_SURFACE_HOST, WEB_SURFACE_ID, loaded_probe_modules
from rheo_app_cli.main import main as cli_main
from rheo_app_core import auth_routes
from rheo_app_core.main import internal_app, public_app
from rheo_app_core.startup import _check_production_scheme
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.boundary.context import (
    INVALID_GRANT,
    INVALID_RETURN,
    NOT_A_MEMBER,
    Refusal,
)
from rheo_core.boundary.factories import context_from_session
from rheo_core.identity.boundary import ProviderIdentity
from rheo_core.operations import ROLE_NOT_PERMITTED, SETTINGS_SET, dispatch
from rheo_core.refs import uuid7
from rheo_core.sessions import (
    CONTINUE_COOKIE,
    OAUTH_STATE_COOKIE,
    SESSION_COOKIE,
    consume_grant,
    create_session,
    mint_host_secret,
    switch_workspace,
    write_grant,
)
from rheo_core.settings import env_variable_names
from rheo_core.storage import control_tables
from rheo_core.storage.control_plane import (
    get_session_by_secret_hash,
    insert_account,
    insert_identity,
    insert_session_grant,
    list_memberships,
)
from sqlalchemy import func, select

pytestmark = pytest.mark.postgres

BASE_HOST = "example.test"
SHELL_HOST = f"circuit.{BASE_HOST}"
IDENTITY_HOST = f"auth.{BASE_HOST}"
INTERNAL_SECRET_VALUE = "internal-listener-test-secret"


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _internal_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RHEO_INTERNAL_SECRET", INTERNAL_SECRET_VALUE)
    monkeypatch.setenv(
        "RHEO__internal__secret_ref", "secret://env/RHEO_INTERNAL_SECRET"
    )


@pytest.fixture(autouse=True)
def _default_identity_provider() -> Iterator[None]:
    """A working identity provider for every test in this module by default, so a
    test that only cares about routing/CSRF behaviour (not a specific identity)
    never 500s on the unconfigured-provider path. A zero-argument lambda, not the
    class itself: FastAPI's dependency system would otherwise introspect
    ``FixedIdentityProvider.__init__``'s own ``identity`` parameter as if it were
    a nested dependency."""
    public_app.dependency_overrides[auth_routes.resolve_identity_provider] = lambda: (
        FixedIdentityProvider()
    )
    yield
    public_app.dependency_overrides.pop(auth_routes.resolve_identity_provider, None)


def _set_routing_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("RHEO__routing__mode", mode)
    monkeypatch.setenv("RHEO__routing__base_host", BASE_HOST)


@contextmanager
def _signed_in_as(identity: ProviderIdentity) -> Iterator[None]:
    """Override the identity-provider dependency for one specific identity,
    restoring whatever override (the module default, or none) was active before."""
    overrides = public_app.dependency_overrides
    key = auth_routes.resolve_identity_provider
    previous = overrides.get(key)
    overrides[key] = lambda: FixedIdentityProvider(identity)
    try:
        yield
    finally:
        if previous is None:
            overrides.pop(key, None)
        else:
            overrides[key] = previous


def _bind_identity(
    cluster: ClusterSession, account_id: UUID, *, subject: str
) -> ProviderIdentity:
    """A fresh ``control.identity`` row for ``account_id``, so signing in through
    ``FixedIdentityProvider(identity)`` resolves to this exact, already-known
    account rather than creating a new one."""
    identity = ProviderIdentity(
        provider_id=FIXED_PROVIDER_ID,
        subject=subject,
        email=f"{subject}@example.test",
        email_verified=True,
        display_name=subject,
    )
    with cluster.backend.control_engine.begin() as connection:
        insert_identity(
            connection,
            account_id=account_id,
            provider_id=identity.provider_id,
            provider_subject=identity.subject,
            email=identity.email,
            email_verified=identity.email_verified,
        )
    return identity


def _client() -> httpx.AsyncClient:
    """No ``base_url``: every call below uses a fully-qualified URL so httpx's own
    cookie jar scopes each cookie to the exact host that set it — a real browser's
    behaviour, and exactly what A11's host-only cookies depend on."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=public_app), follow_redirects=False
    )


def _internal_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
    )


def _run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli_main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# --- cookie helpers -----------------------------------------------------------


def _cookie_headers(response: httpx.Response, name: str) -> list[str]:
    return [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{name}=")
    ]


def _cookie_value(header: str) -> str:
    return header.split(";", 1)[0].partition("=")[2]


def _assert_cookie_attrs(header: str, *, https: bool = True) -> None:
    assert "HttpOnly" in header
    assert "SameSite=Lax" in header
    assert "Path=/" in header
    assert "Domain" not in header
    if https:
        assert "Secure" in header
    else:
        assert "Secure" not in header


def _state_of(location: str) -> str:
    return dict(parse_qsl(urlsplit(location).query))["state"]


def _code_of(location: str) -> str:
    return dict(parse_qsl(urlsplit(location).query))["code"]


def _session_id_for_secret(
    cluster: ClusterSession, session_cookie_value: str, host: str
) -> UUID:
    with cluster.backend.control_engine.connect() as connection:
        row = get_session_by_secret_hash(
            connection,
            secret_hash=hashlib.sha256(bytes.fromhex(session_cookie_value)).digest(),
            host=host,
        )
    assert row is not None
    return row.id


def _grant_row_count_for_session(cluster: ClusterSession, session_id: UUID) -> int:
    with cluster.backend.control_engine.connect() as connection:
        return int(
            connection.execute(
                select(func.count())
                .select_from(control_tables.session_grant)
                .where(control_tables.session_grant.c.session_id == session_id)
            ).scalar_one()
        )


async def _login_and_callback(
    client: httpx.AsyncClient, *, host: str, return_target: str
) -> httpx.Response:
    login = await client.get(
        f"https://{host}/auth/login", params={"return": return_target}
    )
    assert login.status_code == 302
    oauth_cookie = _cookie_headers(login, OAUTH_STATE_COOKIE)[0]
    _assert_cookie_attrs(oauth_cookie)
    state = _state_of(login.headers["location"])
    return await client.get(
        f"https://{host}/auth/callback",
        params={"code": "provider-code", "state": state},
    )


# --- the full flow, both modes, and B10's grant-row-count clauses ------------


@pytest.mark.parametrize("mode", ["path", "subdomain"])
async def test_full_identity_flow_and_grant_row_count(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    _set_routing_mode(monkeypatch, mode)
    subject = f"owner-{uuid7().hex[:12]}"
    identity = _bind_identity(cluster, owner_account_id, subject=subject)

    login_host = BASE_HOST if mode == "path" else IDENTITY_HOST
    final_host = BASE_HOST if mode == "path" else SHELL_HOST
    return_target = f"https://{final_host}/dashboard"

    async with _client() as client:
        with _signed_in_as(identity):
            callback = await _login_and_callback(
                client, host=login_host, return_target=return_target
            )
        assert callback.status_code == 302
        session_cookie = _cookie_headers(callback, SESSION_COOKIE)[0]
        _assert_cookie_attrs(session_cookie)
        session_value = _cookie_value(session_cookie)
        identity_session_id = _session_id_for_secret(cluster, session_value, login_host)

        nonce = f"nonce-{uuid7().hex}"
        client.cookies.set(CONTINUE_COOKIE, nonce, domain=final_host)
        continue_resp = await client.get(
            f"https://{login_host}/auth/continue",
            params={"return": return_target, "nonce": nonce},
        )
        assert continue_resp.status_code == 302

        if mode == "subdomain":
            hop_target = continue_resp.headers["location"]
            continue_resp = await client.get(hop_target)
            assert continue_resp.status_code == 302
            shell_session_cookie = _cookie_headers(continue_resp, SESSION_COOKIE)[0]
            _assert_cookie_attrs(shell_session_cookie)
            session_value = _cookie_value(shell_session_cookie)
            cleared_continue = _cookie_headers(continue_resp, CONTINUE_COOKIE)[0]
            _assert_cookie_attrs(cleared_continue)
            assert _cookie_value(cleared_continue) == ""
        else:
            # Path mode: the single host finds the cookie directly and redirects,
            # writing no grant row and minting no new secret (this host already
            # had a valid one from the callback above).
            assert continue_resp.headers["location"] == "/dashboard"
            assert _cookie_headers(continue_resp, SESSION_COOKIE) == []

        # Both modes land on the same final path.
        assert continue_resp.headers["location"] == "/dashboard"

        grant_count = _grant_row_count_for_session(cluster, identity_session_id)
        assert grant_count == (1 if mode == "subdomain" else 0)

        switch = await client.post(
            f"https://{final_host}/auth/session/workspace",
            json={"target_workspace_id": str(workspace)},
            headers={"Origin": f"https://{final_host}"},
        )
        assert switch.status_code == 200
        assert switch.json() == {
            "state": "ok",
            "active_workspace_id": str(workspace),
        }

    async with _internal_client() as internal_client:
        resp = await internal_client.get(
            "/internal/v1/session",
            headers={
                "X-Rheo-Internal": INTERNAL_SECRET_VALUE,
                "X-Rheo-Session": session_value,
                "X-Rheo-Host": final_host,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    assert body["actor"]["kind"] == "account"
    assert body["active_workspace_id"] == str(workspace)
    assert body["role"] == "owner"


# --- B18: invalid_return, both roles of /auth/continue and /auth/login ----------------


async def test_login_refuses_invalid_return_no_cookie_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_routing_mode(monkeypatch, "path")
    async with _client() as client:
        resp = await client.get(
            f"https://{BASE_HOST}/auth/login",
            params={"return": "https://evil.example.net/steal"},
        )
    assert resp.status_code == 400
    assert resp.json() == {"state": INVALID_RETURN}
    assert resp.headers.get_list("set-cookie") == []


async def test_continue_refuses_invalid_return_on_the_identity_host(
    monkeypatch: pytest.MonkeyPatch, cluster: ClusterSession
) -> None:
    _set_routing_mode(monkeypatch, "subdomain")
    async with _client() as client:
        resp = await client.get(
            f"https://{IDENTITY_HOST}/auth/continue",
            params={"return": "https://evil.example.net/steal", "nonce": "x"},
        )
    assert resp.status_code == 400
    assert resp.json() == {"state": INVALID_RETURN}
    assert resp.headers.get_list("set-cookie") == []


async def test_continue_refuses_invalid_return_in_path_mode_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path mode still has only one real host, but ``return`` can still name an
    arbitrary external URL — refused identically."""
    _set_routing_mode(monkeypatch, "path")
    async with _client() as client:
        resp = await client.get(
            f"https://{BASE_HOST}/auth/continue",
            params={"return": "https://evil.example.net/steal", "nonce": "x"},
        )
    assert resp.status_code == 400
    assert resp.json() == {"state": INVALID_RETURN}


# --- #119: a disabled provider, and module hosts on the identity routes ---------

MODULE_HOST = f"{WEB_SURFACE_HOST}.{BASE_HOST}"


async def _login_with_no_enabled_provider(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> httpx.Response:
    """``/auth/login`` through the real dependency (this module's default override
    is removed first) with GitHub sign-in off."""
    public_app.dependency_overrides.pop(auth_routes.resolve_identity_provider, None)
    for variable in env_variable_names("identity.providers.github.enabled"):
        monkeypatch.setenv(variable, "false")
    _set_routing_mode(monkeypatch, "path")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=public_app, raise_app_exceptions=False),
        follow_redirects=False,
    ) as client:
        return await client.get(f"https://{BASE_HOST}/auth/login", headers=headers)


@pytest.mark.parametrize(
    "accept",
    [None, "application/json", "*/*", "text/html;q=0.5, application/json"],
    ids=["httpx-default", "json", "wildcard", "json-ranked-higher"],
)
async def test_login_with_no_enabled_provider_is_a_typed_503(
    monkeypatch: pytest.MonkeyPatch, accept: str | None
) -> None:
    """The flagship's first deploy runs with GitHub disabled on purpose, so
    ``/auth/login`` must refuse cleanly rather than answer a bare 500. An API
    client keeps the typed JSON body exactly."""
    headers = {} if accept is None else {"accept": accept}
    resp = await _login_with_no_enabled_provider(monkeypatch, headers)
    assert resp.status_code == 503
    assert resp.headers["content-type"] == "application/json"
    assert resp.json() == {"state": "identity_provider_unavailable"}
    assert resp.headers.get_list("set-cookie") == []


async def test_login_with_no_enabled_provider_shows_a_browser_a_readable_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#119 item 2: a browser navigation gets a plain HTML refusal page, not JSON.
    It names no settings key or host, and keeps the error code greppable."""
    browser_accept = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    resp = await _login_with_no_enabled_provider(
        monkeypatch, {"accept": browser_accept}
    )
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["vary"] == "Accept"
    page = resp.text
    assert "Sign-in is not available on this deployment" in page
    assert "no identity provider is" in page
    assert 'data-state="identity_provider_unavailable"' in page
    assert "identity.providers" not in page
    assert BASE_HOST not in page
    assert resp.headers.get_list("set-cookie") == []


async def test_continue_accepts_a_loaded_module_host_as_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed-out visitor on a module's host is sent to ``/auth/continue`` with
    that host as ``return``; with the module loaded it must reach the login
    redirect, not ``invalid_return``. A label nothing loaded is still refused."""
    _set_routing_mode(monkeypatch, "subdomain")
    with loaded_probe_modules(monkeypatch, WEB_SURFACE_ID):
        async with _client() as client:
            accepted = await client.get(
                f"https://{IDENTITY_HOST}/auth/continue",
                params={"return": f"https://{MODULE_HOST}/notes", "nonce": "x"},
            )
            unknown = await client.get(
                f"https://{IDENTITY_HOST}/auth/continue",
                params={"return": f"https://unloaded.{BASE_HOST}/notes", "nonce": "x"},
            )
    assert accepted.status_code == 302, accepted.text
    assert urlsplit(accepted.headers["location"]).path.endswith("/login")
    assert unknown.status_code == 400
    assert unknown.json() == {"state": INVALID_RETURN}


async def test_continue_refuses_a_module_host_when_the_module_is_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_routing_mode(monkeypatch, "subdomain")
    async with _client() as client:
        resp = await client.get(
            f"https://{IDENTITY_HOST}/auth/continue",
            params={"return": f"https://{MODULE_HOST}/notes", "nonce": "x"},
        )
    assert resp.status_code == 400
    assert resp.json() == {"state": INVALID_RETURN}


async def test_origin_check_follows_whether_the_module_surface_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/auth/logout`` on the module host with that host as ``Origin``: allowed
    while the surface is loaded, ``origin_not_allowed`` once it is not."""
    _set_routing_mode(monkeypatch, "subdomain")
    headers = {"Origin": f"https://{MODULE_HOST}"}
    async with _client() as client:
        with loaded_probe_modules(monkeypatch, WEB_SURFACE_ID):
            loaded = await client.post(
                f"https://{MODULE_HOST}/auth/logout", headers=headers
            )
        unloaded = await client.post(
            f"https://{MODULE_HOST}/auth/logout", headers=headers
        )
    assert loaded.status_code == 302, loaded.text
    assert unloaded.status_code == 403
    assert unloaded.json() == {"state": "origin_not_allowed"}


# --- B18: the four invalid_grant cases ----------------------------------------


def test_grant_presented_on_the_wrong_host_is_invalid_grant(
    cluster: ClusterSession, owner_account_id: UUID
) -> None:
    """The wrong-host case is exercised directly against the grant mechanism
    (``write_grant``/``consume_grant``): this deployment's default topology has
    only two application hosts (shell, identity), and a grant is only ever
    *written* by the identity-host role targeting the other one, so there is no
    third, legitimately-routable application host from which to reach this case
    through the live HTTP endpoint. The mechanism itself is exactly what 07's
    cold review flagged as untested (``00-index.md``), and this is a direct,
    unambiguous seam for it."""
    session_row = create_session(owner_account_id)
    nonce = b"a-fixed-nonce-value"
    code = write_grant(session_row.id, SHELL_HOST, nonce)
    result = consume_grant(code, IDENTITY_HOST, nonce)
    assert isinstance(result, Refusal)
    assert result.state == INVALID_GRANT


async def _write_grant_via_http(
    client: httpx.AsyncClient, cluster: ClusterSession, owner_account_id: UUID
) -> tuple[str, str]:
    subject = f"grantee-{uuid7().hex[:12]}"
    identity = _bind_identity(cluster, owner_account_id, subject=subject)
    return_target = f"https://{SHELL_HOST}/dashboard"
    with _signed_in_as(identity):
        callback = await _login_and_callback(
            client, host=IDENTITY_HOST, return_target=return_target
        )
    assert callback.status_code == 302
    nonce = f"nonce-{uuid7().hex}"
    resp = await client.get(
        f"https://{IDENTITY_HOST}/auth/continue",
        params={"return": return_target, "nonce": nonce},
    )
    assert resp.status_code == 302
    return _code_of(resp.headers["location"]), nonce


_SHELL_RETURN = f"https://{SHELL_HOST}/dashboard"


async def test_a_used_grant_code_is_invalid_grant_on_replay(
    monkeypatch: pytest.MonkeyPatch, cluster: ClusterSession, owner_account_id: UUID
) -> None:
    _set_routing_mode(monkeypatch, "subdomain")
    async with _client() as client:
        code, nonce = await _write_grant_via_http(client, cluster, owner_account_id)
        client.cookies.set(CONTINUE_COOKIE, nonce, domain=SHELL_HOST)
        first = await client.get(
            f"https://{SHELL_HOST}/auth/continue",
            params={"code": code, "return": _SHELL_RETURN},
        )
        assert first.status_code == 302
        second = await client.get(
            f"https://{SHELL_HOST}/auth/continue",
            params={"code": code, "return": _SHELL_RETURN},
        )
    assert second.status_code == 400
    assert second.json() == {"state": INVALID_GRANT}


async def test_an_expired_grant_code_is_invalid_grant(
    monkeypatch: pytest.MonkeyPatch, cluster: ClusterSession, owner_account_id: UUID
) -> None:
    """``insert_session_grant`` takes ``expires_at`` directly, so the row is
    written already-expired rather than waiting or mocking the clock (there is
    no time-freezing convention elsewhere in this suite to mirror)."""
    _set_routing_mode(monkeypatch, "subdomain")
    subject = f"grantee-{uuid7().hex[:12]}"
    identity = _bind_identity(cluster, owner_account_id, subject=subject)
    async with _client() as client:
        with _signed_in_as(identity):
            callback = await _login_and_callback(
                client,
                host=IDENTITY_HOST,
                return_target=f"https://{SHELL_HOST}/dashboard",
            )
        assert callback.status_code == 302
        session_value = _cookie_value(_cookie_headers(callback, SESSION_COOKIE)[0])
        session_id = _session_id_for_secret(cluster, session_value, IDENTITY_HOST)

        nonce = "an-expired-nonce-value"
        code = f"expired-{uuid7().hex}"
        with cluster.backend.control_engine.begin() as connection:
            insert_session_grant(
                connection,
                code_hash=hashlib.sha256(code.encode("ascii")).digest(),
                session_id=session_id,
                target_host=SHELL_HOST,
                nonce_hash=hashlib.sha256(nonce.encode("utf-8")).digest(),
                expires_at=datetime.now(UTC) - timedelta(seconds=5),
            )
        client.cookies.set(CONTINUE_COOKIE, nonce, domain=SHELL_HOST)
        resp = await client.get(
            f"https://{SHELL_HOST}/auth/continue",
            params={"code": code, "return": _SHELL_RETURN},
        )
    assert resp.status_code == 400
    assert resp.json() == {"state": INVALID_GRANT}


async def test_a_grant_with_a_mismatched_nonce_cookie_is_invalid_grant(
    monkeypatch: pytest.MonkeyPatch, cluster: ClusterSession, owner_account_id: UUID
) -> None:
    _set_routing_mode(monkeypatch, "subdomain")
    async with _client() as client:
        code, _nonce = await _write_grant_via_http(client, cluster, owner_account_id)
        wrong_nonce = "a-completely-different-nonce"
        client.cookies.set(CONTINUE_COOKIE, wrong_nonce, domain=SHELL_HOST)
        resp = await client.get(
            f"https://{SHELL_HOST}/auth/continue",
            params={"code": code, "return": _SHELL_RETURN},
        )
    assert resp.status_code == 400
    assert resp.json() == {"state": INVALID_GRANT}


# --- B18: cross-host logout resolves session_revoked -------------------------


async def test_logout_on_one_host_revokes_the_session_everywhere(
    monkeypatch: pytest.MonkeyPatch, cluster: ClusterSession, owner_account_id: UUID
) -> None:
    _set_routing_mode(monkeypatch, "subdomain")
    subject = f"logout-{uuid7().hex[:12]}"
    identity = _bind_identity(cluster, owner_account_id, subject=subject)
    return_target = f"https://{SHELL_HOST}/dashboard"

    async with _client() as client:
        with _signed_in_as(identity):
            callback = await _login_and_callback(
                client, host=IDENTITY_HOST, return_target=return_target
            )
        assert callback.status_code == 302

        nonce = f"nonce-{uuid7().hex}"
        client.cookies.set(CONTINUE_COOKIE, nonce, domain=SHELL_HOST)
        at_identity = await client.get(
            f"https://{IDENTITY_HOST}/auth/continue",
            params={"return": return_target, "nonce": nonce},
        )
        assert at_identity.status_code == 302
        at_shell = await client.get(at_identity.headers["location"])
        assert at_shell.status_code == 302
        shell_session_value = _cookie_value(
            _cookie_headers(at_shell, SESSION_COOKIE)[0]
        )

        logout_resp = await client.post(
            f"https://{IDENTITY_HOST}/auth/logout",
            headers={"Origin": f"https://{IDENTITY_HOST}"},
        )
        assert logout_resp.status_code == 302

    async with _internal_client() as internal_client:
        resp = await internal_client.get(
            "/internal/v1/session",
            headers={
                "X-Rheo-Internal": INTERNAL_SECRET_VALUE,
                "X-Rheo-Session": shell_session_value,
                "X-Rheo-Host": SHELL_HOST,
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {"state": "session_revoked"}


# --- B18: listener isolation, both clauses ------------------------------------


async def test_public_listener_ignores_internal_and_session_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_routing_mode(monkeypatch, "path")
    async with _client() as client:
        plain = await client.get(f"https://{BASE_HOST}/auth/login")
        with_headers = await client.get(
            f"https://{BASE_HOST}/auth/login",
            headers={"X-Rheo-Session": "anything", "X-Rheo-Internal": "anything"},
        )
    assert plain.status_code == with_headers.status_code == 302
    for response in (plain, with_headers):
        oauth_cookie = _cookie_headers(response, OAUTH_STATE_COOKIE)[0]
        _assert_cookie_attrs(oauth_cookie)


async def test_internal_listener_ignores_cookies_and_requires_the_shared_secret(
    monkeypatch: pytest.MonkeyPatch, cluster: ClusterSession, owner_account_id: UUID
) -> None:
    _set_routing_mode(monkeypatch, "path")
    subject = f"internal-{uuid7().hex[:12]}"
    identity = _bind_identity(cluster, owner_account_id, subject=subject)
    async with _client() as client:
        with _signed_in_as(identity):
            callback = await _login_and_callback(
                client,
                host=BASE_HOST,
                return_target=f"https://{BASE_HOST}/dashboard",
            )
        assert callback.status_code == 302
        session_value = _cookie_value(_cookie_headers(callback, SESSION_COOKIE)[0])

    async with _internal_client() as internal_client:
        internal_client.cookies.set(SESSION_COOKIE, session_value)
        # (a) a valid X-Rheo-Internal header, a valid rheo_session COOKIE, but no
        # X-Rheo-Session header: resolves the same as no session at all.
        cookie_only = await internal_client.get(
            "/internal/v1/session",
            headers={
                "X-Rheo-Internal": INTERNAL_SECRET_VALUE,
                "X-Rheo-Host": BASE_HOST,
            },
        )
        # (b) no X-Rheo-Internal at all — cookie present or not — refused before
        # either route runs.
        no_secret = await internal_client.get(
            "/internal/v1/session", headers={"X-Rheo-Host": BASE_HOST}
        )
    assert cookie_only.status_code == 200
    assert cookie_only.json() == {"state": "session_missing"}
    assert no_secret.status_code == 401


# --- B3: workspace switching ---------------------------------------------------


async def test_workspace_switch_refuses_not_a_member_and_leaves_workspace_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    owner_account_id: UUID,
    workspace: UUID,
    make_workspace: MakeWorkspace,
) -> None:
    _set_routing_mode(monkeypatch, "path")
    with cluster.backend.control_engine.begin() as connection:
        stranger = insert_account(connection, display_name="stranger-owner").id
    other_workspace = make_workspace(owner=stranger)

    subject = f"switch-{uuid7().hex[:12]}"
    identity = _bind_identity(cluster, owner_account_id, subject=subject)
    async with _client() as client:
        with _signed_in_as(identity):
            callback = await _login_and_callback(
                client,
                host=BASE_HOST,
                return_target=f"https://{BASE_HOST}/dashboard",
            )
        session_value = _cookie_value(_cookie_headers(callback, SESSION_COOKIE)[0])

        first_switch = await client.post(
            f"https://{BASE_HOST}/auth/session/workspace",
            json={"target_workspace_id": str(workspace)},
            headers={"Origin": f"https://{BASE_HOST}"},
        )
        assert first_switch.status_code == 200

        bad_switch = await client.post(
            f"https://{BASE_HOST}/auth/session/workspace",
            json={"target_workspace_id": str(other_workspace)},
            headers={"Origin": f"https://{BASE_HOST}"},
        )
    assert bad_switch.status_code == 403
    assert bad_switch.json() == {"state": NOT_A_MEMBER}

    # The switch's own refusal left active_workspace_id unchanged; a fresh
    # internal call — with no identifier in its own URL — proves it still
    # resolves to the first, valid switch.
    async with _internal_client() as internal_client:
        resp = await internal_client.get(
            "/internal/v1/session",
            headers={
                "X-Rheo-Internal": INTERNAL_SECRET_VALUE,
                "X-Rheo-Session": session_value,
                "X-Rheo-Host": BASE_HOST,
            },
        )
    assert resp.json()["active_workspace_id"] == str(workspace)


async def test_post_endpoints_require_a_matching_origin(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    owner_account_id: UUID,
    workspace: UUID,
) -> None:
    _set_routing_mode(monkeypatch, "path")
    subject = f"origin-{uuid7().hex[:12]}"
    identity = _bind_identity(cluster, owner_account_id, subject=subject)
    async with _client() as client:
        with _signed_in_as(identity):
            callback = await _login_and_callback(
                client,
                host=BASE_HOST,
                return_target=f"https://{BASE_HOST}/dashboard",
            )
        assert callback.status_code == 302

        wrong_origin = await client.post(
            f"https://{BASE_HOST}/auth/session/workspace",
            json={"target_workspace_id": str(workspace)},
            headers={"Origin": "https://evil.example.net"},
        )
        assert wrong_origin.status_code == 403
        assert wrong_origin.json() == {"state": "origin_not_allowed"}

        missing_origin = await client.post(
            f"https://{BASE_HOST}/auth/session/workspace",
            json={"target_workspace_id": str(workspace)},
        )
        assert missing_origin.status_code == 403
        assert missing_origin.json() == {"state": "origin_not_allowed"}

        good_origin = await client.post(
            f"https://{BASE_HOST}/auth/session/workspace",
            json={"target_workspace_id": str(workspace)},
            headers={"Origin": f"https://{BASE_HOST}"},
        )
        assert good_origin.status_code == 200

        # The guard is on both state-changing POSTs, not just this one: a mutant
        # dropping it from /auth/logout specifically would otherwise survive.
        wrong_origin_logout = await client.post(
            f"https://{BASE_HOST}/auth/logout",
            headers={"Origin": "https://evil.example.net"},
        )
        assert wrong_origin_logout.status_code == 403
        assert wrong_origin_logout.json() == {"state": "origin_not_allowed"}

        missing_origin_logout = await client.post(f"https://{BASE_HOST}/auth/logout")
        assert missing_origin_logout.status_code == 403
        assert missing_origin_logout.json() == {"state": "origin_not_allowed"}

        good_origin_logout = await client.post(
            f"https://{BASE_HOST}/auth/logout",
            headers={"Origin": f"https://{BASE_HOST}"},
        )
    assert good_origin_logout.status_code == 302


# --- B5: the owner's and the member's own session, over /internal/v1/session ----------


async def test_owner_session_reports_role_owner_via_internal_session(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    owner_account_id: UUID,
    workspace: UUID,
) -> None:
    _set_routing_mode(monkeypatch, "path")
    subject = f"owner-{uuid7().hex[:12]}"
    identity = _bind_identity(cluster, owner_account_id, subject=subject)
    async with _client() as client:
        with _signed_in_as(identity):
            callback = await _login_and_callback(
                client,
                host=BASE_HOST,
                return_target=f"https://{BASE_HOST}/dashboard",
            )
        session_value = _cookie_value(_cookie_headers(callback, SESSION_COOKIE)[0])
        switch = await client.post(
            f"https://{BASE_HOST}/auth/session/workspace",
            json={"target_workspace_id": str(workspace)},
            headers={"Origin": f"https://{BASE_HOST}"},
        )
        assert switch.status_code == 200

    async with _internal_client() as internal_client:
        resp = await internal_client.get(
            "/internal/v1/session",
            headers={
                "X-Rheo-Internal": INTERNAL_SECRET_VALUE,
                "X-Rheo-Session": session_value,
                "X-Rheo-Host": BASE_HOST,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["actor"]["kind"] == "account"
    assert body["active_workspace_id"] == str(workspace)
    assert body["role"] == "owner"


async def test_member_add_via_cli_then_sign_in_yields_role_member(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    owner_account_id: UUID,
    workspace: UUID,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_routing_mode(monkeypatch, "path")
    subject = f"member-{uuid7().hex[:12]}"

    code, out, err = _run_cli(
        capsys,
        "account", "create",
        "--provider", FIXED_PROVIDER_ID,
        "--subject", subject,
        "--display-name", "second-member",
    )  # fmt: skip
    assert code == 0, err
    account_id = UUID(out.strip())

    code, out, err = _run_cli(
        capsys,
        "member", "add",
        "--workspace", str(workspace),
        "--account", str(account_id),
        "--role", "member",
    )  # fmt: skip
    assert code == 0, err
    assert out == ""
    assert "added" in err

    identity = ProviderIdentity(
        provider_id=FIXED_PROVIDER_ID,
        subject=subject,
        email=None,
        email_verified=False,
        display_name="second-member",
    )
    async with _client() as client:
        with _signed_in_as(identity):
            callback = await _login_and_callback(
                client,
                host=BASE_HOST,
                return_target=f"https://{BASE_HOST}/dashboard",
            )
        session_value = _cookie_value(_cookie_headers(callback, SESSION_COOKIE)[0])
        switch = await client.post(
            f"https://{BASE_HOST}/auth/session/workspace",
            json={"target_workspace_id": str(workspace)},
            headers={"Origin": f"https://{BASE_HOST}"},
        )
    assert switch.status_code == 200

    async with _internal_client() as internal_client:
        resp = await internal_client.get(
            "/internal/v1/session",
            headers={
                "X-Rheo-Internal": INTERNAL_SECRET_VALUE,
                "X-Rheo-Session": session_value,
                "X-Rheo-Host": BASE_HOST,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["role"] == "member"

    member_ctx = context_from_session(bytes.fromhex(session_value), BASE_HOST)
    assert isinstance(member_ctx, WorkspaceContext)
    outcome = dispatch(
        member_ctx,
        SETTINGS_SET,
        {"key": "identity.token_max_days.cli", "value": 45},
    )
    assert outcome.state == ROLE_NOT_PERMITTED

    owner_ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(owner_ctx, WorkspaceContext)
    success = dispatch(
        owner_ctx,
        SETTINGS_SET,
        {"key": "identity.token_max_days.cli", "value": 45},
    )
    assert success.ok, success


# --- memberships: the account-wide list behind /internal/v1/session's own field ------


async def test_internal_session_lists_memberships_across_both_workspaces(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    owner_account_id: UUID,
    workspace: UUID,
    make_workspace: MakeWorkspace,
) -> None:
    """The defect this fixes: ``memberships`` used to be built from the current
    context alone, so it always had exactly one entry -- the workspace the caller
    was already in -- which left the web workspace switcher (chunk 10) with
    nothing to switch to. An account with real memberships in two workspaces must
    get both back, with the active one present (not filtered out) and correctly
    marked by the sibling ``active_workspace_id`` field.

    Built directly through ``rheo_core.sessions.service`` (``create_session`` /
    ``mint_host_secret`` / ``switch_workspace``) rather than replaying the full
    OAuth dance other tests in this module use: nothing about this defect is
    login-flow-specific, and this mirrors the existing direct-mechanism idiom
    ``test_grant_presented_on_the_wrong_host_is_invalid_grant`` already uses above,
    for the same reason.
    """
    _set_routing_mode(monkeypatch, "path")
    second_workspace = make_workspace(owner=owner_account_id)

    with cluster.backend.control_engine.connect() as connection:
        rows = list_memberships(connection, account_id=owner_account_id)
    # The repository's own ordering contract: oldest membership first. `workspace`
    # (the fixture, provisioned before this test body ran) precedes
    # `second_workspace` (provisioned just above) -- proving the `created_at`
    # ordering this run chose, not an arbitrary database order.
    assert [row.workspace_id for row in rows] == [workspace, second_workspace]

    session_row = create_session(owner_account_id)
    secret = mint_host_secret(session_row.id, BASE_HOST)
    assert switch_workspace(session_row.id, workspace) is None

    async with _internal_client() as internal_client:
        resp = await internal_client.get(
            "/internal/v1/session",
            headers={
                "X-Rheo-Internal": INTERNAL_SECRET_VALUE,
                "X-Rheo-Session": secret.hex(),
                "X-Rheo-Host": BASE_HOST,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    assert body["active_workspace_id"] == str(workspace)
    assert body["memberships"] == [
        {"workspace_id": str(workspace), "role": "owner"},
        {"workspace_id": str(second_workspace), "role": "owner"},
    ]


async def test_internal_session_lists_exactly_one_membership_when_only_one(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    owner_account_id: UUID,
    workspace: UUID,
) -> None:
    """Regression: an account in exactly one workspace still gets exactly one
    membership entry back -- the shape the field already had before this fix, now
    produced by the real account-wide query instead of by construction from
    ``ctx`` alone."""
    _set_routing_mode(monkeypatch, "path")
    session_row = create_session(owner_account_id)
    secret = mint_host_secret(session_row.id, BASE_HOST)
    assert switch_workspace(session_row.id, workspace) is None

    async with _internal_client() as internal_client:
        resp = await internal_client.get(
            "/internal/v1/session",
            headers={
                "X-Rheo-Internal": INTERNAL_SECRET_VALUE,
                "X-Rheo-Session": secret.hex(),
                "X-Rheo-Host": BASE_HOST,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["memberships"] == [
        {"workspace_id": str(workspace), "role": "owner"}
    ]


# --- the production/https startup invariant --------------------------------


def test_check_production_scheme_refuses_production_over_http() -> None:
    """New startup safety logic (this chunk's own), mirroring the sibling
    env-reference check that is already tested: the ``RuntimeError`` fires for
    ``profile="production"`` with ``scheme="http"`` and does not otherwise."""
    with pytest.raises(RuntimeError, match="production"):
        _check_production_scheme("production", "http")
    _check_production_scheme("production", "https")
    _check_production_scheme("development", "http")
    _check_production_scheme("test", "http")
