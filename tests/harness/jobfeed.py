"""Shared job feed collector scaffolding: fakes for the ``dependencies()`` seam and an
installed collector over the real intake workspace.

The fakes reach handlers only through ``rheo_leads.jobfeed.use_dependencies``; no
module global is patched. The installed workspace reuses the Leads intake fixture
(Relationships and Leads enabled) and its real dispatcher and worker.
"""

import base64
import hashlib
import json
import threading
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from postgres.test_leads_intake import Intake
from rheo_contracts import Entry, RecordRef, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.boundary.factories import context_for_connection
from rheo_core.connectors import oauth
from rheo_core.connectors.oauth import ClientAuth, Discovered, RegisteredClient
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.operations import dispatch
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.refs import uuid7
from rheo_core.settings import deployment as settings_deployment
from rheo_core.settings import resolver as settings_resolver
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_tables import job
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import VisitResult, visit_workspace
from rheo_core.work.schedules import ensure_module_schedule, run_due_schedules
from rheo_leads.jobfeed import Dependencies, delivery, use_dependencies
from rheo_leads.jobfeed.collect import Sighting
from rheo_leads.jobfeed.delivery import DeliveryResult
from rheo_leads.jobfeed.install import install_connection
from rheo_leads.jobfeed.normalize import NormalizedPosting
from rheo_leads.jobfeed.presets import Purpose
from rheo_leads.jobfeed.run import (
    COLLECT_JOB_KIND,
    SCHEDULE_NAME,
    CollectPayload,
    collect_job,
)
from rheo_leads.manifest import MANIFEST
from rheo_leads.pipeline_common import lock
from rheo_leads.references import ref
from rheo_leads.storage import jobfeed as j
from sqlalchemy import Engine, insert, select, update
from sqlalchemy import event as sa_event
from sqlalchemy.pool import Pool

# A global IPv6 literal, as the egress tests use: the provenance gate flags any
# public IPv4 literal, and the outbound check refuses a non-public address.
PUBLIC_ADDRESS = "2600::1"
PRIVATE_ADDRESS = "10.1.2.3"
BASE = "https://www.example.com"
ENDPOINTS = Discovered(
    resource_url=f"{BASE}/mcp",
    issuer=BASE,
    authorization_endpoint=f"{BASE}/oauth/authorize",
    token_endpoint=f"{BASE}/oauth/token",
    registration_endpoint=f"{BASE}/oauth/register",
    revocation_endpoint=f"{BASE}/oauth/revoke",
)


def registered_client(**changes: Any) -> RegisteredClient:
    values: dict[str, Any] = {
        "client_id": "fixture-client",
        "auth_method": "none",
        "redirect_uri": "https://auth.example.com/connectors/leads/callback",
        "secret_expires_at": None,
        "client_secret_ref": None,
        "registration_token_ref": None,
    }
    values.update(changes)
    return RegisteredClient(**values)


@dataclass
class FakeClock:
    """A monotonic clock, a wall clock and a sleep that only advances both."""

    monotonic_at: float = 1000.0
    wall_at: datetime = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    slept: list[float] = field(default_factory=list)

    def advance(self, seconds: float) -> None:
        self.monotonic_at += seconds
        self.wall_at += timedelta(seconds=seconds)

    def monotonic(self) -> float:
        return self.monotonic_at

    def now(self) -> datetime:
        return self.wall_at

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)


@dataclass
class FakeResolver:
    """Resolves every host to fixed addresses and records what it was asked."""

    addresses: dict[str, Sequence[str]] = field(default_factory=dict)
    default: Sequence[str] = (PUBLIC_ADDRESS,)
    asked: list[str] = field(default_factory=list)

    def __call__(self, host: str) -> Sequence[str]:
        self.asked.append(host)
        return self.addresses.get(host, self.default)

    def make_private(self, host: str) -> None:
        """From now on ``host`` resolves to an RFC 1918 address only."""
        self.addresses[host] = (PRIVATE_ADDRESS,)


