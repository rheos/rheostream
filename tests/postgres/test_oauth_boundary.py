"""The OAuth connector's cross-cutting claims (issue #287): AC-15, AC-17, AC-21, and
FR 16's ``last_used_at`` half.

Seams under test: ``rheo_app_core.main.app`` with its real lifespan, driven by the
test-double connector (``harness.oauth_client``), with the feature configured, off,
or enabled but incomplete, in both routing modes.

- **AC-15:** a connector access token on the ``api`` surface is ``token_wrong_kind``;
  on ``mcp`` it lists the connector set's tools; a tool outside its snapshot gets the
  façade's ``not_found`` (the shipped MCP answer for a tool the token cannot see), and
  an operation outside its snapshot dispatched with the token's own context is
  ``operation_not_permitted``. A call is dispatched through the façade's one
  ``dispatch`` exactly once, as for a token minted by ``rheo token issue``.
- **AC-17:** one full flow (register, authorize, login, consent, exchange, call,
  refresh, and the refusals a connector can provoke) against a capturing log
  handler. Every server log line, every request line as uvicorn's ``uvicorn.access``
  logger records it (filter attached by ``serve.server_configs``, the path
  ``serve()`` runs), every ``oauth_event``, ``core.operation``, ``core.audit_record``
  and tool-telemetry row, the grant's token rows, and every error body is scanned
  for each access token, refresh token, code, verifier and challenge. A bearer in a
  query string is refused on ``mcp`` and ``api``.
- **AC-21:** off, and enabled with the GitHub provider disabled: a bare ``Bearer``
  401, the well-known paths 404, ``/auth/oauth/*`` 404, and doctor's ``oauth
  surface`` line (``FAIL`` for the incomplete case). Configured: every URL the two
  documents advertise answers neither 404 nor 500 and equals its ``url_for``-derived
  value, in both modes.
- **FR 16:** each existing caller's token row (Claude Code's ``rheo token issue``
  ``mcp`` token, the bridge's ``cli`` token, the box bot's ``agent_default`` token)
  and a connector token: ``last_used_at`` is set on first use, advances on the next
  and is untouched by a refused presentation, through the same unchanged
  ``resolve_token``.

Log capture runs at INFO, the level ``configure_logging`` gives production. The test
client's own library loggers (``httpx2``, ``httpcore``) are not the server's and are
left out of the scan: they log the URLs the client requested, which is the client's
business. ``oauth_authorization`` is not scanned: it holds the PKCE challenge by
design (the server must compare against it), and FR 17 names audit, operation and
runtime records, not that table.
"""

import hashlib
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx2
import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.evidence import EvidenceWorkspace, enable_recording, probe_registry
from harness.identity import FIXED_PROVIDER_ID, FixedIdentityProvider
from harness.oauth_client import (
    CLAUDE_CALLBACK,
    FORM_CONTENT_TYPE,
    MCP_HEADERS,
    OAuthTestClient,
    oauth_client,
)
from harness.registry import register_harness
from rheo_app_cli.main import main
from rheo_app_core import api_routes, auth_routes, serve
from rheo_app_core.main import app
from rheo_bridge.client import INGEST_PATH
from rheo_contracts import Role, WorkspaceContext
from rheo_core.audit.telemetry_tables import tool_telemetry
from rheo_core.boundary import context_for_operator
from rheo_core.boundary.context import TOKEN_MALFORMED, TOKEN_WRONG_KIND
from rheo_core.boundary.factories import context_from_token
from rheo_core.evidence.enrollment import ENROLLMENT_CREATE
from rheo_core.identity.boundary import ProviderIdentity
from rheo_core.log_config import ACCESS_LOGGER, JsonLogFormatter
from rheo_core.oauth import OAuthSurface, OAuthUnconfigured, oauth_surface
from rheo_core.operations import (
    OPERATION_NOT_PERMITTED,
    SETTINGS_SET,
    dispatch,
    register_core_operations,
    tool_facade,
)
from rheo_core.operations.core_ops import TOKEN_ISSUE, TOOL_CALL, WORKSPACE_STATUS
from rheo_core.operations.tool_facade import TOOL_NOT_FOUND
from rheo_core.refs import uuid7
from rheo_core.routing import API, IDENTITY, MCP, RoutingConfig, url_for
from rheo_core.settings import resolve
from rheo_core.storage import control_tables, work_tables
from rheo_core.storage import oauth_tables as o
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import (
    AccessTokenRow,
    get_access_token_by_hash,
    insert_account,
    insert_identity,
)
from rheo_core.storage.evidence_enrollment_tables import evidence_enrollment
from rheo_core.tokens.format import parse
from rheo_core.tokens.issue import connector_operations
from rheo_core.tokens.sets import TOOL_REGISTRY, register_core_tools
from sqlalchemy import Table, delete, inspect, select

