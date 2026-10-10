"""Issue #290: core keeps ``/api/*`` on the ``api`` host and serves the identity
routes at ``routing.identity.path``, without relying on the reverse proxy.

Seams under test: ``rheo_app_core.main.app`` with its real lifespan (the rules are
built there, as the ``mcp`` mount is), driven over ``httpx.ASGITransport`` with an
explicit ``Host`` header per request: exactly what a proxy that routed a host to
``core`` would forward. A dispatch counter wrapped round ``api_routes.dispatch``
separates "refused before the route" from "reached the route and refused there".
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
import pytest
from rheo_app_core import api_routes
from rheo_app_core.main import app, lifespan
from rheo_app_core.routing import routing_config
from rheo_app_core.surface_guard import STATE_ATTRIBUTE, SurfaceRules
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.core_ops import TOKEN_ISSUE, WORKSPACE_STATUS
from rheo_core.routing import SHELL, application_hosts, url_for

pytestmark = pytest.mark.postgres

BASE_HOST = "example.test"
API_HOST = f"api.{BASE_HOST}"
SHELL_HOST = f"circuit.{BASE_HOST}"
IDENTITY_HOST = f"auth.{BASE_HOST}"
STATUS_PATH = f"/api/v1/operations/{WORKSPACE_STATUS}"


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture
def api_token(workspace: UUID, owner_account_id: UUID) -> str:
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    outcome = dispatch(ctx, TOKEN_ISSUE, {"kind": "cli", "set_name": "cli_full"})
    assert outcome.ok, outcome
    assert outcome.result is not None
    return str(outcome.result.value)  # type: ignore[attr-defined]


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every operation name the bearer route actually dispatched."""
    seen: list[str] = []
    real = api_routes.dispatch

    def counting(ctx: Any, name: str, payload: Any, **kwargs: Any) -> Any:
        seen.append(name)
        return real(ctx, name, payload, **kwargs)

    monkeypatch.setattr(api_routes, "dispatch", counting)
    return seen


def _set_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str, *, identity_path: str | None = None
) -> None:
    monkeypatch.setenv("RHEO__routing__mode", mode)
    monkeypatch.setenv("RHEO__routing__scheme", "https")
    monkeypatch.setenv("RHEO__routing__base_host", BASE_HOST)
    monkeypatch.setenv("RHEO__routing__public_host", "")
    if identity_path is not None:
        monkeypatch.setenv("RHEO__routing__identity__path", identity_path)


@contextlib.asynccontextmanager
async def _wire() -> AsyncIterator[httpx.AsyncClient]:
    async with lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"https://{BASE_HOST}"
        ) as client:
            yield client


def _bearer(value: str, host: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}", "Host": host}


# --- /api/* is the api host's, in subdomain mode ------------------------------------


async def test_subdomain_mode_serves_the_api_on_the_api_host(
    monkeypatch: pytest.MonkeyPatch, api_token: str, dispatched: list[str]
) -> None:
    _set_mode(monkeypatch, "subdomain")
    async with _wire() as client:
        for host in (API_HOST, f"{API_HOST}:443", f"{API_HOST}.", "API.Example.Test"):
            response = await client.post(
                STATUS_PATH, json={}, headers=_bearer(api_token, host)
            )
            assert response.status_code == 200, (host, response.text)
            assert response.json()["state"] == "succeeded"
        health = await client.get("/healthz", headers={"Host": API_HOST})
    assert health.status_code == 200, health.text
    assert dispatched == [WORKSPACE_STATUS] * 4


async def test_subdomain_mode_refuses_the_api_on_every_other_host(
    monkeypatch: pytest.MonkeyPatch, api_token: str, dispatched: list[str]
) -> None:
    """Every application host (shell, identity, each loaded module's), the bare base
    host and a stranger: a valid token still gets a plain 404, and the route is
    never entered. ``/api`` and ``/api/`` themselves are covered, ``/apis`` is not
    the surface's and stays FastAPI's ordinary 404."""
    _set_mode(monkeypatch, "subdomain")
    async with _wire() as client:
        hosts = sorted(application_hosts(routing_config()))
        assert SHELL_HOST in hosts and IDENTITY_HOST in hosts
        hosts += [BASE_HOST, "elsewhere.example.org"]
        for host in hosts:
            for path in (STATUS_PATH, "/api", "/api/", "/api/v1/operations/"):
                response = await client.post(
                    path, json={}, headers=_bearer(api_token, host)
                )
                assert response.status_code == 404, (host, path, response.text)
                assert response.json() == {"detail": "Not Found"}
        unrelated = await client.get("/apis", headers={"Host": SHELL_HOST})
    assert unrelated.status_code == 404
    assert dispatched == []