@contextmanager
def fakes(
    *,
    clock: FakeClock | None = None,
    resolver: FakeResolver | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Iterator[Dependencies]:
    clock = clock or FakeClock()
    with use_dependencies(
        Dependencies(
            transport=transport,
            resolver=resolver or FakeResolver(),
            clock=clock.monotonic,
            sleep=clock.sleep,
            now=clock.now,
        )
    ) as deps:
        yield deps


@dataclass
class Collector:
    intake: Intake
    pipeline_id: UUID
    connection_id: UUID
    owner: WorkspaceContext
    poll: WorkspaceContext


def owner_web(i: Intake) -> WorkspaceContext:
    ctx = context_for_harness(i.workspace, i.ctx.actor.id, Role.OWNER, Entry.WEB)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def call(i: Intake, ctx: WorkspaceContext, name: str, /, **payload: Any) -> Any:
    outcome = dispatch(
        ctx, name, payload, registry=i.surfaces.operations, consumers=i.consumers
    )
    assert outcome.ok, outcome
    return outcome.result


def jobsearch_pipeline(i: Intake, owner: WorkspaceContext) -> UUID:
    """Turn the capability on and create a Job search pipeline."""
    call(i, owner, "core.settings.set", key="leads.capabilities.jobsearch", value=True)
    created = call(
        i, owner, "leads.pipeline.create", name="Job leads", preset="job_search"
    )
    return RecordRef.parse(created.ref).id


def poll_context(i: Intake, connection_id: UUID) -> WorkspaceContext:
    ctx = context_for_connection(i.workspace, connection_id, "leads", "poll")
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def install_collector(
    i: Intake,
    *,
    client: RegisteredClient | None = None,
    endpoints: Discovered = ENDPOINTS,
    connection_id: UUID | None = None,
) -> Collector:
    owner = owner_web(i)
    pipeline_id = jobsearch_pipeline(i, owner)
    connection_id = connection_id or uuid7()
    with open_unit_of_work(owner) as uow:
        install_connection(
            uow,
            owner,
            connection_id=connection_id,
            pipeline_id=pipeline_id,
            endpoints=endpoints,
            client=client or registered_client(),
        )
        uow.commit()
    return Collector(
        i, pipeline_id, connection_id, owner, poll_context(i, connection_id)
    )


@pytest.fixture
def collector(intake: Intake) -> Iterator[Collector]:
    yield install_collector(intake)


def deliver(
    poll_ctx: WorkspaceContext,
    i: Intake,
    body: dict[str, Any] | str,
    event_id: str,
    occurred_at: datetime | None = None,
) -> OperationOutcome:
    """Dispatch acceptance for the poll context's own connection."""
    payload: dict[str, Any] = {
        "connection_ref": ref("intake_connection", poll_ctx.actor.id),
        "source_event_id": event_id,
        "body": body if isinstance(body, str) else json.dumps(body),
    }
    if occurred_at is not None:
        payload["source_occurred_at"] = occurred_at.isoformat()
    return dispatch(
        poll_ctx,
        "leads.intake.accept_delivery",
        payload,
        registry=i.surfaces.operations,
        consumers=i.consumers,
    )


def drain(i: Intake) -> None:
    """Run the intake consumer so accepted deliveries become observations."""
    i.visit()


# --- a fake authorization server -----------------------------------------------------

RESOURCE_URL = "https://mcp.example.com/mcp"
ISSUER = "https://auth.example.com"
CLIENT_NAME = "client-name-marker-3c9e7a51"
ROUTING_HOST = "example.test"

Responder = Callable[[httpx.Request], httpx.Response]


@dataclass
class Hold:
    """Blocks one kind of request until released, telling the test it arrived."""

    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)


def answer(
    status: int,
    document: object = None,
    *,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Responder:
    """A fixed response: a JSON document, raw bytes, or an empty body."""

    def respond(request: httpx.Request) -> httpx.Response:
        if content is not None:
            return httpx.Response(status, content=content, headers=headers)
        if document is None:
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, json=document, headers=headers)

    return respond


def issued(
    client_id: str | None = None, *, status: int = 201, **extra: Any
) -> Responder:
    """A valid registration answer echoing the request's redirect URIs; ``extra``
    adds members such as ``client_secret`` or ``token_endpoint_auth_method``."""
    counter = iter(range(1, 1_000_000))

    def respond(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        document: dict[str, Any] = {
            "client_id": client_id or f"issued-client-{next(counter):04d}",
            "redirect_uris": sent["redirect_uris"],
            "grant_types": ["authorization_code", "refresh_token"],
        }
        document.update(extra)
        return httpx.Response(status, json=document)

    return respond


def redirected() -> Responder:
    return answer(302, headers={"Location": f"{ISSUER}/elsewhere"})


def oversized() -> Responder:
    return answer(201, content=b'{"client_id":"' + b"x" * (17 * 1024) + b'"}')


def malformed() -> Responder:
    return answer(201, content=b"{not json")


@dataclass
class Tokens:
    """A token endpoint that answers 200 with a bearer access token.

    The authorization-code grant carries a fresh ``refresh-marker-<uuid>`` unless
    ``refresh`` is false. A refresh grant carries a fresh one when ``rotating``, and
    none otherwise. Every value issued is kept in ``issued`` for leak checks.

    ``reject_superseded`` (by default, whether ``rotating``) models a provider that
    rotates on use: a refresh token that a rotating refresh grant was answered for
    is spent, and presenting it again is a 400 ``invalid_grant``. It is spent
    whether or not the caller stored the replacement.
    """

    refresh: bool = True
    rotating: bool = True
    reject_superseded: bool | None = None
    issued: list[str] = field(default_factory=list)
    superseded: set[str] = field(default_factory=set)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("ascii"))
        grant = form.get("grant_type", [""])[0]
        presented = form.get("refresh_token", [""])[0]
        rejects = self.reject_superseded
        if rejects is None:
            rejects = self.rotating
        if grant == "refresh_token" and rejects and presented in self.superseded:
            return httpx.Response(400, json={"error": "invalid_grant"})
        access = f"access-marker-{uuid4()}"
        document: dict[str, Any] = {"token_type": "Bearer", "access_token": access}
        self.issued.append(access)
        if (grant == "authorization_code" and self.refresh) or (
            grant == "refresh_token" and self.rotating
        ):
            document["refresh_token"] = f"refresh-marker-{uuid4()}"
            self.issued.append(document["refresh_token"])
            if grant == "refresh_token":
                self.superseded.add(presented)
        return httpx.Response(200, json=document)


def token_error(error: str = "invalid_client", *, status: int = 400) -> Responder:
    """An RFC 6749 s5.2 error answer from the token endpoint."""
    return answer(status, {"error": error})


