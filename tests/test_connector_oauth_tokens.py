"""Core's OAuth client: the code exchange, refresh with rotation, and revocation, each
authenticated as the registered client.

A counting fake token endpoint and a fake revocation endpoint sit behind an
``httpx.MockTransport``; host names go to an injected resolver and budgets run on a
fake clock, so no test touches the network. Refresh tokens and client secrets live as
files in a real secret store under a temporary data root.
"""

import base64
import copy
import json
import logging
import os
import pickle
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus
from uuid import UUID

import httpx
import pytest
from rheo_core.connectors.oauth import (
    AccessToken,
    ClientAuth,
    OAuthFailure,
    Revocation,
    exchange_code,
    new_reference,
    refresh,
    revoke,
)
from rheo_core.secrets import SecretRef, SecretRefusal, SecretStore, SecretValue

PUBLIC = "2600::1"
"""A global unicast address; no test resolves or contacts it."""
TOKEN = "https://auth.example.com/token"
REVOKE = "https://auth.example.com/revoke"
ELSEWHERE = "https://www.example.com/landing"
RESOURCE = "https://mcp.example.com/mcp"
REDIRECT = "https://www.example.com/auth/connector/leads/callback"
WS = UUID("0190f3a2-6b1e-7c11-8d2a-3f4b5c6d7e01")
CONN = UUID("0190f3a2-6b1e-7c11-8d2a-3f4b5c6d7e02")
OTHER_CONN = UUID("0190f3a2-6b1e-7c11-8d2a-3f4b5c6d7e03")
NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
CLIENT_ID = "client id:1"
CODE = "code-marker-2c51a7d0-9e3b-4f61-8a02-5d7c1e9b3f44"
VERIFIER = "verifier-marker-7a1e3c5b-0d9f-4b28-96e4-1f3a5c7e9b02"
ACCESS = "access-marker-0b6d2f84-1c3e-4a57-9f20-8e6d4c2a1b39"
REFRESH = "refresh-marker-4e2c8a61-7b3d-4f09-a5e1-2c9b7d3f6a18"
NEW_REFRESH = "refresh-marker-9d3b5f17-2a4c-4e86-b1d0-7f5e3a9c2b64"
SECRET = "client-secret-marker-5d1c7a9e-2b44-4f0e-9a31-7c2d18e6b0f4"
REG_TOKEN = "registration-token-marker-8e3a1f62-0c7d-4b59-a4e2-91d5c3b7f806"
ORG = "org-marker-3f7a9c1e-5b2d-4e80-a6f4-0d8c2b6e4a91"
MARKERS = (CODE, VERIFIER, ACCESS, REFRESH, NEW_REFRESH, SECRET, REG_TOKEN, ORG)
METHODS = ("none", "client_secret_basic", "client_secret_post")

NON_PUBLIC = {
    "loopback": "127.0.0.1",
    "rfc1918": "10.1.2.3",
    "link-local": "169.254.169.254",
    "unique-local": "fd00::5",
    "unspecified": "0.0.0.0",
    "reserved": "240.0.0.9",
    "multicast": "239.1.1.1",
    "nat64-private": "64:ff9b::10.1.2.3",
}

NONE_CLIENT = ClientAuth(
    client_id=CLIENT_ID,
    auth_method="none",
    client_secret_ref=None,
    secret_expires_at=None,
)

