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
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from rheo_core.connectors.oauth import Discovered, RegisteredClient
from rheo_core.operations import dispatch
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.refs import uuid7
from rheo_core.settings import deployment as settings_deployment
from rheo_core.settings import resolver as settings_resolver
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads.jobfeed import Dependencies, use_dependencies
from rheo_leads.jobfeed.install import install_connection
from rheo_leads.references import ref

# A global IPv6 literal, as the egress tests use: the provenance gate flags any
# public IPv4 literal, and the outbound check refuses a non-public address.
PUBLIC_ADDRESS = "2600::1"
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
    """

    refresh: bool = True
    rotating: bool = True
    issued: list[str] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("ascii"))
        grant = form.get("grant_type", [""])[0]
        access = f"access-marker-{uuid4()}"
        document: dict[str, Any] = {"token_type": "Bearer", "access_token": access}
        self.issued.append(access)
        if (grant == "authorization_code" and self.refresh) or (
            grant == "refresh_token" and self.rotating
        ):
            document["refresh_token"] = f"refresh-marker-{uuid4()}"
            self.issued.append(document["refresh_token"])
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
    (protected-resource metadata), ``"registration"`` or ``"token"`` request until
    released. :meth:`authorize` stands in for the owner's consent and mints a code.
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
) -> None:
    """Deployment settings for the connector client and a fresh secret data root.

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
    monkeypatch.setenv("RHEO__leads__jobfeed__resource_url", resource_url)
    monkeypatch.setenv("RHEO__routing__mode", "subdomain")
    monkeypatch.setenv("RHEO__routing__scheme", "https")
    monkeypatch.setenv("RHEO__routing__base_host", routing_host)
    monkeypatch.setenv("RHEO__routing__public_host", "")
    if identity_path is None:
        monkeypatch.delenv("RHEO__routing__identity__path", raising=False)
    else:
        monkeypatch.setenv("RHEO__routing__identity__path", identity_path)


# --- a basic fake MCP server ----------------------------------------------------------

McpKey = tuple[str, str | None, str | None]
"""``(method, tool, action)`` of one JSON-RPC request; tool and action are ``None``
when the request has none."""


def _mcp_key(request: httpx.Request) -> McpKey:
    try:
        message = json.loads(request.content)
    except ValueError:
        return ("<unparsed>", None, None)
    if not isinstance(message, dict):
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


@dataclass
class FakeMcpServer:
    """Counts every request it receives by method, tool and action, and answers from
    ``script`` (keyed by method), else a JSON-RPC ``method not found`` error."""

    resource_url: str = RESOURCE_URL
    script: dict[str, Responder] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    counts: Counter[McpKey] = field(default_factory=Counter)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = _mcp_key(request)
        self.counts[key] += 1
        responder = self.script.get(key[0])
        if responder is not None:
            return responder(request)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32601, "message": "method not found"},
            },
        )


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