@dataclass
class FakeAuthorizationServer:
    """The probe, both metadata documents, a counting registration endpoint with
    scriptable answers, the authorization endpoint, and counting, body-capturing
    token and revocation endpoints, all behind one ``httpx.MockTransport``.

    Every request is kept in ``requests``. ``holds`` blocks the ``"discovery"``
    (protected-resource metadata), ``"registration"``, ``"token"`` or
    ``"revocation"`` request until released. :meth:`authorize` stands in for the
    owner's consent and mints a code.
    """

    resource_url: str = RESOURCE_URL
    issuer: str = ISSUER
    registration_path: str | None = "/register"
    revocation: bool = True
    scope: str | None = "jobs:read"
    registration: Responder = field(default_factory=issued)
    token: Responder = field(
        default_factory=lambda: answer(
            200,
            {
                "token_type": "Bearer",
                "access_token": "access-marker",
                "refresh_token": "refresh-marker",
            },
        )
    )
    revoke: Responder = field(default_factory=lambda: answer(200))
    holds: dict[str, Hold] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    registrations: list[httpx.Request] = field(default_factory=list)
    token_requests: list[httpx.Request] = field(default_factory=list)
    revocation_requests: list[httpx.Request] = field(default_factory=list)
    codes: dict[str, str] = field(default_factory=dict)

    def authorize(self, authorization_url: str) -> tuple[str, str]:
        """The owner's consent at the authorization endpoint: mint a code bound to
        the URL's PKCE challenge; return the URL's state and the code."""
        query = parse_qs(urlsplit(authorization_url).query)
        code = f"code-marker-{uuid4()}"
        self.codes[code] = query["code_challenge"][0]
        return query["state"][0], code

    def verifier_matches(self, request: httpx.Request) -> bool:
        """Whether a code grant's verifier answers the challenge its code was
        minted for (RFC 7636 S256)."""
        form = parse_qs(request.content.decode("ascii"))
        challenge = self.codes.get(form.get("code", [""])[0])
        verifier = form.get("code_verifier", [""])[0].encode("ascii")
        digest = hashlib.sha256(verifier).digest()
        return challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    @property
    def registration_endpoint(self) -> str | None:
        # Independent of ``issuer``, so a test can change either one alone.
        if self.registration_path is None:
            return None
        return f"{ISSUER}{self.registration_path}"

    def discovered(self) -> Discovered:
        """What discovery against this server pins."""
        endpoint = self.registration_endpoint
        assert endpoint is not None
        return Discovered(
            resource_url=self.resource_url,
            issuer=self.issuer,
            authorization_endpoint=f"{self.issuer}/authorize",
            token_endpoint=f"{self.issuer}/token",
            registration_endpoint=endpoint,
            revocation_endpoint=f"{self.issuer}/revoke" if self.revocation else None,
        )

    def _origin(self, url: str) -> tuple[str, str]:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}", parts.path.rstrip("/")

    def server_metadata(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "code_challenge_methods_supported": ["S256"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
            "response_types_supported": ["code"],
        }
        if self.registration_endpoint is not None:
            document["registration_endpoint"] = self.registration_endpoint
        if self.revocation:
            document["revocation_endpoint"] = f"{self.issuer}/revoke"
        return document

    def _hold(self, kind: str) -> None:
        hold = self.holds.get(kind)
        if hold is not None:
            hold.entered.set()
            assert hold.release.wait(timeout=60), f"{kind} was never released"

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url.copy_with(query=None))
        resource_origin, resource_path = self._origin(self.resource_url)
        issuer_origin, issuer_path = self._origin(self.issuer)
        if request.method == "POST" and url == self.resource_url:
            challenge = "Bearer" + (f' scope="{self.scope}"' if self.scope else "")
            return httpx.Response(401, headers={"WWW-Authenticate": challenge})
        if (
            url
            == f"{resource_origin}/.well-known/oauth-protected-resource{resource_path}"
        ):
            self._hold("discovery")
            return httpx.Response(
                200,
                json={
                    "resource": self.resource_url,
                    "authorization_servers": [self.issuer],
                },
            )
        if (
            url
            == f"{issuer_origin}/.well-known/oauth-authorization-server{issuer_path}"
        ):
            return httpx.Response(200, json=self.server_metadata())
        if request.method == "POST" and url == self.registration_endpoint:
            self.registrations.append(request)
            self._hold("registration")
            return self.registration(request)
        if url == f"{self.issuer}/authorize":
            return httpx.Response(200)
        if request.method == "POST" and url == f"{self.issuer}/token":
            self.token_requests.append(request)
            self._hold("token")
            return self.token(request)
        if request.method == "POST" and url == f"{self.issuer}/revoke":
            self.revocation_requests.append(request)
            self._hold("revocation")
            return self.revoke(request)
        return httpx.Response(404)