pytestmark = pytest.mark.postgres

BASE_HOST: Final = "example.test"
MODES: Final = ("subdomain", "path")
CLIENT_LIBRARY_LOGGERS: Final = frozenset({"httpx2", "httpx", "httpcore"})
UVICORN_ACCESS_FORMAT: Final = '%s - "%s %s HTTP/%s" %d'
"""The format string uvicorn's HTTP protocols pass to ``access_logger.info``."""
_UVICORN_LOGGERS: Final = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
_POINTER: Final = re.compile(r'^Bearer resource_metadata="([^"]+)"$')


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
    """The feature on, as a deployment turns it on; the counted-client cap raised
    because the control plane is shared with every other test's registrations."""
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


@pytest.fixture(autouse=True)
def _no_provider_override() -> Iterator[None]:
    yield
    app.dependency_overrides.pop(auth_routes.resolve_identity_provider, None)


@dataclass(frozen=True)
class Member:
    account_id: UUID
    identity: ProviderIdentity
    workspace_id: UUID


@pytest.fixture
def member(cluster: ClusterSession, make_workspace: MakeWorkspace) -> Member:
    """An account with a GitHub-shaped identity, owning one workspace."""
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
    return Member(account_id, identity, make_workspace(owner=account_id))


def _sign_in_as(member: Member) -> None:
    app.dependency_overrides[auth_routes.resolve_identity_provider] = lambda: (
        FixedIdentityProvider(member.identity)
    )


# --- the three existing callers' tokens, minted as they are minted --------------------


def _cli_issue(capsys: pytest.CaptureFixture[str], member: Member) -> str:
    """Claude Code's token: ``rheo token issue --set agent_default --kind mcp``."""
    capsys.readouterr()
    code = main(
        [
            "token",
            "issue",
            "--account",
            str(member.account_id),
            "--workspace",
            str(member.workspace_id),
            "--set",
            "agent_default",
            "--kind",
            "mcp",
        ]
    )
    out, err = capsys.readouterr()
    assert code == 0, err
    return out.strip()


def _operator_ctx(member: Member) -> WorkspaceContext:
    ctx = context_for_operator(member.workspace_id)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _operator_issue(member: Member) -> str:
    """The box bot's token: ``core.token.issue`` of ``agent_default`` under an
    operator context."""
    outcome = dispatch(
        _operator_ctx(member),
        TOKEN_ISSUE,
        {
            "kind": "mcp",
            "set_name": "agent_default",
            "account_id": str(member.account_id),
        },
    )
    assert outcome.ok and outcome.result is not None, outcome
    return str(outcome.result.value)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Bridge:
    value: str
    payload: dict[str, object]


def _enrolled_bridge(member: Member) -> Bridge:
    """The bridge's ``cli`` token, as an enrolled bridge gets it, and an ingest body
    for its fingerprints."""
    machine, project = "a1" * 32, "b2" * 32
    outcome = dispatch(
        _operator_ctx(member),
        ENROLLMENT_CREATE,
        {
            "machine_fingerprint": machine,
            "project_fingerprint": project,
            "account_id": str(member.account_id),
        },
    )
    assert outcome.ok and outcome.result is not None, outcome
    return Bridge(
        str(outcome.result.value),  # type: ignore[attr-defined]
        {
            "machine_fingerprint": machine,
            "project_fingerprint": project,
            "records": [],
            "gaps": [],
        },
    )