Handler = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Trickle(httpx.SyncByteStream):
    """A body that yields one byte every 4 s of fake time, forever."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def __iter__(self) -> Iterator[bytes]:
        while True:
            self.clock.advance(4.0)
            yield b" "


def token_document(**extra: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "access_token": ACCESS,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": NEW_REFRESH,
    }
    document.update(extra)
    return document


def _json(status: int, document: object) -> Handler:
    return lambda r: httpx.Response(status, json=document)


class FakeServer:
    """Counting token and revocation endpoints that keep every request received."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.requests: list[httpx.Request] = []
        self.addresses: dict[str, str] = {}
        self.issued = 0
        self.token: Handler = self.issue
        self.revocation: Handler = lambda r: httpx.Response(200)

    def issue(self, request: httpx.Request) -> httpx.Response:
        """A good token response carrying a refresh token never issued before."""
        self.issued += 1
        refresh_token = f"{NEW_REFRESH}-{self.issued}"
        return httpx.Response(200, json=token_document(refresh_token=refresh_token))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/revoke":
            return self.revocation(request)
        return self.token(request)

    def resolve(self, host: str) -> Sequence[str]:
        return [self.addresses.get(host, PUBLIC)]

    def _io(self) -> dict[str, Any]:
        return {
            "resolver": self.resolve,
            "clock": self.clock,
            "transport": httpx.MockTransport(self.handle),
        }

    def exchange(
        self,
        client: ClientAuth = NONE_CLIENT,
        *,
        endpoint: str = TOKEN,
        now: datetime = NOW,
    ) -> tuple[str, AccessToken]:
        return exchange_code(
            endpoint,
            code=CODE,
            redirect_uri=REDIRECT,
            code_verifier=VERIFIER,
            resource=RESOURCE,
            client=client,
            workspace_id=WS,
            connection_id=CONN,
            now=now,
            **self._io(),
        )

    def refresh(
        self,
        refresh_ref: str,
        client: ClientAuth = NONE_CLIENT,
        *,
        endpoint: str = TOKEN,
        now: datetime = NOW,
    ) -> tuple[AccessToken, str | None]:
        return refresh(
            endpoint,
            refresh_ref=refresh_ref,
            resource=RESOURCE,
            client=client,
            workspace_id=WS,
            connection_id=CONN,
            now=now,
            **self._io(),
        )

    def revoke(
        self,
        refresh_ref: str,
        client: ClientAuth = NONE_CLIENT,
        *,
        endpoint: str | None = REVOKE,
        now: datetime = NOW,
    ) -> Revocation:
        return revoke(
            endpoint,
            refresh_ref=refresh_ref,
            client=client,
            workspace_id=WS,
            connection_id=CONN,
            now=now,
            **self._io(),
        )

    def run(self, op: str, client: ClientAuth = NONE_CLIENT, **kw: Any) -> object:
        """``exchange`` or ``refresh`` (against a freshly stored refresh token)."""
        if op == "exchange":
            return self.exchange(client, **kw)
        return self.refresh(stored(REFRESH), client, **kw)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "secrets").mkdir(mode=0o700)
    monkeypatch.setenv("RHEO_DATA_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake(root: Path) -> FakeServer:
    return FakeServer()


def stored(value: str, connection_id: UUID = CONN) -> str:
    """A file in the connection's ``oauth/`` namespace, written by test setup."""
    ref = new_reference(WS, connection_id)
    store = SecretStore(Path(os.environ["RHEO_DATA_ROOT"]))
    writable = SecretStore.scope_for(
        "test-setup", f"secret://file/ws/{WS}/connection/", writable=True
    )
    store.create(SecretRef.parse(ref), SecretValue(value.encode()), writable)
    return ref


def secret_client(method: str, *, expires_at: datetime | None = None) -> ClientAuth:
    return ClientAuth(
        client_id=CLIENT_ID,
        auth_method=method,  # type: ignore[arg-type]
        client_secret_ref=stored(SECRET) if method != "none" else None,
        secret_expires_at=expires_at,
    )


def file_for(root: Path, ref: str) -> Path:
    return root / "secrets" / SecretRef.parse(ref).id


def stored_files(root: Path) -> list[Path]:
    return sorted(p for p in (root / "secrets").rglob("*") if p.is_file())


def form_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode(), keep_blank_values=True)


def sent_text(request: httpx.Request) -> str:
    """Everything a request carries: URL, headers (Basic decoded) and body."""
    parts = [str(request.url), request.content.decode()]
    for name, value in request.headers.items():
        parts.append(f"{name}: {value}")
        if name == "authorization" and value.startswith("Basic "):
            parts.append(base64.b64decode(value.removeprefix("Basic ")).decode())
    return "\n".join(parts)