def configure_connector(
    monkeypatch: pytest.MonkeyPatch,
    data_root: Path,
    *,
    enabled: bool = True,
    identity_path: str | None = None,
    resource_url: str = RESOURCE_URL,
    client_name: str = CLIENT_NAME,
    routing_host: str = ROUTING_HOST,
    job_hosts: str | None = None,
    redirect: str = "identity",
    loopback_port: int | None = None,
) -> None:
    """Deployment settings for the connector client and a fresh secret data root.
    ``redirect`` is the redirect mode; ``loopback_port`` left ``None`` keeps the
    package default port.
    ``job_hosts`` (comma-separated) configures the collector's job hosts; left
    ``None`` they stay unset, so a run stops before its claim.

    Call it after the intake fixture has installed its isolated settings registry:
    the environment layer checks ``RHEO__`` variables against its own registry
    reference, which this points at the same isolated one so the Leads keys count
    as declared (in a deployment both are the one process-wide registry).
    """
    monkeypatch.setattr(settings_deployment, "REGISTRY", settings_resolver.REGISTRY)
    secrets_dir = data_root / "secrets"
    secrets_dir.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv("RHEO_DATA_ROOT", str(data_root))
    monkeypatch.setenv("RHEO__connectors__oauth_client__enabled", str(enabled).lower())
    monkeypatch.setenv("RHEO__connectors__oauth_client__client_name", client_name)
    monkeypatch.setenv("RHEO__connectors__oauth_client__redirect", redirect)
    if loopback_port is None:
        monkeypatch.delenv("RHEO__connectors__oauth_client__loopback_port", raising=False)
    else:
        monkeypatch.setenv(
            "RHEO__connectors__oauth_client__loopback_port", str(loopback_port)
        )
    monkeypatch.setenv("RHEO__leads__jobfeed__resource_url", resource_url)
    monkeypatch.setenv("RHEO__routing__mode", "subdomain")
    monkeypatch.setenv("RHEO__routing__scheme", "https")
    monkeypatch.setenv("RHEO__routing__base_host", routing_host)
    monkeypatch.setenv("RHEO__routing__public_host", "")
    if job_hosts is None:
        monkeypatch.delenv("RHEO__leads__jobfeed__job_hosts", raising=False)
    else:
        monkeypatch.setenv("RHEO__leads__jobfeed__job_hosts", job_hosts)
    if identity_path is None:
        monkeypatch.delenv("RHEO__routing__identity__path", raising=False)
    else:
        monkeypatch.setenv("RHEO__routing__identity__path", identity_path)


# --- a connected collector ------------------------------------------------------------

CLIENT_SECRET = "client-secret-marker-4d1f9a62-7c3e-4b85-a0d2-9e6b1c8f5a37"
REGISTRATION_TOKEN = "registration-token-marker-8e2c5b19-0f4a-4d73-b6e1-3a9d7c2f0b48"
STORED_CLIENT_ID = "stored-client"


@dataclass
class ConnectedCollector(Collector):
    """An installed collector holding a real refresh-token file. ``refresh_token``
    is the fixture's marker value, kept for leak checks and for what a fake
    revocation endpoint should receive."""

    token_ref: str = field(repr=False, default="")
    refresh_token: str = field(repr=False, default="")


def connected_collector(
    i: Intake,
    server: FakeAuthorizationServer,
    *,
    auth_method: str = "none",
    registration_token: bool = True,
    report: dict[str, int] | None = None,
    choices: Sequence[tuple[str, str | None]] = (),
    **columns: Any,
) -> ConnectedCollector:
    """A connected collector as a completed authorization leaves it, built through
    the real OAuth client against ``server`` (the requests it took are forgotten).

    The stored client is a real registration with ``auth_method``: the secret
    methods own a client-secret file, ``none`` owns none, and ``registration_token``
    adds a registration-token file. The refresh token is a real file written by a
    real code exchange, referenced at generation 1, and the schedule is enabled.
    ``report`` sets report counts, ``choices`` the stored account choices as
    ``(organization_id, label)`` in order, and ``columns`` any other connection
    column (written last).
    """
    connection_id = uuid7()
    redirect = oauth.redirect_uri("leads")
    transport = httpx.MockTransport(server.handle)
    clock = FakeClock()
    extra: dict[str, Any] = {"token_endpoint_auth_method": auth_method}
    if auth_method != "none":
        extra["client_secret"] = CLIENT_SECRET
    if registration_token:
        extra["registration_access_token"] = REGISTRATION_TOKEN
    endpoint = server.registration_endpoint
    assert endpoint is not None
    registration, server.registration = (
        server.registration,
        issued(STORED_CLIENT_ID, **extra),
    )
    try:
        client = oauth.register(
            endpoint,
            redirect,
            CLIENT_NAME,
            workspace_id=i.workspace,
            connection_id=connection_id,
            resolver=FakeResolver(),
            clock=clock.monotonic,
            transport=transport,
        )
    finally:
        server.registration = registration
    discovered = server.discovered()
    collector = install_collector(
        i, client=client, endpoints=discovered, connection_id=connection_id
    )
    refresh_token = f"refresh-marker-{uuid4()}"
    token, server.token = (
        server.token,
        answer(
            200,
            {
                "token_type": "Bearer",
                "access_token": f"access-marker-{uuid4()}",
                "refresh_token": refresh_token,
            },
        ),
    )
    try:
        reference, _access = oauth.exchange_code(
            discovered.token_endpoint,
            code=f"code-marker-{uuid4()}",
            redirect_uri=redirect,
            code_verifier="v" * 43,
            resource=discovered.resource_url,
            client=ClientAuth(
                client_id=client.client_id,
                auth_method=client.auth_method,
                client_secret_ref=client.client_secret_ref,
                secret_expires_at=client.secret_expires_at,
            ),
            workspace_id=i.workspace,
            connection_id=connection_id,
            resolver=FakeResolver(),
            clock=clock.monotonic,
            transport=transport,
            now=clock.now(),
        )
    finally:
        server.token = token
    server.requests.clear()
    server.registrations.clear()
    server.token_requests.clear()
    with open_unit_of_work(collector.owner) as uow:
        lock(uow)
        uow.connection.execute(
            update(j.jobfeed_connection).values(
                token_ref=reference,
                credential_generation=1,
                **(report or {}),
                **columns,
            )
        )
        for ordinal, (organization_id, label) in enumerate(choices):
            uow.connection.execute(
                insert(j.jobfeed_account_choice).values(
                    connection_id=connection_id,
                    organization_id=organization_id,
                    label=label,
                    ordinal=ordinal,
                    seen_at=clock.now(),
                )
            )
        ensure_module_schedule(
            uow.connection,
            module_id="leads",
            name=SCHEDULE_NAME,
            enabled=True,
            due_at=clock.now(),
        )
        uow.commit()
    return ConnectedCollector(
        intake=collector.intake,
        pipeline_id=collector.pipeline_id,
        connection_id=collector.connection_id,
        owner=collector.owner,
        poll=collector.poll,
        token_ref=reference,
        refresh_token=refresh_token,
    )


