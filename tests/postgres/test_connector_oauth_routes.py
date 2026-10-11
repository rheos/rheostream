"""The connector OAuth callback on the identity host, over the wire.

Seams under test: ``GET <identity prefix>/connector/{module_id}/callback`` through
``rheo_app_core.main.app`` with its real lifespan (so ``surface_guard``'s rules are
live) and the real dispatcher, over ``httpx.ASGITransport`` with an explicit
``Host`` per request. The switch-gated 404 and the absent client document; the
session rule; the four bounded parameters reaching the declared operation; the two
fixed ``no-store`` pages; no request text in any response.

The completing module is the harness: a manifest for ``harness`` built with
``harness.modules.manifest`` declares an owner-only ``authorization_completion``
operation registered on the process registry under the test-harness origin. The
route's lookup of the loaded manifests is extended with it (and with a module that
declares nothing) for the length of the lifespan. The ``harness`` module row is
enabled in the workspace, so the real dispatcher authorizes and runs the operation. A
spy wrapped round the route's ``dispatch`` records what the route sent; the handler
records what actually ran.
"""

import contextlib
import logging
import subprocess
import sys
import traceback
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
import pytest
from conftest import ClusterSession
from harness.modules import manifest
from harness.registry import add_member, enable_harness_module, register_harness
from pydantic import BaseModel, ConfigDict
from rheo_app_core import connector_oauth_routes
from rheo_app_core.auth_routes import identity_host
from rheo_app_core.connector_oauth_routes import (
    CALLBACK_PATH,
    CONNECTED_PAGE,
    CONNECTED_TEXT,
    MAX_PARAMETER_LENGTH,
    REFUSED_PAGE,
    REFUSED_TEXT,
)
from rheo_app_core.main import app, lifespan
from rheo_app_core.routing import routing_config
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.connectors import oauth
from rheo_core.modules import loader as loader_module
from rheo_core.operations import REGISTRY, OperationRefused, register_core_operations
from rheo_core.sessions import create_session, mint_host_secret, switch_workspace
from rheo_core.sessions.cookies import SESSION_COOKIE
from rheo_core.settings import TEST_HARNESS_ORIGIN
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork

pytestmark = pytest.mark.postgres

BASE_HOST = "example.test"
MODES = ("subdomain", "path")
ENABLED_ENV = "RHEO__connectors__oauth_client__enabled"
IDENTITY_PATH_ENV = "RHEO__routing__identity__path"
HARNESS = "harness"
PLAIN = "plain_probe"
UNKNOWN = "absent_probe"
COMPLETE = "harness.authorization.complete"
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
MARK: Final = "reflect-marker-7c41e9"
"""Put into every request value; no response body may contain it."""

ROOT = Path(__file__).resolve().parents[2]


# --- the completing operation ------------------------------------------------------


class CompleteInput(BaseModel):
    """The four parameters, each optional; anything else is ``input_invalid``."""

    model_config = ConfigDict(extra="forbid")

    state: str | None = None
    code: str | None = None
    error: str | None = None
    iss: str | None = None


class CompleteResult(BaseModel):
    state: str


class Handler:
    """What ran, and what the next run answers (``connected`` unless told)."""

    def __init__(self) -> None:
        self.ran: list[dict[str, Any]] = []
        self.answer: str = "connected"
        self.raises: BaseException | None = None

    def __call__(
        self, ctx: WorkspaceContext, uow: HandlerUnitOfWork, model_input: CompleteInput
    ) -> CompleteResult:
        self.ran.append(model_input.model_dump(exclude_unset=True))
        if self.raises is not None:
            raise self.raises
        return CompleteResult(state=self.answer)


HANDLER: Final = Handler()

COMPLETE_DECLARATION: Final = OperationDeclaration(
    name=COMPLETE,
    safety_class=SafetyClass.MUTATE,
    roles=frozenset({Role.OWNER}),
    input_model=CompleteInput,
    output=CompleteResult,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)