async def test_path_mode_still_serves_the_api_on_its_one_host(
    monkeypatch: pytest.MonkeyPatch, api_token: str, dispatched: list[str]
) -> None:
    """Path mode has one host and no host check, as before (the ``mcp`` surface is
    served the same way): the base host and a loopback name both dispatch."""
    _set_mode(monkeypatch, "path")
    async with _wire() as client:
        for host in (BASE_HOST, "127.0.0.1:8000"):
            response = await client.post(
                STATUS_PATH, json={}, headers=_bearer(api_token, host)
            )
            assert response.status_code == 200, (host, response.text)
    assert dispatched == [WORKSPACE_STATUS] * 2


# --- the identity routes follow routing.identity.path -------------------------------


def _shell_url() -> str:
    return url_for(routing_config(), SHELL, "/")


@pytest.mark.parametrize("mode", ["path", "subdomain"])
async def test_a_moved_identity_path_is_served_and_the_old_one_is_not(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """With ``routing.identity.path`` at ``/id``, ``/id/continue`` is the identity
    handler (its no-session redirect names ``/id/login``, which ``identity_path``
    built), ``/id/login`` reaches the login handler (the 503 a disabled provider
    answers, not a 404), and the fixed ``/auth/*`` paths answer 404."""
    _set_mode(monkeypatch, mode, identity_path="/id")
    host = BASE_HOST if mode == "path" else IDENTITY_HOST
    async with _wire() as client:
        query = urlencode({"return": _shell_url()})
        moved = await client.get(f"/id/continue?{query}", headers={"Host": host})
        assert moved.status_code == 302, moved.text
        location = moved.headers["location"]
        assert urlsplit(location).path == "/id/login"
        login = await client.get(location, headers={"Host": host})
        old_continue = await client.get(
            f"/auth/continue?{query}", headers={"Host": host}
        )
        old_login = await client.get("/auth/login", headers={"Host": host})
        old_root = await client.get("/auth", headers={"Host": host})
    assert login.status_code == 503, login.text
    assert login.json()["state"] == "identity_provider_unavailable"
    for response in (old_continue, old_login, old_root):
        assert response.status_code == 404, response.text


@pytest.mark.parametrize("mode", ["path", "subdomain"])
async def test_the_default_identity_path_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _set_mode(monkeypatch, mode)
    host = BASE_HOST if mode == "path" else IDENTITY_HOST
    async with _wire() as client:
        query = urlencode({"return": _shell_url()})
        response = await client.get(f"/auth/continue?{query}", headers={"Host": host})
    assert response.status_code == 302, response.text
    assert urlsplit(response.headers["location"]).path == "/auth/login"


@pytest.mark.parametrize(
    ("mode", "prefix"),
    [
        ("path", "/api"),
        ("subdomain", "/api/auth"),
        ("path", "/healthz"),
        ("subdomain", "/.well-known"),
        ("path", "/mcp"),
        ("path", "/mcp/auth"),
    ],
)
async def test_an_identity_path_over_another_core_route_fails_startup(
    monkeypatch: pytest.MonkeyPatch, mode: str, prefix: str
) -> None:
    _set_mode(monkeypatch, mode, identity_path=prefix)
    with pytest.raises(ValueError, match=r"routing\.identity\.path"):
        async with lifespan(app):
            pass
    assert getattr(app.state, STATE_ATTRIBUTE, None) is None


async def test_rules_are_published_only_while_the_lifespan_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch, "subdomain")
    assert getattr(app.state, STATE_ATTRIBUTE, None) is None
    async with lifespan(app):
        rules = getattr(app.state, STATE_ATTRIBUTE, None)
        assert rules == SurfaceRules(api_host=API_HOST, identity_prefix="/auth")
    assert getattr(app.state, STATE_ATTRIBUTE, None) is None