# --- a fake MCP server ----------------------------------------------------------------

McpKey = tuple[str, str | None, str | None]
"""``(method, tool, action)`` of one JSON-RPC request; tool and action are ``None``
when the request has none."""

PROTOCOL_VERSION = "2025-06-18"
SESSION_ID = "session-example-1"
ORGANIZATION = "org-example-1"
WRITE_TOOLS = ("submit_proposal", "send_message", "accept_offer")
"""Write-capable tool names the fake advertises; the collector must never call one."""


def account(
    organization_id: str = ORGANIZATION,
    label: str | None = "Example Freelancer",
    role: str = "TALENT",
) -> dict[str, object]:
    """One ``list_accounts`` entry as the server lists it."""
    entry: dict[str, object] = {"org_uid": organization_id, "role": role}
    if label is not None:
        entry["name"] = label
    return entry


def _mcp_message(request: httpx.Request) -> dict[str, Any]:
    try:
        message = json.loads(request.content)
    except ValueError:
        return {}
    return message if isinstance(message, dict) else {}


def _mcp_key(request: httpx.Request) -> McpKey:
    message = _mcp_message(request)
    if not message:
        return ("<unparsed>", None, None)
    method = str(message.get("method"))
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    tool = params.get("name")
    arguments = params.get("arguments")
    action = arguments.get("action") if isinstance(arguments, dict) else None
    return (
        method,
        tool if isinstance(tool, str) else None,
        action if isinstance(action, str) else None,
    )


def script_name(key: McpKey) -> str:
    """How :attr:`FakeMcpServer.script` and ``holds`` name a request: the method,
    else the tool for ``tools/call``, with ``:action`` when it has one."""
    method, tool, action = key
    if method != "tools/call" or tool is None:
        return method
    return tool if action is None else f"{tool}:{action}"


def rpc_result(result: object, *, headers: dict[str, str] | None = None) -> Responder:
    """A JSON-RPC response carrying ``result`` for the request's own id."""

    def respond(request: httpx.Request) -> httpx.Response:
        document = {
            "jsonrpc": "2.0",
            "id": _mcp_message(request).get("id"),
            "result": result,
        }
        return httpx.Response(200, json=document, headers=headers)

    return respond


def tool_result(document: object, *, is_error: bool = False) -> Responder:
    """A ``tools/call`` result whose single text block is ``document`` as JSON."""
    return rpc_result(
        {
            "content": [{"type": "text", "text": json.dumps(document)}],
            "isError": is_error,
        }
    )


def accounts_result(*entries: dict[str, object]) -> Responder:
    """A ``list_accounts`` answer listing ``entries`` in that order."""
    return tool_result({"accounts": list(entries)})


class Stalling(httpx.SyncByteStream):
    """An event stream that sends a comment line every ``interval`` seconds of fake
    time, ``chunks`` times, then answers with ``document`` (or, when it is
    ``None``, stalls until the read timeout fires)."""

    def __init__(
        self,
        clock: FakeClock,
        request: httpx.Request,
        *,
        interval: float,
        chunks: int,
        document: object = None,
    ) -> None:
        self.clock = clock
        self.request_id = _mcp_message(request).get("id")
        self.interval = interval
        self.chunks = chunks
        self.document = document

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(self.chunks):
            self.clock.advance(self.interval)
            yield b":\n"
        if self.document is None:
            self.clock.advance(5.0)
            raise httpx.ReadTimeout("read timed out")
        message = {"jsonrpc": "2.0", "id": self.request_id, "result": self.document}
        yield b"event: message\ndata: " + json.dumps(message).encode() + b"\n\n"


def slow(
    clock: FakeClock,
    seconds: float,
    responder: Responder | None = None,
) -> Responder:
    """``responder``'s answer after ``seconds`` of fake time spent in the call."""

    def respond(request: httpx.Request) -> httpx.Response:
        clock.advance(seconds)
        if responder is None:
            raise httpx.ReadTimeout("read timed out")
        return responder(request)

    return respond