@pytest.fixture
def bridge_recording(
    monkeypatch: pytest.MonkeyPatch, cluster: ClusterSession, member: Member
) -> Iterator[None]:
    """Recording on with the probe subscriber, as the HTTP ingest tests run it; the
    enrollment rows removed afterwards."""
    evidence = EvidenceWorkspace(cluster, member.workspace_id, member.account_id)
    enable_recording(monkeypatch, evidence)
    monkeypatch.setattr(api_routes, "CONSUMERS", probe_registry())
    yield
    engine = cluster.backend.pools.engine_for(evidence.database_name)
    if inspect(engine).has_table("evidence_enrollment", schema="core"):
        with engine.begin() as connection:
            connection.execute(delete(evidence_enrollment))


def _token_row(cluster: ClusterSession, value: str) -> AccessTokenRow:
    parsed = parse(value)
    assert parsed is not None
    with cluster.backend.control_engine.connect() as connection:
        row = get_access_token_by_hash(connection, hashlib.sha256(parsed.raw).digest())
    assert row is not None
    return row


def _api_url(operation: str) -> str:
    parts = urlsplit(url_for(_routing(), API, "/"))
    return f"{parts.scheme}://{parts.netloc}/api/v1/operations/{operation}"


def _ingest_url() -> str:
    parts = urlsplit(url_for(_routing(), API, "/"))
    return f"{parts.scheme}://{parts.netloc}{INGEST_PATH}"


def _bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def _tool_call(name: str) -> dict[str, object]:
    return {"name": name, "arguments": {}}


# --- AC-15: a connector token is an ordinary mcp token, at the same boundary ---------


@dataclass
class DispatchSpy:
    calls: list[tuple[str, UUID]]


@pytest.fixture
def dispatch_spy(monkeypatch: pytest.MonkeyPatch) -> DispatchSpy:
    """Wrap (never replace) the façade's one ``dispatch``; record the operation and
    the token id of every context that reaches it."""
    spy = DispatchSpy([])
    real = tool_facade.dispatch

    def counting(
        ctx: WorkspaceContext, operation: str, *args: Any, **kwargs: Any
    ) -> Any:
        spy.calls.append((operation, ctx.actor.id))
        return real(ctx, operation, *args, **kwargs)

    monkeypatch.setattr(tool_facade, "dispatch", counting)
    return spy


async def test_a_connector_token_is_mcp_only_and_dispatches_like_an_issued_one(
    surface: OAuthSurface,
    address: str,
    member: Member,
    cluster: ClusterSession,
    capsys: pytest.CaptureFixture[str],
    dispatch_spy: DispatchSpy,
) -> None:
    issued = _cli_issue(capsys, member)
    _sign_in_as(member)
    async with oauth_client(surface, client_address=address) as client:
        client_id, token = await client.connect()
        assert token.status_code == 200, token.text
        connector = token.json()["access_token"]

        on_api = await client.http.post(
            _api_url(WORKSPACE_STATUS), headers=_bearer(connector), json={}
        )
        issued_on_api = await client.http.post(
            _api_url(WORKSPACE_STATUS), headers=_bearer(issued), json={}
        )
        listed = await client.mcp_call(connector)
        issued_listed = await client.mcp_call(issued)
        outside = await client.mcp_call(
            connector, "tools/call", _tool_call("operations_call")
        )
        assert dispatch_spy.calls == []
        connector_call = await client.mcp_call(
            connector, "tools/call", _tool_call("workspace_status")
        )
        issued_call = await client.mcp_call(
            issued, "tools/call", _tool_call("workspace_status")
        )

    with cluster.backend.control_engine.connect() as connection:
        connector_token_id = connection.execute(
            select(o.oauth_grant.c.token_id).where(
                o.oauth_grant.c.client_id == client_id
            )
        ).scalar_one()
    issued_token_id = _token_row(cluster, issued).id

    # The api surface refuses both mcp tokens alike.
    for response in (on_api, issued_on_api):
        assert response.status_code == 401, response.text
        assert response.json()["state"] == TOKEN_WRONG_KIND

    # The listing is what the same account's `rheo token issue` agent_default token
    # lists, less any discover-grant tool (the connector set never holds one).
    granted = connector_operations(Role.OWNER)
    operation_of = {tool.name: tool.operation for tool in TOOL_REGISTRY.declarations()}
    for response in (listed, issued_listed):
        assert response.status_code == 200, response.text
    connector_tools = sorted(t["name"] for t in listed.json()["result"]["tools"])
    issued_tools = sorted(t["name"] for t in issued_listed.json()["result"]["tools"])
    assert connector_tools == [
        name for name in issued_tools if operation_of[name] != TOOL_CALL
    ]
    assert connector_tools and all(operation_of[n] in granted for n in connector_tools)

    # A tool outside the snapshot: the façade's not_found, and nothing dispatched.
    assert outside.status_code == 200, outside.text
    assert outside.json()["result"]["isError"] is True
    assert outside.json()["result"]["structuredContent"]["state"] == TOOL_NOT_FOUND

    # One dispatch per call, through the same function, for both tokens.
    assert dispatch_spy.calls == [
        (WORKSPACE_STATUS, connector_token_id),
        (WORKSPACE_STATUS, issued_token_id),
    ]
    for response in (connector_call, issued_call):
        assert response.status_code == 200, response.text
        assert response.json()["result"]["isError"] is False
    assert (
        connector_call.json()["result"]["structuredContent"]
        == issued_call.json()["result"]["structuredContent"]
    )

    # An operation outside the snapshot, dispatched with the token's own context.
    ctx = context_from_token(connector, "mcp")
    assert isinstance(ctx, WorkspaceContext), ctx
    assert ctx.actor.id == connector_token_id
    refused = dispatch(
        ctx, SETTINGS_SET, {"key": "identity.token_max_days.cli", "value": 5}
    )
    assert refused.state == OPERATION_NOT_PERMITTED