def assert_content_free(exc: BaseException, code: str, *hidden: str) -> None:
    assert str(exc) == code
    assert repr(exc) == code
    for marker in (*MARKERS, *hidden):
        assert marker not in str(exc) and marker not in repr(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


def failure_of(call: Callable[[], object]) -> OAuthFailure:
    with pytest.raises(OAuthFailure) as caught:
        call()
    return caught.value


def all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(
        f"{r.name} {r.getMessage()} {r.exc_text or ''} {r.args!r}"
        for r in caplog.records
    )


FAILED = {"exchange": "exchange_failed", "refresh": "token_refresh_failed"}


# --- the code exchange (AC 6, I5) ---------------------------------------------------


def test_exchange_accepts_a_bearer_response_and_writes_exactly_one_reference(
    fake: FakeServer, root: Path
) -> None:
    ref, access = fake.exchange()
    assert ref.startswith(f"secret://file/ws/{WS}/connection/{CONN}/oauth/")
    assert stored_files(root) == [file_for(root, ref)]
    assert file_for(root, ref).read_text() == f"{NEW_REFRESH}-1"
    assert access.authorization_header() == f"Bearer {ACCESS}"
    [request] = fake.requests
    assert request.method == "POST"
    assert str(request.url) == TOKEN
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert form_of(request) == {
        "grant_type": ["authorization_code"],
        "code": [CODE],
        "redirect_uri": [REDIRECT],
        "code_verifier": [VERIFIER],
        "resource": [RESOURCE],
        "client_id": [CLIENT_ID],
    }


@pytest.mark.parametrize("token_type", ["bearer", "BEARER", "Bearer"])
def test_the_token_type_is_compared_case_insensitively(
    fake: FakeServer, token_type: str
) -> None:
    fake.token = _json(200, token_document(token_type=token_type))
    fake.exchange()


@pytest.mark.parametrize(
    "document",
    [
        token_document(token_type="mac"),
        {k: v for k, v in token_document().items() if k != "token_type"},
        token_document(token_type=None),
        token_document(access_token=""),
        token_document(access_token=7),
        token_document(access_token="has space"),
        token_document(access_token="line\nbreak"),
        token_document(refresh_token=["not", "a", "string"]),
        ["not", "an", "object"],
    ],
    ids=[
        "mac",
        "no token type",
        "null token type",
        "empty access token",
        "numeric access token",
        "space in access token",
        "newline in access token",
        "list refresh token",
        "array",
    ],
)
def test_a_malformed_token_response_is_refused_and_writes_nothing(
    fake: FakeServer, root: Path, document: object
) -> None:
    fake.token = _json(200, document)
    failure = failure_of(fake.exchange)
    assert_content_free(failure, "exchange_failed")
    assert stored_files(root) == []


@pytest.mark.parametrize(
    "document",
    [
        {k: v for k, v in token_document().items() if k != "refresh_token"},
        token_document(refresh_token=None),
        token_document(refresh_token=""),
    ],
    ids=["absent", "null", "empty"],
)
def test_a_response_without_a_refresh_token_writes_nothing(
    fake: FakeServer, root: Path, document: object
) -> None:
    fake.token = _json(200, document)
    failure = failure_of(fake.exchange)
    assert_content_free(failure, "no_refresh_token")
    assert stored_files(root) == []


def test_exchange_refuses_an_id_that_is_not_a_uuid_with_zero_requests(
    fake: FakeServer, root: Path
) -> None:
    failure = failure_of(
        lambda: exchange_code(
            TOKEN,
            code=CODE,
            redirect_uri=REDIRECT,
            code_verifier=VERIFIER,
            resource=RESOURCE,
            client=NONE_CLIENT,
            workspace_id=str(WS),  # type: ignore[arg-type]
            connection_id=CONN,
            resolver=fake.resolve,
            clock=fake.clock,
            transport=httpx.MockTransport(fake.handle),
        )
    )
    assert_content_free(failure, "endpoint_misconfigured")
    assert fake.requests == []
    assert stored_files(root) == []


# --- refresh and rotation (AC 13, I5, I7) -------------------------------------------


def test_refresh_sends_the_grant_with_the_pinned_resource_and_no_scope(
    fake: FakeServer,
) -> None:
    ref = stored(REFRESH)
    fake.token = _json(200, token_document(refresh_token=None))
    access, rotated = fake.refresh(ref)
    assert rotated is None
    assert access.authorization_header() == f"Bearer {ACCESS}"
    [request] = fake.requests
    assert str(request.url) == TOKEN
    assert form_of(request) == {
        "grant_type": ["refresh_token"],
        "refresh_token": [REFRESH],
        "resource": [RESOURCE],
        "client_id": [CLIENT_ID],
    }


def test_resource_rides_every_token_request_and_never_a_revocation(
    fake: FakeServer,
) -> None:
    ref, _ = fake.exchange()
    access, rotated = fake.refresh(ref)
    assert rotated is not None
    fake.token = _json(200, token_document(refresh_token=None))
    fake.refresh(rotated)
    assert fake.revoke(rotated) == Revocation("revoked", client_rejected=False)
    token_requests = [r for r in fake.requests if str(r.url) == TOKEN]
    revocations = [r for r in fake.requests if str(r.url) == REVOKE]
    assert len(token_requests) == 3 and len(revocations) == 1
    for request in token_requests:
        assert form_of(request)["resource"] == [RESOURCE]
        assert "scope" not in form_of(request)
    assert "resource" not in form_of(revocations[0])


def test_a_rotated_refresh_token_lands_in_a_fresh_leaf(
    fake: FakeServer, root: Path
) -> None:
    ref = stored(REFRESH)
    _, rotated = fake.refresh(ref)
    assert rotated is not None and rotated != ref
    assert rotated.startswith(f"secret://file/ws/{WS}/connection/{CONN}/oauth/")
    assert file_for(root, rotated).read_text() == f"{NEW_REFRESH}-1"
    # Retiring the old file is the caller's job, after its compare-and-set commits.
    assert file_for(root, ref).read_text() == REFRESH
    assert len(stored_files(root)) == 2


def test_a_refresh_token_equal_to_the_current_one_is_not_a_rotation(
    fake: FakeServer, root: Path
) -> None:
    ref = stored(REFRESH)
    fake.token = _json(200, token_document(refresh_token=REFRESH))
    _, rotated = fake.refresh(ref)
    assert rotated is None
    assert stored_files(root) == [file_for(root, ref)]


def _failing_create(monkeypatch: pytest.MonkeyPatch, error: Exception) -> list[int]:
    calls: list[int] = []

    def create(
        self: SecretStore, ref: SecretRef, value: SecretValue, scope: Any
    ) -> None:
        calls.append(1)
        raise error

    monkeypatch.setattr(SecretStore, "create", create)
    return calls


def _write_errors(root: Path) -> dict[str, Exception]:
    path_text = str(root / "secrets" / "ws")
    return {
        "refusal": SecretRefusal("secret_write_failed", f"could not write {path_text}"),
        "oserror": OSError(28, f"No space left on device: {path_text}"),
    }


@pytest.mark.parametrize("kind", ["refusal", "oserror"])
def test_a_failed_rotation_write_keeps_the_old_reference(
    fake: FakeServer, root: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    ref = stored(REFRESH)
    real_create = SecretStore.create
    calls = _failing_create(monkeypatch, _write_errors(root)[kind])
    failure = failure_of(lambda: fake.refresh(ref))
    assert_content_free(failure, "secret_write_failed", str(root), ref)
    assert failure.terminal is False
    assert len(calls) == 1
    assert stored_files(root) == [file_for(root, ref)]
    assert file_for(root, ref).read_text() == REFRESH
    # The old reference still refreshes.
    monkeypatch.setattr(SecretStore, "create", real_create)
    fake.token = _json(200, token_document(refresh_token=None))
    fake.refresh(ref)


@pytest.mark.parametrize("kind", ["refusal", "oserror"])
def test_a_failed_exchange_write_leaves_no_file(
    fake: FakeServer, root: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _failing_create(monkeypatch, _write_errors(root)[kind])
    failure = failure_of(fake.exchange)
    assert_content_free(failure, "secret_write_failed", str(root))
    assert stored_files(root) == []


def test_a_discard_failure_after_a_failed_write_is_swallowed_and_logged(
    fake: FakeServer,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    real_create = SecretStore.create

    def create_then_fail(
        self: SecretStore, ref: SecretRef, value: SecretValue, scope: Any
    ) -> None:
        real_create(self, ref, value, scope)
        raise OSError(5, f"I/O error after publishing {root}")

    def broken_discard(self: SecretStore, ref: SecretRef, scope: Any) -> None:
        raise RuntimeError(f"cannot remove {root}/{ref}")

    monkeypatch.setattr(SecretStore, "create", create_then_fail)
    monkeypatch.setattr(SecretStore, "discard", broken_discard)
    with caplog.at_level(logging.DEBUG):
        failure = failure_of(fake.exchange)
    assert_content_free(failure, "secret_write_failed", str(root))
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("rheo")]
    assert ours == ["secret_discard_failed"]
    assert str(root) not in all_log_text(caplog)


def test_every_reference_is_a_fresh_leaf(fake: FakeServer, root: Path) -> None:
    references = {fake.exchange()[0] for _ in range(3)}
    current = stored(REFRESH)
    for _ in range(3):
        _, rotated = fake.refresh(current)
        assert rotated is not None
        references.add(rotated)
    assert current not in references
    assert len(references) == 6
    assert len(stored_files(root)) == 7


# --- destinations (AC 76, I6) -------------------------------------------------------


def test_tokens_go_only_to_their_pinned_endpoints(fake: FakeServer) -> None:
    ref, access = fake.exchange()
    _, rotated = fake.refresh(ref)
    assert rotated is not None
    fake.revoke(rotated)
    assert len(fake.requests) == 3
    assert access.authorization_header() == f"Bearer {ACCESS}"
    for request in fake.requests:
        text = sent_text(request)
        assert ACCESS not in text
        assert REG_TOKEN not in text
        if NEW_REFRESH in text:
            assert str(request.url) in (TOKEN, REVOKE)
    assert CODE in sent_text(fake.requests[0])
    assert [CODE in sent_text(r) for r in fake.requests] == [True, False, False]


def _redirect(r: httpx.Request) -> httpx.Response:
    return httpx.Response(307, headers={"Location": ELSEWHERE})


@pytest.mark.parametrize("op", ["exchange", "refresh"])
def test_a_token_redirect_is_misconfigured_and_nothing_follows_it(
    fake: FakeServer, root: Path, op: str
) -> None:
    fake.token = _redirect
    before = set(stored_files(root))
    failure = failure_of(lambda: fake.run(op))
    assert_content_free(failure, "endpoint_misconfigured")
    assert [str(r.url) for r in fake.requests] == [TOKEN]
    assert len(set(stored_files(root)) - before) <= 1  # only the test's own file


def test_a_revocation_redirect_fails_and_nothing_follows_it(fake: FakeServer) -> None:
    ref = stored(REFRESH)
    fake.revocation = _redirect
    assert fake.revoke(ref) == Revocation("revocation_failed", client_rejected=False)
    assert [str(r.url) for r in fake.requests] == [REVOKE]


# --- client authentication at both endpoints (AC 80) --------------------------------


def _assert_authenticated(request: httpx.Request, method: str) -> None:
    form = form_of(request)
    if method == "none":
        assert form["client_id"] == [CLIENT_ID]
        assert "client_secret" not in form
        assert "authorization" not in request.headers
    elif method == "client_secret_basic":
        assert "client_id" not in form and "client_secret" not in form
        pair = f"{quote_plus(CLIENT_ID)}:{quote_plus(SECRET)}"
        expected = base64.b64encode(pair.encode()).decode()
        assert request.headers["authorization"] == f"Basic {expected}"
        assert SECRET not in request.content.decode()
    else:
        assert form["client_id"] == [CLIENT_ID]
        assert form["client_secret"] == [SECRET]
        assert "authorization" not in request.headers


@pytest.mark.parametrize("method", METHODS)
def test_each_method_authenticates_every_token_and_revocation_request(
    fake: FakeServer, method: str
) -> None:
    client = secret_client(method)
    stored(REG_TOKEN)  # a registration access token on disk is sent nowhere
    ref, _ = fake.exchange(client)
    _, rotated = fake.refresh(ref, client)
    assert rotated is not None
    assert fake.revoke(rotated, client).outcome == "revoked"
    assert len(fake.requests) == 3
    for request in fake.requests:
        _assert_authenticated(request, method)
        text = sent_text(request)
        assert REG_TOKEN not in text
        if SECRET in text:
            assert method != "none"
            assert str(request.url) in (TOKEN, REVOKE)


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
def test_an_expired_secret_is_never_sent(fake: FakeServer, method: str) -> None:
    client = secret_client(method, expires_at=NOW - timedelta(seconds=1))
    ref = stored(REFRESH)
    for call in (lambda: fake.exchange(client), lambda: fake.refresh(ref, client)):
        failure = failure_of(call)
        assert_content_free(failure, "client_rejected")
        assert failure.terminal is True
    assert fake.revoke(ref, client) == Revocation(
        "revocation_failed", client_rejected=True
    )
    assert fake.requests == []


# --- terminal and transient mapping (AC 47, AC 79) ----------------------------------


def _error(status: int, error: str) -> Handler:
    return _json(status, {"error": error, "error_description": f"{ORG} {ACCESS}"})


TERMINAL = {
    "invalid_grant": (_error(400, "invalid_grant"), "invalid_grant"),
    "unauthorized_client": (_error(400, "unauthorized_client"), "unauthorized_client"),
    "invalid_client 400": (_error(400, "invalid_client"), "client_rejected"),
    "invalid_client 401": (_error(401, "invalid_client"), "client_rejected"),
}


@pytest.mark.parametrize("op", ["exchange", "refresh"])
@pytest.mark.parametrize("case", sorted(TERMINAL))
def test_terminal_token_errors(
    fake: FakeServer, root: Path, op: str, case: str
) -> None:
    handler, code = TERMINAL[case]
    fake.token = handler
    before = set(stored_files(root))
    failure = failure_of(lambda: fake.run(op))
    assert_content_free(failure, code)
    assert failure.terminal is True
    assert failure.retry_after is None
    assert [str(r.url) for r in fake.requests] == [TOKEN]  # no retry, no registration
    assert len(set(stored_files(root)) - before) <= 1  # only the test's own file


def test_unauthorized_client_is_not_a_client_rejection(fake: FakeServer) -> None:
    fake.token = _error(400, "unauthorized_client")
    failure = failure_of(lambda: fake.run("refresh"))
    assert failure.code == "unauthorized_client"
    assert failure.code != "client_rejected"


def _raises(error: type[Exception]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error(f"upstream {ORG} {ACCESS}", request=request)  # type: ignore[call-arg]

    return handler


TRANSIENT: dict[str, tuple[Handler, str | None, int | None]] = {
    "429 seconds": (
        lambda r: httpx.Response(429, headers={"Retry-After": "120"}),
        "rate_limited",
        120,
    ),
    "429 date": (
        lambda r: httpx.Response(
            429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        ),
        "rate_limited",
        None,
    ),
    "429 none": (lambda r: httpx.Response(429), "rate_limited", None),
    "500": (lambda r: httpx.Response(500, text="oops"), None, None),
    "503": (lambda r: httpx.Response(503), None, None),
    "404": (lambda r: httpx.Response(404), None, None),
    "400 invalid_request": (_error(400, "invalid_request"), None, None),
    "500 invalid_client": (_error(500, "invalid_client"), None, None),
    "201": (_json(201, token_document()), None, None),
    "timeout": (_raises(httpx.ReadTimeout), "unavailable", None),
    "network error": (_raises(httpx.ConnectError), "unavailable", None),
}


@pytest.mark.parametrize("op", ["exchange", "refresh"])
@pytest.mark.parametrize("case", sorted(TRANSIENT))
def test_transient_token_failures(fake: FakeServer, op: str, case: str) -> None:
    handler, code, retry_after = TRANSIENT[case]
    fake.token = handler
    failure = failure_of(lambda: fake.run(op))
    assert_content_free(failure, code or FAILED[op])
    assert failure.terminal is False
    assert failure.retry_after == retry_after
    assert len(fake.requests) == 1


# --- revocation outcomes ------------------------------------------------------------


REVOCATIONS: dict[str, tuple[Handler, Revocation]] = {
    "200": (lambda r: httpx.Response(200), Revocation("revoked", False)),
    "400": (_error(400, "invalid_request"), Revocation("revocation_failed", False)),
    "503": (lambda r: httpx.Response(503), Revocation("revocation_failed", False)),
    "invalid_client 400": (
        _error(400, "invalid_client"),
        Revocation("revocation_failed", True),
    ),
    "invalid_client 401": (
        _error(401, "invalid_client"),
        Revocation("revocation_failed", True),
    ),
    "timeout": (_raises(httpx.ReadTimeout), Revocation("revocation_failed", False)),
    "network error": (
        _raises(httpx.ConnectError),
        Revocation("revocation_failed", False),
    ),
}


@pytest.mark.parametrize("case", sorted(REVOCATIONS))
def test_revocation_outcomes(fake: FakeServer, case: str) -> None:
    handler, expected = REVOCATIONS[case]
    fake.revocation = handler
    ref = stored(REFRESH)
    assert fake.revoke(ref) == expected
    [request] = fake.requests
    assert str(request.url) == REVOKE
    assert form_of(request) == {
        "token": [REFRESH],
        "token_type_hint": ["refresh_token"],
        "client_id": [CLIENT_ID],
    }


def test_no_pinned_revocation_endpoint_is_unavailable_with_zero_requests(
    fake: FakeServer,
) -> None:
    ref = stored(REFRESH)
    assert fake.revoke(ref, endpoint=None) == Revocation(
        "revocation_unavailable", client_rejected=False
    )
    assert fake.requests == []


def test_a_revocation_is_frozen() -> None:
    assert Revocation("revoked", False).outcome == "revoked"
    with pytest.raises(AttributeError):
        Revocation("revoked", False).outcome = "revocation_failed"  # type: ignore[misc]


# --- secret-store refusals ----------------------------------------------------------


@pytest.mark.parametrize("how", ["missing", "mode 0644"])
def test_an_unreadable_refresh_token_is_terminal_with_zero_requests(
    fake: FakeServer, root: Path, how: str
) -> None:
    ref = stored(REFRESH)
    path = file_for(root, ref)
    if how == "missing":
        path.unlink()
    else:
        path.chmod(0o644)
    failure = failure_of(lambda: fake.refresh(ref))
    assert_content_free(failure, "secret_unavailable", ref, str(root), str(path))
    assert failure.terminal is True
    assert fake.requests == []


def test_another_connections_refresh_token_is_unavailable(
    fake: FakeServer,
) -> None:
    ref = stored(REFRESH, OTHER_CONN)
    failure = failure_of(lambda: fake.refresh(ref))
    assert_content_free(failure, "secret_unavailable", ref)
    assert failure.terminal is True
    assert fake.requests == []


@pytest.mark.parametrize("op", ["exchange", "refresh"])
@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
def test_a_missing_client_secret_is_client_rejected_with_zero_requests(
    fake: FakeServer, root: Path, op: str, method: str
) -> None:
    client = secret_client(method)
    assert client.client_secret_ref is not None
    file_for(root, client.client_secret_ref).unlink()
    failure = failure_of(lambda: fake.run(op, client))
    assert_content_free(failure, "client_rejected", client.client_secret_ref, str(root))
    assert failure.terminal is True
    assert fake.requests == []


def test_revoke_with_the_refresh_token_missing_fails_without_rejecting_the_client(
    fake: FakeServer, root: Path
) -> None:
    client = secret_client("client_secret_basic")
    ref = stored(REFRESH)
    file_for(root, ref).unlink()
    assert fake.revoke(ref, client) == Revocation("revocation_failed", False)
    assert fake.requests == []


def test_revoke_with_the_client_secret_missing_rejects_the_client(
    fake: FakeServer, root: Path
) -> None:
    client = secret_client("client_secret_post")
    assert client.client_secret_ref is not None
    file_for(root, client.client_secret_ref).unlink()
    assert fake.revoke(stored(REFRESH), client) == Revocation("revocation_failed", True)
    assert fake.requests == []


def test_revoke_never_raises(
    fake: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"unexpected {ORG} {REFRESH}")

    ref = stored(REFRESH)
    unexpected = Revocation("revocation_failed", False)
    with caplog.at_level(logging.DEBUG):
        fake.revocation = explode
        assert fake.revoke(ref) == unexpected
        # Malformed inputs land the same way, before any request.
        assert fake.revoke(None) == unexpected  # type: ignore[arg-type]
        assert fake.revoke(ref, endpoint=42) == unexpected  # type: ignore[arg-type]
    assert len(fake.requests) == 1
    text = all_log_text(caplog)
    for marker in MARKERS:
        assert marker not in text
    assert ref not in text


def test_an_unexpected_revocation_error_is_logged_by_type_only(
    fake: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise LookupError(f"secret {REFRESH}")

    monkeypatch.setattr("rheo_core.connectors.oauth.apply_client_authentication", boom)
    with caplog.at_level(logging.DEBUG):
        result = fake.revoke(stored(REFRESH))
    assert result == Revocation("revocation_failed", False)
    assert [r.getMessage() for r in caplog.records] == [
        "revocation_unexpected_error LookupError"
    ]
    assert all(r.exc_info is None for r in caplog.records)


# --- public addresses only (AC 72) ---------------------------------------------------


def _calls(fake: FakeServer, endpoint: str) -> dict[str, Callable[[], object]]:
    return {
        "exchange": lambda: fake.exchange(endpoint=endpoint),
        "refresh": lambda: fake.refresh(stored(REFRESH), endpoint=endpoint),
    }


@pytest.mark.parametrize("op", ["exchange", "refresh"])
@pytest.mark.parametrize("address", sorted(NON_PUBLIC.values()))
def test_a_non_public_token_host_sends_nothing(
    fake: FakeServer, op: str, address: str
) -> None:
    fake.addresses["auth.example.com"] = address
    failure = failure_of(_calls(fake, TOKEN)[op])
    assert_content_free(failure, "endpoint_misconfigured")
    assert failure.terminal is False
    assert fake.requests == []


@pytest.mark.parametrize("op", ["exchange", "refresh"])
@pytest.mark.parametrize("address", sorted(NON_PUBLIC.values()))
def test_a_literal_non_public_token_endpoint_sends_nothing(
    fake: FakeServer, op: str, address: str
) -> None:
    host = f"[{address}]" if ":" in address else address
    failure = failure_of(_calls(fake, f"https://{host}/token")[op])
    assert_content_free(failure, "endpoint_misconfigured")
    assert fake.requests == []


@pytest.mark.parametrize("op", ["exchange", "refresh"])
def test_a_plain_http_token_endpoint_sends_nothing(fake: FakeServer, op: str) -> None:
    failure = failure_of(_calls(fake, "http://auth.example.com/token")[op])
    assert_content_free(failure, "endpoint_misconfigured")
    assert fake.requests == []


@pytest.mark.parametrize("address", sorted(NON_PUBLIC.values()))
def test_a_non_public_revocation_host_sends_nothing(
    fake: FakeServer, address: str
) -> None:
    ref = stored(REFRESH)
    fake.addresses["auth.example.com"] = address
    assert fake.revoke(ref) == Revocation("revocation_failed", False)
    host = f"[{address}]" if ":" in address else address
    assert fake.revoke(ref, endpoint=f"https://{host}/revoke") == Revocation(
        "revocation_failed", False
    )
    assert fake.requests == []


# --- timing (AC 35) ------------------------------------------------------------------


@pytest.mark.parametrize("op", ["exchange", "refresh"])
def test_a_trickling_token_response_ends_within_twenty_seconds(
    fake: FakeServer, op: str
) -> None:
    fake.token = lambda r: httpx.Response(200, stream=Trickle(fake.clock))
    failure = failure_of(lambda: fake.run(op))
    assert_content_free(failure, "unavailable")
    assert failure.terminal is False
    # Chunks arrive at 4, 8, 12 and 16 s; the first after the 15 s budget ends it.
    assert fake.clock.now == 16.0
    assert len(fake.requests) == 1


def test_a_trickling_revocation_response_ends_within_twenty_seconds(
    fake: FakeServer,
) -> None:
    ref = stored(REFRESH)
    fake.revocation = lambda r: httpx.Response(200, stream=Trickle(fake.clock))
    assert fake.revoke(ref) == Revocation("revocation_failed", False)
    assert fake.clock.now == 16.0
    assert len(fake.requests) == 1


def test_the_read_timeout_is_five_seconds_on_every_request(fake: FakeServer) -> None:
    ref, _ = fake.exchange()
    _, rotated = fake.refresh(ref)
    assert rotated is not None
    fake.revoke(rotated)
    assert len(fake.requests) == 3
    for request in fake.requests:
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 5.0,
            "write": 5.0,
            "pool": 5.0,
        }


# --- content-free failures (I1, AC 50 core half) ------------------------------------


FAILING: dict[str, tuple[Handler, str | None]] = {
    "400 json error": (
        _json(
            400,
            {
                "error": "invalid_request",
                "error_description": f"{ACCESS} {NEW_REFRESH} {SECRET} {ORG}",
            },
        ),
        None,
    ),
    "200 truncated json": (
        lambda r: httpx.Response(
            200,
            content=(
                f'{{"access_token":"{ACCESS}","refresh_token":"{NEW_REFRESH}",'
                f'"client_secret":"{SECRET}","org":"{ORG}"'
            ).encode(),
            headers={"content-type": "application/json"},
        ),
        None,
    ),
    "500 html": (
        lambda r: httpx.Response(
            500,
            content=f"<html>{ACCESS} {SECRET} {ORG}</html>".encode(),
            headers={"content-type": "text/html"},
        ),
        None,
    ),
    "unparseable header": (
        lambda r: httpx.Response(
            429,
            headers={"Retry-After": f"soon {ORG} {ACCESS} {SECRET}"},
            content=b"{}",
        ),
        "rate_limited",
    ),
}


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("op", ["exchange", "refresh"])
@pytest.mark.parametrize("case", sorted(FAILING))
def test_a_failing_token_response_carries_no_upstream_text(
    fake: FakeServer,
    root: Path,
    caplog: pytest.LogCaptureFixture,
    case: str,
    op: str,
    method: str,
) -> None:
    handler, code = FAILING[case]
    fake.token = handler
    client = secret_client(method)
    with caplog.at_level(logging.DEBUG):
        failure = failure_of(lambda: fake.run(op, client))
    assert_content_free(failure, code or FAILED[op], str(root))
    assert failure.__cause__ is None
    assert failure.__context__ is None
    assert failure.retry_after is None
    text = all_log_text(caplog)
    for marker in MARKERS:
        assert marker not in text


@pytest.mark.parametrize("case", sorted(FAILING))
def test_a_failing_revocation_response_carries_no_upstream_text(
    fake: FakeServer, caplog: pytest.LogCaptureFixture, case: str
) -> None:
    fake.revocation = FAILING[case][0]
    with caplog.at_level(logging.DEBUG):
        result = fake.revoke(stored(REFRESH), secret_client("client_secret_post"))
    # RFC 7009: a 200 is a revocation whatever its body says.
    expected = "revoked" if case == "200 truncated json" else "revocation_failed"
    assert result == Revocation(expected, False)
    text = all_log_text(caplog) + repr(result)
    for marker in MARKERS:
        assert marker not in text


def test_secret_store_failures_have_no_chain(
    fake: FakeServer, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = stored(REFRESH)
    gone = stored(REFRESH)
    file_for(root, gone).unlink()
    rejected = secret_client("client_secret_basic")
    assert rejected.client_secret_ref is not None
    file_for(root, rejected.client_secret_ref).unlink()
    failures = [
        failure_of(lambda: fake.refresh(gone)),
        failure_of(lambda: fake.exchange(rejected)),
        failure_of(lambda: fake.refresh(current, rejected)),
    ]
    _failing_create(monkeypatch, _write_errors(root)["refusal"])
    failures.append(failure_of(fake.exchange))
    failures.append(failure_of(lambda: fake.refresh(current)))
    assert [f.code for f in failures] == [
        "secret_unavailable",
        "client_rejected",
        "client_rejected",
        "secret_write_failed",
        "secret_write_failed",
    ]
    for failure in failures:
        assert failure.__cause__ is None
        assert failure.__context__ is None
        assert str(root) not in str(failure) and str(root) not in repr(failure)


# --- the access token holder ---------------------------------------------------------


def test_the_access_token_hides_its_bytes(fake: FakeServer) -> None:
    _, access = fake.exchange()
    for rendered in (repr(access), str(access), f"{access}", f"{access!r:>40}"):
        assert ACCESS not in rendered
    assert repr(access) == "AccessToken(<redacted>)"
    assert access.authorization_header() == f"Bearer {ACCESS}"
    assert not hasattr(access, "__dict__")


def test_the_access_token_refuses_every_serialisation(fake: FakeServer) -> None:
    _, access = fake.exchange()
    with pytest.raises(TypeError):
        pickle.dumps(access)
    with pytest.raises(TypeError):
        copy.copy(access)
    with pytest.raises(TypeError):
        copy.deepcopy(access)
    with pytest.raises(TypeError):
        json.dumps({"token": access})
    with pytest.raises(TypeError):
        hash(access)
    with pytest.raises(TypeError):

        class Leaky(AccessToken):  # type: ignore[misc]
            pass