def trickling(
    clock: FakeClock, *, interval: float, chunks: int, document: object = None
) -> Responder:
    """A 200 event stream built by :class:`Stalling`."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=Stalling(
                clock, request, interval=interval, chunks=chunks, document=document
            ),
        )

    return respond


def raising(error: Exception) -> Responder:
    """A transport failure instead of an answer."""

    def respond(request: httpx.Request) -> httpx.Response:
        raise error

    return respond


SENSITIVE_RATE_LIMIT = "SENSITIVE_RATE_LIMIT_EXCEEDED"
FEED_DESCRIPTION = "A full synthetic description of an example dashboard build."
FEED_SNIPPET = "snippet-marker shortened feed text"


def feed_row(
    number: int,
    *,
    proposals: int | None = None,
    tier: str | None = "5 to 10",
    applied: bool = False,
    title: str | None = None,
) -> dict[str, object]:
    """One feed row as a search page lists it: posting id ``~02<number>``, feed id
    ``<number>``, a snippet that must never be delivered as a description."""
    row: dict[str, object] = {
        "id": str(number),
        "title": title or f"Example build {number}",
        "url": f"https://www.example.com/jobs/Example-build_~02{number}?source=feed",
        "job_type": "fixed",
        "budget": "$500 - $1,500",
        "description": FEED_SNIPPET,
        "applied": applied,
        "client": {"verification_status": "VERIFIED", "total_spent": "$2,000"},
    }
    if proposals is not None:
        row["proposal_count"] = proposals
    elif tier is not None:
        row["proposals_tier"] = tier
    return row


def feed_page(
    action: str,
    params: dict[str, Any],
    jobs: Sequence[object],
    *,
    cursor: str | None = None,
) -> dict[str, object]:
    """A page that passes every page check: the requested mode echoed and, when
    ``cursor`` is given, more claimed under the cursor field its action uses."""
    document: dict[str, object] = {"status": "ok", "jobs": list(jobs)}
    if action == "smart_search":
        document.update(
            mode=params.get("mode"), hasMore=cursor is not None, next_cursor=cursor
        )
    else:
        document["pageInfo"] = {"hasNextPage": cursor is not None, "endCursor": cursor}
    return document


def detail_document(description: str = FEED_DESCRIPTION) -> dict[str, object]:
    """A successful ``find_jobs get`` answer carrying the full description and the
    live client record."""
    return {
        "status": "ok",
        "data": {"marketplaceJobPosting": {"content": {"description": description}}},
        "client_record": {"spend_total": "$3,100.00", "hire_rate_percent": 40},
    }


def rate_limited_tool() -> Responder:
    """A ``find_jobs`` tool error carrying the sensitive-read rate-limit code."""
    return tool_result({"error_code": SENSITIVE_RATE_LIMIT}, is_error=True)


def arguments_of(request: httpx.Request) -> dict[str, Any]:
    """A ``tools/call`` request's arguments."""
    params = _mcp_message(request).get("params")
    arguments = params.get("arguments") if isinstance(params, dict) else None
    return arguments if isinstance(arguments, dict) else {}


@dataclass
class FakeMcpServer:
    """A Streamable HTTP MCP server for the run tests.

    By default it answers ``initialize`` (with an ``Mcp-Session-Id`` header), the
    ``initialized`` notification, ``tools/list`` (advertising write-capable tools
    beside the read-only ones), ``list_accounts`` (``accounts``, one TALENT account
    unless changed) and ``find_jobs`` from ``feeds`` and ``details``.

    ``feeds`` maps a search (its ``mode``, or ``"search"``) to its pages of rows: a
    page that is not the last claims more under the cursor ``<search>-<next>``,
    and an unknown search or page is an empty last page. A ``get`` answers
    ``details[<id>]`` (a document, or a responder), else :func:`detail_document`.

    ``queued`` answers the next requests of one name, in order, before anything
    else; ``script`` replaces the answer for a request named as
    :func:`script_name` names it (for example ``"initialize"``,
    ``"list_accounts"`` or ``"find_jobs:search"``); ``holds`` blocks one the same
    way. Every request is kept and counted by ``(method, tool, action)``; with a
    ``clock``, each one's start on its monotonic clock is kept in ``starts``.
    """

    resource_url: str = RESOURCE_URL
    accounts: list[dict[str, object]] = field(default_factory=lambda: [account()])
    feeds: dict[str, list[list[dict[str, object]]]] = field(default_factory=dict)
    details: dict[str, object] = field(default_factory=dict)
    queued: dict[str, list[Responder]] = field(default_factory=dict)
    script: dict[str, Responder] = field(default_factory=dict)
    holds: dict[str, Hold] = field(default_factory=dict)
    clock: FakeClock | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    counts: Counter[McpKey] = field(default_factory=Counter)
    starts: list[tuple[str, float]] = field(default_factory=list)

    def find_jobs_calls(self) -> list[tuple[str, dict[str, Any]]]:
        """Every ``find_jobs`` request's ``(action, params)``, in arrival order."""
        return [
            (str(arguments_of(r).get("action")), dict(arguments_of(r).get("params")))
            for r in self.requests
            if script_name(_mcp_key(r)).startswith("find_jobs")
        ]

    def find_jobs_starts(self) -> list[float]:
        return [at for name, at in self.starts if name.startswith("find_jobs")]

    def _find_jobs(self, request: httpx.Request) -> httpx.Response:
        arguments = arguments_of(request)
        action = str(arguments.get("action"))
        params = arguments.get("params")
        params = params if isinstance(params, dict) else {}
        if action == "get":
            detail = self.details.get(str(params.get("id")), detail_document())
            if callable(detail):
                return detail(request)
            return tool_result(detail)(request)
        search = str(params.get("mode") or "search")
        cursor = params.get("cursor")
        index = 0 if cursor is None else int(str(cursor).rsplit("-", 1)[1])
        pages = self.feeds.get(search, [])
        rows = pages[index] if index < len(pages) else []
        more = f"{search}-{index + 1}" if index + 1 < len(pages) else None
        return tool_result(feed_page(action, params, rows, cursor=more))(request)

    def calls(self, name: str) -> int:
        """How many requests :func:`script_name` names ``name``."""
        return sum(n for key, n in self.counts.items() if script_name(key) == name)

    def names(self) -> list[str]:
        """Every request's :func:`script_name`, in arrival order."""
        return [script_name(_mcp_key(r)) for r in self.requests]

    def default(self, name: str) -> Responder:
        if name == "initialize":
            return rpc_result(
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "example-jobs", "version": "1.0.0"},
                },
                headers={"Mcp-Session-Id": SESSION_ID},
            )
        if name == "notifications/initialized":
            return answer(202)
        if name == "tools/list":
            return rpc_result(
                {
                    "tools": [
                        {"name": tool, "inputSchema": {"type": "object"}}
                        for tool in ("list_accounts", "find_jobs", *WRITE_TOOLS)
                    ]
                }
            )
        if name == "list_accounts":
            return accounts_result(*self.accounts)
        if name.startswith("find_jobs"):
            return self._find_jobs
        return rpc_result(None)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = _mcp_key(request)
        self.counts[key] += 1
        name = script_name(key)
        if self.clock is not None:
            self.starts.append((name, self.clock.monotonic()))
        hold = self.holds.get(name)
        if hold is not None:
            hold.entered.set()
            assert hold.release.wait(timeout=60), f"{name} was never released"
        queued = self.queued.get(name)
        responder = (
            queued.pop(0) if queued else self.script.get(name) or self.default(name)
        )
        return responder(request)