# --- AC-17: nothing secret in logs, rows or error bodies ----------------------------


class _Capture(logging.Handler):
    """Every record, rendered by the production formatter (``extra`` included)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(JsonLogFormatter())
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.split(".", 1)[0] in CLIENT_LIBRARY_LOGGERS:
            return
        self.lines.append(self.format(record))


@pytest.fixture
def uvicorn_loggers() -> Iterator[None]:
    """``serve.server_configs`` reconfigures the uvicorn loggers; put them back."""
    saved = {
        name: (
            list(logger.handlers),
            list(logger.filters),
            logger.propagate,
            logger.level,
            logger.disabled,
        )
        for name in _UVICORN_LOGGERS
        for logger in [logging.getLogger(name)]
    }
    yield
    for name, (handlers, filters, propagate, level, disabled) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.filters[:] = filters
        logger.propagate = propagate
        logger.setLevel(level)
        logger.disabled = disabled


@pytest.fixture
def server_log(uvicorn_loggers: None) -> Iterator[_Capture]:
    """A capturing handler on the root logger at production's INFO, and on
    ``uvicorn.access`` as ``serve.server_configs`` leaves it."""
    serve.server_configs()
    capture = _Capture()
    root = logging.getLogger()
    access = logging.getLogger(ACCESS_LOGGER)
    level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(capture)
    access.addHandler(capture)
    yield capture
    root.removeHandler(capture)
    access.removeHandler(capture)
    root.setLevel(level)


def _log_request_line(response: httpx2.Response) -> None:
    """Log ``response``'s request exactly as uvicorn's protocol logs it: the
    percent-quoted path, then ``?`` and the raw query string when there is one."""
    request = response.request
    target = quote(request.url.path)
    if request.url.query:
        target = f"{target}?{request.url.query.decode('ascii')}"
    logging.getLogger(ACCESS_LOGGER).info(
        UVICORN_ACCESS_FORMAT,
        "192.0.2.1:40000",
        request.method,
        target,
        "1.1",
        response.status_code,
    )


def _rows(connection: Any, table: Table, *where: Any) -> list[str]:
    return [
        repr(dict(row))
        for row in connection.execute(select(table).where(*where)).mappings()
    ]


def _assert_none_in(texts: list[str], secrets: list[str], where: str) -> None:
    for text in texts:
        for secret in secrets:
            assert secret not in text, f"a secret appears in {where}: {text[:200]}"


async def test_a_full_flow_leaves_no_secret_in_logs_rows_or_error_bodies(
    surface: OAuthSurface,
    address: str,
    member: Member,
    cluster: ClusterSession,
    server_log: _Capture,
) -> None:
    responses: list[httpx2.Response] = []

    async def keep(response: httpx2.Response) -> None:
        await response.aread()
        responses.append(response)

    _sign_in_as(member)
    async with oauth_client(surface, client_address=address) as client:
        client.http.event_hooks["response"].append(keep)
        client_id, token = await client.connect()
        assert token.status_code == 200, token.text
        first = token.json()
        code, verifier = client.issued.code[-1], client.issued.verifier[-1]
        assert (await client.mcp_call(first["access_token"])).status_code == 200
        called = await client.mcp_call(
            first["access_token"], "tools/call", _tool_call("workspace_status")
        )
        assert called.status_code == 200, called.text
        refreshed = await client.refresh(first["refresh_token"], client_id)
        assert refreshed.status_code == 200, refreshed.text
        second = refreshed.json()["access_token"]

        # The refusals a connector can provoke, each carrying a secret in its request.
        superseded = await client.mcp_call(first["access_token"])
        reused = await client.refresh(first["refresh_token"], client_id)
        in_query = [
            await client.http.post(
                f"{surface.resource}?{name}={second}",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers=MCP_HEADERS,
            )
            for name in ("access_token", "token")
        ] + [
            await client.http.post(
                f"{_api_url(WORKSPACE_STATUS)}?{name}={second}", json={}
            )
            for name in ("access_token", "token")
        ]
        still_valid = await client.mcp_call(second)
        # Last, because any second redemption of the code revokes the grant.
        wrong_verifier = await client.redeem(code, client_id, CLAUDE_CALLBACK, "x" * 43)
        replayed = await client.redeem(code, client_id, CLAUDE_CALLBACK, verifier)

    assert superseded.status_code == 401
    for refusal in (reused, wrong_verifier, replayed):
        assert refusal.status_code == 400, refusal.text
        assert refusal.json()["error"] == "invalid_grant"
    # A valid bearer in a query string is not read on either surface: the request
    # is unauthenticated, while the same value in the header is served.
    for response in in_query:
        assert response.status_code == 401, response.text
        assert response.json()["state"] == TOKEN_MALFORMED
    assert still_valid.status_code == 200, still_valid.text

    secrets = client.issued.all()
    assert len(client.issued.access) == 2 and len(client.issued.refresh) == 2
    assert client.issued.code and client.issued.verifier and client.issued.challenge

    # The access log: every request of the flow, as uvicorn records it.
    for response in responses:
        _log_request_line(response)
    access_lines = [
        line for line in server_log.lines if '"logger": "uvicorn.access"' in line
    ]
    assert len(access_lines) == len(responses)
    authorize_path = urlsplit(surface.authorization_endpoint).path
    callback_path = urlsplit(url_for(_routing(), IDENTITY, "/callback")).path
    for path in (authorize_path, callback_path):
        lines = [line for line in access_lines if f"GET {path} HTTP" in line]
        assert lines, path
        for line in lines:
            assert f"{path}?" not in line
    _assert_none_in(server_log.lines, secrets, "a log line")

    # Error bodies (and their headers).
    errors = [response for response in responses if response.status_code >= 400]
    assert len(errors) >= 7
    _assert_none_in(
        [f"{response.headers!r} {response.text}" for response in errors],
        secrets,
        "an error response",
    )

    # Rows.
    with cluster.backend.control_engine.connect() as connection:
        grant = (
            connection.execute(
                select(o.oauth_grant).where(o.oauth_grant.c.client_id == client_id)
            )
            .mappings()
            .one()
        )
        rows = (
            _rows(connection, o.oauth_event, o.oauth_event.c.client_id == client_id)
            + _rows(connection, o.oauth_grant, o.oauth_grant.c.client_id == client_id)
            + _rows(
                connection,
                o.oauth_refresh_token,
                o.oauth_refresh_token.c.token_id == grant["token_id"],
            )
            + _rows(
                connection,
                control_tables.access_token,
                control_tables.access_token.c.id == grant["token_id"],
            )
        )
    assert any("code_redeemed" in row for row in rows)
    database = cluster.registry_row(member.workspace_id).database_name
    engine = cluster.backend.pools.engine_for(database)
    with UnitOfWork(engine, database) as uow:
        workspace_rows = (
            _rows(uow.connection, work_tables.operation)
            + _rows(uow.connection, work_tables.audit_record)
            + _rows(uow.connection, tool_telemetry)
        )
    # The call left its tool-telemetry row, so the scan read a populated table.
    assert any("'workspace_status'" in row for row in workspace_rows), workspace_rows
    _assert_none_in(rows + workspace_rows, secrets, "a stored row")


# --- AC-21: nothing advertised unless it works -------------------------------------


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("incomplete", [False, True], ids=["off", "github-disabled"])
async def test_an_unconfigured_deployment_advertises_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    address: str,
    mode: str,
    incomplete: bool,
) -> None:
    """The probes are the URLs the configured surface would serve, resolved before
    the feature is switched back off (or left on with GitHub disabled)."""
    _set_mode(monkeypatch, mode)
    with monkeypatch.context() as on:
        configured = _configure(on)
    if incomplete:
        monkeypatch.setenv("RHEO__identity__oauth__enabled", "true")
        monkeypatch.setenv("RHEO__identity__providers__github__enabled", "false")
    settings = resolve()
    assert isinstance(
        oauth_surface(settings, RoutingConfig.from_settings(settings)),
        OAuthUnconfigured,
    )
    async with oauth_client(configured, client_address=address) as client:
        unauthenticated = await client.mcp_call(None)
        dead = [
            await client.http.get(configured.protected_resource_metadata_url),
            await client.http.get(f"{configured.protected_resource_metadata_url}/"),
            await client.http.get(configured.authorization_server_metadata_url),
            await client.http.get(configured.authorization_endpoint),
            await client.http.get(client.consent_endpoint),
            await client.register({"redirect_uris": [CLAUDE_CALLBACK]}),
            await client.token({"grant_type": "refresh_token"}),
        ]
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers.get_list("www-authenticate") == ["Bearer"]
    for response in dead:
        assert response.status_code == 404, (response.request.url, response.text)

    capsys.readouterr()
    main(["doctor"])
    (line,) = [
        line
        for line in capsys.readouterr().out.splitlines()
        if " oauth surface: " in line
    ]
    if incomplete:
        assert line.startswith("FAIL oauth surface: enabled but unconfigured: "), line
        assert "identity_provider_disabled" in line
    else:
        assert line == "ok   oauth surface: disabled"


def _rfc8414_url(identifier: str, name: str) -> str:
    """``/.well-known/<name>`` inserted between origin and path (RFC 8414 s3.1,
    RFC 9728 s3.1), computed here from the identifier, not by the server."""
    parts = urlsplit(identifier)
    return f"{parts.scheme}://{parts.netloc}/.well-known/{name}{parts.path.rstrip('/')}"


@pytest.mark.parametrize("mode", MODES)
async def test_every_advertised_url_answers_and_follows_url_for(
    monkeypatch: pytest.MonkeyPatch, address: str, mode: str
) -> None:
    """Discovery walked as a connector walks it, from the 401's pointer: every URL
    in the protected-resource and authorization-server documents equals its
    ``url_for``-derived value and answers neither 404 nor 500."""
    _set_mode(monkeypatch, mode)
    surface = _configure(monkeypatch)
    config = _routing()
    resource = url_for(config, MCP, "/")
    expected = {
        "resource": resource,
        "issuer": resource.rstrip("/"),
        "authorization_endpoint": url_for(config, IDENTITY, "/oauth/authorize"),
        "token_endpoint": url_for(config, IDENTITY, "/oauth/token"),
        "registration_endpoint": url_for(config, IDENTITY, "/oauth/register"),
    }
    async with oauth_client(surface, client_address=address) as client:
        challenge = await client.mcp_call(None)
        match = _POINTER.match(challenge.headers["www-authenticate"])
        assert match is not None, challenge.headers
        pointer = match.group(1)
        assert pointer == _rfc8414_url(resource, "oauth-protected-resource")
        resource_doc = await client.http.get(pointer)
        assert resource_doc.status_code == 200, resource_doc.text
        resource_document: Mapping[str, Any] = resource_doc.json()
        (issuer,) = resource_document["authorization_servers"]
        as_url = _rfc8414_url(issuer, "oauth-authorization-server")
        as_doc = await client.http.get(as_url)
        assert as_doc.status_code == 200, as_doc.text
        as_document: Mapping[str, Any] = as_doc.json()

        advertised = {
            "resource": resource_document["resource"],
            "issuer": as_document["issuer"],
            "authorization_endpoint": as_document["authorization_endpoint"],
            "token_endpoint": as_document["token_endpoint"],
            "registration_endpoint": as_document["registration_endpoint"],
        }
        urls_in_documents = {
            value
            for document in (resource_document, as_document)
            for value in [
                *document.values(),
                *resource_document["authorization_servers"],
            ]
            if isinstance(value, str) and value.startswith("https://")
        }
        assert urls_in_documents == set(advertised.values())
        answers = {
            "resource": await client.mcp_call(None),
            "issuer": as_doc,
            "authorization_endpoint": await client.http.get(
                advertised["authorization_endpoint"]
            ),
            "token_endpoint": await client.http.post(
                advertised["token_endpoint"],
                content=b"",
                headers={"Content-Type": FORM_CONTENT_TYPE},
            ),
            "registration_endpoint": await client.register({}),
        }
    assert advertised == expected
    assert issuer == expected["issuer"]
    for name, response in answers.items():
        assert response.status_code not in (404, 500), (name, response.text)
        assert response.status_code < 500, (name, response.text)


# --- FR 16: last_used_at, for the three existing callers and a connector -------------


@dataclass(frozen=True)
class Caller:
    name: str
    value: str
    use: str
    """``mcp`` (a ``tools/list``) or ``ingest`` (the bridge's own post)."""


async def _present(
    client: OAuthTestClient, caller: Caller, bridge: Bridge, *, surface: str
) -> httpx2.Response:
    if surface == "mcp":
        return await client.mcp_call(caller.value)
    if caller.use == "ingest":
        return await client.http.post(
            _ingest_url(), headers=_bearer(caller.value), json=bridge.payload
        )
    return await client.http.post(
        _api_url(WORKSPACE_STATUS), headers=_bearer(caller.value), json={}
    )


async def test_last_used_at_moves_on_use_only_for_every_caller(
    surface: OAuthSurface,
    address: str,
    member: Member,
    cluster: ClusterSession,
    capsys: pytest.CaptureFixture[str],
    bridge_recording: None,
) -> None:
    """Each token: ``null`` until first presented; set within the first request's
    window; unchanged by a presentation its surface refuses (the wrong surface);
    later on the next accepted request. The same ``resolve_token`` serves all four,
    so a connector token behaves exactly as the three existing ones."""
    bridge = _enrolled_bridge(member)
    _sign_in_as(member)
    async with oauth_client(surface, client_address=address) as client:
        _, token = await client.connect()
        assert token.status_code == 200, token.text
        callers = [
            Caller("claude_code", _cli_issue(capsys, member), "mcp"),
            Caller("box_bot", _operator_issue(member), "mcp"),
            Caller("bridge", bridge.value, "ingest"),
            Caller("connector", token.json()["access_token"], "mcp"),
        ]
        for caller in callers:
            accepted = "api" if caller.use == "ingest" else "mcp"
            refused = "mcp" if accepted == "api" else "api"
            assert _token_row(cluster, caller.value).last_used_at is None, caller.name

            started = datetime.now(UTC)
            first_use = await _present(client, caller, bridge, surface=accepted)
            assert first_use.status_code == 200, (caller.name, first_use.text)
            first = _token_row(cluster, caller.value).last_used_at
            assert first is not None and started <= first <= datetime.now(UTC)

            wrong = await _present(client, caller, bridge, surface=refused)
            assert wrong.status_code == 401, (caller.name, wrong.text)
            assert wrong.json()["state"] == TOKEN_WRONG_KIND
            assert _token_row(cluster, caller.value).last_used_at == first

            again = await _present(client, caller, bridge, surface=accepted)
            assert again.status_code == 200, (caller.name, again.text)
            later = _token_row(cluster, caller.value).last_used_at
            assert later is not None and later > first, caller.name