COMPLETING_MANIFEST: Final = manifest(
    HARNESS,
    operations=((COMPLETE_DECLARATION, HANDLER),),
    authorization_completion=COMPLETE,
)
PLAIN_MANIFEST: Final = manifest(PLAIN)
"""A loaded module that declares no completion operation."""


@pytest.fixture(autouse=True)
def registrations() -> Iterator[None]:
    register_core_operations()
    register_harness()
    REGISTRY.register(COMPLETE_DECLARATION, HANDLER, origin=TEST_HARNESS_ORIGIN)
    HANDLER.ran.clear()
    HANDLER.answer = "connected"
    HANDLER.raises = None
    yield
    HANDLER.raises = None


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Every (operation, payload) the route handed the dispatcher."""
    seen: list[tuple[str, dict[str, Any]]] = []
    real = connector_oauth_routes.dispatch

    def counting(ctx: Any, name: str, payload: Any, **kwargs: Any) -> Any:
        seen.append((name, dict(payload)))
        return real(ctx, name, payload, **kwargs)

    monkeypatch.setattr(connector_oauth_routes, "dispatch", counting)
    return seen


@pytest.fixture
def harness_enabled(cluster: ClusterSession, workspace: UUID) -> UUID:
    database = cluster.registry_row(workspace).database_name
    with UnitOfWork(cluster.backend.pools.engine_for(database), database) as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    return workspace


# --- wiring ------------------------------------------------------------------------


def _set_mode(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    *,
    enabled: bool = True,
    identity_path: str | None = None,
) -> None:
    monkeypatch.setenv("RHEO__routing__mode", mode)
    monkeypatch.setenv("RHEO__routing__scheme", "https")
    monkeypatch.setenv("RHEO__routing__base_host", BASE_HOST)
    monkeypatch.setenv("RHEO__routing__public_host", "")
    monkeypatch.setenv(ENABLED_ENV, "true" if enabled else "false")
    if identity_path is not None:
        monkeypatch.setenv(IDENTITY_PATH_ENV, identity_path)


@contextlib.asynccontextmanager
async def _wire(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    async with lifespan(app):
        # After startup's own module load. The route's view of the loaded table
        # gains the two fixture manifests; the dispatcher's does not, so ``harness``
        # keeps the authorization every harness operation has (it is enabled by its
        # module row) and is not held to a loaded module's readiness checks.
        loaded = dict(loader_module.loaded_manifests())
        loaded.update({HARNESS: COMPLETING_MANIFEST, PLAIN: PLAIN_MANIFEST})
        monkeypatch.setattr(
            connector_oauth_routes, "loaded_manifests", lambda: dict(loaded)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"https://{BASE_HOST}"
        ) as client:
            yield client


def _identity_host() -> str:
    return identity_host(routing_config())


def _served_path(module_id: str) -> str:
    return CALLBACK_PATH.format(module_id=module_id)


def _callback_path(module_id: str) -> str:
    """The path of the redirect URI the module registers (switch on only)."""
    return urlsplit(oauth.redirect_uri(module_id)).path


def _session_cookie(account_id: UUID, workspace_id: UUID, host: str) -> str:
    row = create_session(account_id)
    secret = mint_host_secret(row.id, host)
    assert switch_workspace(row.id, workspace_id) is None
    return f"{SESSION_COOKIE}={secret.hex()}"


def _headers(host: str, cookie: str | None = None) -> dict[str, str]:
    headers = {"Host": host}
    if cookie is not None:
        headers["Cookie"] = cookie
    return headers


def _marked() -> dict[str, str]:
    return {
        "state": f"{MARK}-state",
        "code": f"{MARK}-code",
        "error": f"{MARK}-error",
        "iss": f"https://{MARK}.example.com",
        "error_description": f"{MARK}-description",
        "unrelated": f"{MARK}-unrelated",
    }


def _assert_page(response: httpx.Response, *, connected: bool) -> None:
    assert response.status_code == (200 if connected else 400), response.text
    assert response.text == (CONNECTED_PAGE if connected else REFUSED_PAGE)
    assert (CONNECTED_TEXT if connected else REFUSED_TEXT) in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-type"].startswith("text/html")
    assert "location" not in response.headers
    assert MARK not in response.text


async def _unrouted(client: httpx.AsyncClient, host: str) -> httpx.Response:
    return await client.get("/nothing-is-routed-here", headers=_headers(host))


def _same_as_unrouted(response: httpx.Response, unrouted: httpx.Response) -> None:
    assert unrouted.status_code == 404
    assert response.status_code == 404, response.text
    assert response.json() == unrouted.json()


# --- the switch gate and the absent client document --------------------------------


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("enabled", (True, False))
async def test_there_is_no_client_document_route(
    monkeypatch: pytest.MonkeyPatch,
    harness_enabled: UUID,
    owner_account_id: UUID,
    mode: str,
    enabled: bool,
    dispatched: list[tuple[str, dict[str, Any]]],
) -> None:
    _set_mode(monkeypatch, mode, enabled=enabled)
    host = _identity_host()
    cookie = _session_cookie(owner_account_id, harness_enabled, host)
    async with _wire(monkeypatch) as client:
        unrouted = await _unrouted(client, host)
        for path in (
            "/auth/connector/client.json",
            f"/auth/connector/{HARNESS}/client.json",
        ):
            response = await client.get(path, headers=_headers(host, cookie))
            _same_as_unrouted(response, unrouted)
    assert dispatched == []
    assert HANDLER.ran == []


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("switch", ("default", "false"))
async def test_the_callback_is_an_unrouted_404_while_the_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
    harness_enabled: UUID,
    owner_account_id: UUID,
    mode: str,
    switch: str,
    dispatched: list[tuple[str, dict[str, Any]]],
) -> None:
    _set_mode(monkeypatch, mode, enabled=False)
    if switch == "default":
        monkeypatch.delenv(ENABLED_ENV)
    assert not oauth.enabled()
    host = _identity_host()
    cookie = _session_cookie(owner_account_id, harness_enabled, host)
    async with _wire(monkeypatch) as client:
        unrouted = await _unrouted(client, host)
        response = await client.get(
            f"{_served_path(HARNESS)}?{urlencode(_marked())}",
            headers=_headers(host, cookie),
        )
        _same_as_unrouted(response, unrouted)
        assert MARK not in response.text
        assert dispatched == []
        assert HANDLER.ran == []
        # The control: the identical request, with only the switch turned on.
        monkeypatch.setenv(ENABLED_ENV, "true")
        control = await client.get(
            f"{_served_path(HARNESS)}?{urlencode(_marked())}",
            headers=_headers(host, cookie),
        )
    _assert_page(control, connected=True)
    assert [name for name, _ in dispatched] == [COMPLETE]


@pytest.mark.parametrize("mode", MODES)
async def test_the_callback_is_404_when_the_identity_path_is_not_the_served_one(
    monkeypatch: pytest.MonkeyPatch,
    harness_enabled: UUID,
    owner_account_id: UUID,
    mode: str,
    dispatched: list[tuple[str, dict[str, Any]]],
) -> None:
    _set_mode(monkeypatch, mode, identity_path="/signin")
    assert not oauth.enabled()
    host = _identity_host()
    cookie = _session_cookie(owner_account_id, harness_enabled, host)
    async with _wire(monkeypatch) as client:
        unrouted = await _unrouted(client, host)
        for path in (
            f"/signin/connector/{HARNESS}/callback",
            _served_path(HARNESS),
        ):
            response = await client.get(
                f"{path}?{urlencode(_marked())}", headers=_headers(host, cookie)
            )
            _same_as_unrouted(response, unrouted)
    assert dispatched == []
    assert HANDLER.ran == []
    # The control: the served prefix again, everything else unchanged.
    monkeypatch.delenv(IDENTITY_PATH_ENV)
    async with _wire(monkeypatch) as client:
        control = await client.get(
            f"{_served_path(HARNESS)}?{urlencode(_marked())}",
            headers=_headers(host, cookie),
        )
    _assert_page(control, connected=True)
    assert [name for name, _ in dispatched] == [COMPLETE]


async def test_the_callback_is_served_on_the_identity_host_only(
    monkeypatch: pytest.MonkeyPatch,
    harness_enabled: UUID,
    owner_account_id: UUID,
    dispatched: list[tuple[str, dict[str, Any]]],
) -> None:
    _set_mode(monkeypatch, "subdomain")
    config = routing_config()
    others = (
        f"{config.surfaces.shell.host}.{BASE_HOST}",
        f"{config.surfaces.api.host}.{BASE_HOST}",
        BASE_HOST,
    )
    async with _wire(monkeypatch) as client:
        for host in others:
            cookie = _session_cookie(owner_account_id, harness_enabled, host)
            unrouted = await _unrouted(client, host)
            response = await client.get(
                f"{_callback_path(HARNESS)}?{urlencode({'state': 's', 'code': 'c'})}",
                headers=_headers(host, cookie),
            )
            _same_as_unrouted(response, unrouted)
        assert dispatched == []
        assert HANDLER.ran == []
        # The control: the identity host, with its own session.
        identity = _identity_host()
        control = await client.get(
            f"{_callback_path(HARNESS)}?{urlencode({'state': 's', 'code': 'c'})}",
            headers=_headers(
                identity, _session_cookie(owner_account_id, harness_enabled, identity)
            ),
        )
    _assert_page(control, connected=True)
    assert dispatched == [(COMPLETE, {"state": "s", "code": "c"})]


# --- refusals before dispatch ------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_no_valid_session_dispatches_and_consumes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    harness_enabled: UUID,
    owner_account_id: UUID,
    mode: str,
    dispatched: list[tuple[str, dict[str, Any]]],
) -> None:
    _set_mode(monkeypatch, mode)
    host = _identity_host()
    other_host = f"elsewhere.{BASE_HOST}"
    foreign = _session_cookie(owner_account_id, harness_enabled, other_host)
    query = urlencode(_marked())
    async with _wire(monkeypatch) as client:
        for cookie in (
            None,
            f"{SESSION_COOKIE}=",
            f"{SESSION_COOKIE}=not-hex-{MARK}",
            f"{SESSION_COOKIE}={'ab' * 32}",
            foreign,
        ):
            response = await client.get(
                f"{_callback_path(HARNESS)}?{query}", headers=_headers(host, cookie)
            )
            _assert_page(response, connected=False)
        assert dispatched == []
        assert HANDLER.ran == []
        # The control: the same request with the owner's live session on this host.
        own = _session_cookie(owner_account_id, harness_enabled, host)
        control = await client.get(
            f"{_callback_path(HARNESS)}?{query}", headers=_headers(host, own)
        )
    _assert_page(control, connected=True)
    assert [name for name, _ in dispatched] == [COMPLETE]
    assert len(HANDLER.ran) == 1


@pytest.mark.parametrize("module_id", (UNKNOWN, PLAIN, f"{MARK}-module"))
async def test_an_unknown_module_or_one_without_a_declaration_dispatches_nothing(
    monkeypatch: pytest.MonkeyPatch,
    harness_enabled: UUID,
    owner_account_id: UUID,
    module_id: str,
    dispatched: list[tuple[str, dict[str, Any]]],
) -> None:
    _set_mode(monkeypatch, "subdomain")
    host = _identity_host()
    cookie = _session_cookie(owner_account_id, harness_enabled, host)
    async with _wire(monkeypatch) as client:
        response = await client.get(
            f"{_served_path(module_id)}?{urlencode(_marked())}",
            headers=_headers(host, cookie),
        )
    _assert_page(response, connected=False)
    assert dispatched == []
    assert HANDLER.ran == []


async def test_an_over_long_or_repeated_parameter_is_refused_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    harness_enabled: UUID,
    owner_account_id: UUID,
    dispatched: list[tuple[str, dict[str, Any]]],
) -> None:
    _set_mode(monkeypatch, "subdomain")
    host = _identity_host()
    cookie = _session_cookie(owner_account_id, harness_enabled, host)
    too_long = MARK + "x" * (MAX_PARAMETER_LENGTH + 1 - len(MARK))
    async with _wire(monkeypatch) as client:
        for name in ("state", "code", "error", "iss"):
            response = await client.get(
                f"{_callback_path(HARNESS)}?{urlencode({name: too_long})}",
                headers=_headers(host, cookie),
            )
            _assert_page(response, connected=False)
            repeated = urlencode([(name, f"{MARK}-1"), (name, f"{MARK}-2")])
            response = await client.get(
                f"{_callback_path(HARNESS)}?{repeated}", headers=_headers(host, cookie)
            )
            _assert_page(response, connected=False)
        # An over-long value of a parameter that is never read changes nothing.
        exact = "s" * MAX_PARAMETER_LENGTH
        response = await client.get(
            f"{_callback_path(HARNESS)}?"
            f"{urlencode({'state': exact, 'error_description': too_long})}",
            headers=_headers(host, cookie),
        )
        _assert_page(response, connected=True)
    assert dispatched == [(COMPLETE, {"state": exact})]
    assert HANDLER.ran == [{"state": exact}]


# --- dispatch --------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_a_valid_session_dispatches_exactly_the_declared_operation(
    monkeypatch: pytest.MonkeyPatch,
    harness_enabled: UUID,
    owner_account_id: UUID,
    mode: str,
    dispatched: list[tuple[str, dict[str, Any]]],
) -> None:
    _set_mode(monkeypatch, mode)
    host = _identity_host()
    cookie = _session_cookie(owner_account_id, harness_enabled, host)
    marked = _marked()
    four = {name: marked[name] for name in ("state", "code", "error", "iss")}
    async with _wire(monkeypatch) as client:
        response = await client.get(
            f"{_callback_path(HARNESS)}?{urlencode(marked)}",
            headers=_headers(host, cookie),
        )
        _assert_page(response, connected=True)
        # Absent keys are omitted, not sent empty.
        partial = await client.get(
            f"{_callback_path(HARNESS)}?"
            f"{urlencode({'code': 'c-1', 'state': 's-1', 'iss': 'i-1'})}",
            headers=_headers(host, cookie),
        )
        _assert_page(partial, connected=True)
    assert dispatched == [
        (COMPLETE, four),
        (COMPLETE, {"state": "s-1", "code": "c-1", "iss": "i-1"}),
    ]
    assert HANDLER.ran == [four, {"state": "s-1", "code": "c-1", "iss": "i-1"}]
    for _, payload in dispatched:
        assert "error_description" not in payload
        assert "unrelated" not in payload


async def test_a_member_session_reaches_the_dispatcher_and_is_refused_there(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    harness_enabled: UUID,
    dispatched: list[tuple[str, dict[str, Any]]],
) -> None:
    _set_mode(monkeypatch, "subdomain")
    host = _identity_host()
    member = add_member(
        cluster.backend, harness_enabled, Role.MEMBER, display_name="member-one"
    )
    cookie = _session_cookie(member, harness_enabled, host)
    async with _wire(monkeypatch) as client:
        response = await client.get(
            f"{_callback_path(HARNESS)}?{urlencode({'state': 's', 'code': 'c'})}",
            headers=_headers(host, cookie),
        )
    _assert_page(response, connected=False)
    assert dispatched == [(COMPLETE, {"state": "s", "code": "c"})]
    assert HANDLER.ran == []


@pytest.mark.parametrize(
    "outcome",
    ("refused-by-operation", "other-state", "raises", "dispatcher-raises"),
)
async def test_every_outcome_but_connected_is_the_refusal_page(
    monkeypatch: pytest.MonkeyPatch,
    harness_enabled: UUID,
    owner_account_id: UUID,
    outcome: str,
    dispatched: list[tuple[str, dict[str, Any]]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    _set_mode(monkeypatch, "subdomain")
    host = _identity_host()
    cookie = _session_cookie(owner_account_id, harness_enabled, host)
    if outcome == "refused-by-operation":
        HANDLER.raises = OperationRefused("refused", f"{MARK} provider said no")
    elif outcome == "other-state":
        HANDLER.answer = f"{MARK}-not-connected"
    elif outcome == "raises":
        HANDLER.raises = RuntimeError(f"{MARK} unexpected")
    else:

        def exploding(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"{MARK} dispatcher")

        monkeypatch.setattr(connector_oauth_routes, "dispatch", exploding)
    async with _wire(monkeypatch) as client:
        response = await client.get(
            f"{_callback_path(HARNESS)}?{urlencode(_marked())}",
            headers=_headers(host, cookie),
        )
    _assert_page(response, connected=False)
    if outcome != "dispatcher-raises":
        assert len(dispatched) == 1
        assert len(HANDLER.ran) == 1
    if outcome in ("raises", "dispatcher-raises"):
        # The exception's text carries the marker; no server-side log record may.
        # The test's own httpx client logs the URL it requested, so its records
        # (the ``httpx``/``httpcore`` loggers, client side only) are left out.
        server = [
            record
            for record in caplog.records
            if record.name.split(".", 1)[0] not in _CLIENT_LOGGERS
        ]
        assert server
        for record in server:
            assert MARK not in _rendered(record), record.name
    if outcome == "dispatcher-raises":
        # The route's own line names the exception type and nothing else.
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "rheo_app_core.connector_oauth_routes"
        ] == ["connector_callback_unexpected_error RuntimeError"]


_CLIENT_LOGGERS: Final = frozenset({"httpx", "httpcore"})


def _rendered(record: logging.LogRecord) -> str:
    """Everything a handler could write for ``record``: the formatted message, the
    raw message and arguments, and any exception or stack text."""
    parts = [record.getMessage(), str(record.msg), repr(record.args)]
    if record.exc_info:
        parts.extend(traceback.format_exception(*record.exc_info))
    parts.extend(str(text) for text in (record.exc_text, record.stack_info) if text)
    return "\n".join(parts)


async def test_no_request_text_is_reflected_in_any_response(
    monkeypatch: pytest.MonkeyPatch,
    harness_enabled: UUID,
    owner_account_id: UUID,
    dispatched: list[tuple[str, dict[str, Any]]],
) -> None:
    _set_mode(monkeypatch, "subdomain")
    host = _identity_host()
    cookie = _session_cookie(owner_account_id, harness_enabled, host)
    query = urlencode(_marked())
    bodies: list[str] = []
    async with _wire(monkeypatch) as client:
        for path, with_cookie in (
            (_callback_path(HARNESS), True),
            (_callback_path(HARNESS), False),
            (_served_path(f"{MARK}-module"), True),
            (_served_path(PLAIN), True),
        ):
            response = await client.get(
                f"{path}?{query}",
                headers=_headers(host, cookie if with_cookie else None),
            )
            bodies.append(response.text)
            assert MARK not in response.text
            assert all(MARK not in value for value in response.headers.values())
    assert set(bodies) == {CONNECTED_PAGE, REFUSED_PAGE}
    # The marker really reached the server: it is in what was dispatched.
    assert dispatched and all(
        MARK in value for _, payload in dispatched for value in payload.values()
    )


# --- the routing-literal gate -------------------------------------------------------


def test_the_module_passes_the_routing_literal_gate_with_the_allowlist_unchanged() -> (
    None
):
    script = ROOT / "scripts" / "check_routing_literals.py"
    assert "connector_oauth_routes" not in script.read_text(encoding="utf-8")
    source = (
        ROOT / "apps" / "core" / "src" / "rheo_app_core" / "connector_oauth_routes.py"
    ).read_text(encoding="utf-8")
    for literal in ("/auth/", "/api/", "/mcp", "http://", "https://"):
        assert literal not in source, literal
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