def combined(
    authorization: FakeAuthorizationServer, mcp: FakeMcpServer
) -> httpx.MockTransport:
    """One transport for both fakes: a request to the resource URL that carries an
    ``Authorization`` header goes to the MCP server, everything else to the
    authorization server (whose unauthenticated probe answers 401)."""

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        if url == mcp.resource_url and "authorization" in request.headers:
            return mcp.handle(request)
        return authorization.handle(request)

    return httpx.MockTransport(handle)


# --- sightings and the real delivery step ---------------------------------------------

SIGHTED_AT = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
FULL_DESCRIPTION = "Build an example data pipeline for a fictional retailer."


def posting(
    posting_id: str = "~02100",
    *,
    title: str = "Example posting",
    description: str | None = None,
    proposal_count: int | None = 5,
    exact: bool | None = True,
    band: str | None = None,
    observed_at: datetime = SIGHTED_AT,
    purpose: Purpose = "discover",
    applied: bool = False,
    verified: bool | None = True,
    spend: str | None = "25000",
    rating: str | None = "4.9",
    reviews: int | None = 31,
    hire_rate: str | None = None,
    budget: tuple[str, str] | None = ("500", "1500"),
) -> NormalizedPosting:
    """A synthetic normalized posting, as ``normalize_row`` (and ``apply_detail``)
    would produce it. A count also stamps the proposal observation time."""
    return NormalizedPosting(
        posting_id=posting_id,
        title=title,
        posting_url=f"https://www.example.com/jobs/{posting_id}",
        description=description,
        job_type="fixed",
        budget_min=None if budget is None else Decimal(budget[0]),
        budget_max=None if budget is None else Decimal(budget[1]),
        proposal_count=proposal_count,
        proposal_count_exact=exact if proposal_count is not None else None,
        proposal_observed_at=observed_at if proposal_count is not None else None,
        posted_on=date(2026, 1, 4),
        client_payment_verified=verified,
        client_spend=None if spend is None else Decimal(spend),
        client_rating=None if rating is None else Decimal(rating),
        client_reviews=reviews,
        client_hire_rate=None if hire_rate is None else Decimal(hire_rate),
        applied=applied,
        observed_at=observed_at,
        purpose=purpose,
        search_ordinal=0,
        count_band_label=band,
        feed_id=f"feed{posting_id[3:]}",
    )


def discovered(posting_id: str = "~02100", **changes: Any) -> Sighting:
    """A discovery sighting whose detail read succeeded: it carries the full
    description and the detail-only hire rate."""
    changes.setdefault("description", FULL_DESCRIPTION)
    changes.setdefault("hire_rate", "0.8")
    return Sighting(posting(posting_id, **changes), detail_ok=True)


def refreshed(posting_id: str = "~02100", **changes: Any) -> Sighting:
    """A refresh-only sighting: no detail read, so no description."""
    changes.setdefault("purpose", "refresh_only")
    return Sighting(posting(posting_id, **changes), detail_ok=False)


def over_cap(posting_id: str = "~02100", **changes: Any) -> Sighting:
    """A sighting from the over-cap refresh search: the "50+" band."""
    changes.setdefault("proposal_count", 51)
    changes.setdefault("exact", False)
    changes.setdefault("band", "50+")
    return refreshed(posting_id, **changes)


@dataclass
class DeliveryRun:
    result: DeliveryResult
    checkpoints: int
    statements: list[str]


def run_delivery(
    c: Collector,
    sightings: Sequence[Sighting],
    *,
    now: datetime | None = None,
) -> DeliveryRun:
    """Deliver ``sightings`` through the real ``deliver`` in one work transaction,
    shaped as the run handler's Phase B: a sealed handler view of one unit of work,
    the lifecycle lock taken first, the poll connection's own acceptance context, then
    one commit. Every SQL statement the transaction executes is captured."""
    checkpoints: list[None] = []
    statements: list[str] = []

    def capture(*args: Any) -> None:
        statements.append(str(args[2]))

    with open_unit_of_work(c.owner) as uow:
        sa_event.listen(uow.connection, "before_cursor_execute", capture)
        try:
            handler = HandlerUnitOfWork(uow, consumers=c.intake.consumers)
            lock(handler)
            result = delivery.deliver(
                handler,
                c.poll,
                connection_id=c.connection_id,
                pipeline_id=c.pipeline_id,
                sightings=sightings,
                now=now or datetime.now(UTC),
                checkpoint=lambda: checkpoints.append(None),
            )
        finally:
            sa_event.remove(uow.connection, "before_cursor_execute", capture)
        uow.commit()
    return DeliveryRun(result, len(checkpoints), statements)


@contextmanager
def serve(
    server: FakeAuthorizationServer,
    *,
    clock: FakeClock | None = None,
    resolver: FakeResolver | None = None,
    mcp: FakeMcpServer | None = None,
) -> Iterator[Dependencies]:
    """Install ``server`` (and ``mcp`` beside it, when given) with a fake resolver
    and fake clocks, through the collector's dependency seam only."""
    with fakes(
        clock=clock,
        resolver=resolver,
        transport=httpx.MockTransport(server.handle)
        if mcp is None
        else combined(server, mcp),
    ) as deps:
        yield deps


# --- scheduler ticks, worker visits and run interleavings -----------------------------

COLLECT_OWNER = "jobfeed-run-test"


def collect_kinds() -> JobKindRegistry:
    """The Leads manifest's job kinds, as the worker registers them."""
    kinds = JobKindRegistry()
    for kind in MANIFEST.jobs:
        kinds.register(kind.name, kind.input_model, kind.handler)
    return kinds


def tick(i: Intake, ctx: WorkspaceContext, clock: FakeClock) -> None:
    """One scheduler tick alone (``run_due_schedules``) at the fake clock's instant:
    due rows enqueue, nothing runs."""
    with open_unit_of_work(ctx) as uow:
        run_due_schedules(uow.connection, workspace_id=i.workspace, now=clock.now())
        uow.commit()


def visit(i: Intake, clock: FakeClock) -> VisitResult:
    """One real worker visit on the fake clock: the scheduler tick, then the job
    drain. The cancellation token the worker hands the handler runs on the same
    clock."""
    return visit_workspace(
        DueWorkspace(workspace_id=i.workspace, observed_due_at=None),
        kinds=collect_kinds(),
        consumers=i.consumers,
        backend=i.cluster.backend,
        owner=COLLECT_OWNER,
        clock=clock.now,
        jitter=None,
    )


@dataclass(frozen=True)
class JobState:
    state: str
    last_error: str | None
    attempts: int
    next_run_at: datetime


def collect_jobs(ctx: WorkspaceContext) -> list[JobState]:
    """Every collector job's final state, oldest first."""
    with open_unit_of_work(ctx) as uow:
        return [
            JobState(row.state, row.last_error, row.attempts, row.next_run_at)
            for row in uow.connection.execute(
                select(job.c.state, job.c.last_error, job.c.attempts, job.c.next_run_at)
                .where(job.c.kind == COLLECT_JOB_KIND)
                .order_by(job.c.created_at, job.c.id)
            )
        ]


def invoke(c: Collector, token: Any) -> None:
    """The handler called directly, as the worker calls it: on a sealed view of one
    work transaction, committed after it returns. ``token`` stands in for the
    worker's cancellation token."""
    with open_unit_of_work(c.owner) as uow:
        collect_job(
            HandlerUnitOfWork(uow, consumers=c.intake.consumers),
            CollectPayload(workspace_id=c.intake.workspace),
            token,
        )
        uow.commit()


def grant_type(request: httpx.Request) -> str:
    return parse_qs(request.content.decode("ascii")).get("grant_type", [""])[0]


def refresh_requests(server: FakeAuthorizationServer) -> list[httpx.Request]:
    """The token endpoint's refresh grants alone (a reconnect's code exchange is
    not a refresh)."""
    return [r for r in server.token_requests if grant_type(r) == "refresh_token"]


def before(callback: Callable[[], None], responder: Responder) -> Responder:
    """A token endpoint that runs ``callback`` just before it answers the first
    refresh grant; every other request (a reconnect's own code exchange included)
    goes straight to ``responder``."""
    fired: list[bool] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if not fired and grant_type(request) == "refresh_token":
            fired.append(True)
            callback()
        return responder(request)

    return respond


def _is_claim(statement: str) -> bool:
    return (
        statement.lstrip()
        .upper()
        .startswith("UPDATE LEADS.JOBFEED_CONNECTION SET RUN_CLAIMED_AT")
    )


def _is_step_one_read(statement: str) -> bool:
    upper = statement.lstrip().upper()
    return (
        upper.startswith("SELECT LEADS.JOBFEED_CONNECTION.CONNECTION_ID")
        and "FOR UPDATE" not in upper
    )


@contextmanager
def _after_unit(
    matches: Callable[[str], bool], callback: Callable[[], None], what: str
) -> Iterator[None]:
    """Run ``callback`` once, after the first unit that executed a statement
    ``matches`` recognises has ended: an owner operation then runs on connections
    of its own.

    Core's connection events offer no after-commit hook (``commit`` fires before
    the database commits), so the callback runs when that unit gives its
    connection back to the pool, which happens only after its transaction ended.
    """
    seen: set[int] = set()
    fired: list[bool] = []

    def watch(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if not fired and matches(statement):
            seen.add(id(conn.connection.dbapi_connection))

    def returned(dbapi_connection: Any, record: Any) -> None:
        if not fired and id(dbapi_connection) in seen:
            fired.append(True)
            callback()

    sa_event.listen(Engine, "before_cursor_execute", watch)
    sa_event.listen(Pool, "checkin", returned)
    try:
        yield
    finally:
        sa_event.remove(Engine, "before_cursor_execute", watch)
        sa_event.remove(Pool, "checkin", returned)
    assert fired, f"the run never reached {what}"


def after_claim(callback: Callable[[], None]) -> AbstractContextManager[None]:
    """Run ``callback`` once, after the run's claim unit (its ``UPDATE ... SET
    run_claimed_at``) has committed, before the refresh."""
    return _after_unit(_is_claim, callback, "its claim")


def after_step_one_read(
    callback: Callable[[], None],
) -> AbstractContextManager[None]:
    """Run ``callback`` once, after the run's first short read of the connection
    has ended, before the claim."""
    return _after_unit(_is_step_one_read, callback, "its first read")
