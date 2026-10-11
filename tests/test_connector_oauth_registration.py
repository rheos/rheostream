"""Core's OAuth client: dynamic client registration, the secret namespace and client
authentication at the token endpoint.

A fake registration endpoint and a counting token endpoint sit behind an
``httpx.MockTransport``; host names go to an injected resolver and the budget runs on
a fake clock, so no test touches the network. Secrets land in a real file store under
a temporary data root.
"""

import base64
import inspect
import json
import logging
import os
import stat
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus
from uuid import UUID

import httpx
import pytest
from rheo_core.connectors import credentials, oauth
from rheo_core.connectors.oauth import (
    ClientAuth,
    OAuthFailure,
    RegisteredClient,
    apply_client_authentication,
    discard,
    new_reference,
    register,
)
from rheo_core.refs import uuid7
from rheo_core.routing import IDENTITY, RoutingConfig, url_for
from rheo_core.secrets import SecretRef, SecretRefusal, SecretStore, SecretValue
from rheo_core.settings import resolve

PUBLIC = "2600::1"
"""A global unicast address; no test resolves or contacts it."""
ENDPOINT = "https://register.example.com/register"
TOKEN_ENDPOINT = "https://token.example.com/token"
REDIRECT = "https://localhost/auth/connector/leads/callback"
CLIENT_NAME = "client-name-marker-6f1d0c2e"
WS = UUID("0190f3a2-6b1e-7c11-8d2a-3f4b5c6d7e01")
CONN = UUID("0190f3a2-6b1e-7c11-8d2a-3f4b5c6d7e02")
OTHER_CONN = UUID("0190f3a2-6b1e-7c11-8d2a-3f4b5c6d7e03")
SECRET = "client-secret-marker-5d1c7a9e-2b44-4f0e-9a31-7c2d18e6b0f4"
TOKEN = "registration-token-marker-8e3a1f62-0c7d-4b59-a4e2-91d5c3b7f806"
MARKER = "marker-4b9a2f10-upstream-text"
ENABLED_ENV = "RHEO__connectors__oauth_client__enabled"
IDENTITY_PATH_ENV = "RHEO__routing__identity__path"

NON_PUBLIC = {
    "loopback": "127.0.0.1",
    "rfc1918": "10.1.2.3",
    "link-local": "169.254.169.254",
    "unique-local": "fd00::5",
    "unspecified": "0.0.0.0",
    "reserved": "240.0.0.9",
    "documentation": "192.0.2.44",
    "multicast": "239.1.1.1",
    "nat64-private": "64:ff9b::10.1.2.3",
}


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


def accepted_document(**extra: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "client_id": "client-0001",
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
    }
    document.update(extra)
    return document


class FakeRegistration:
    """A counting registration endpoint that keeps every request it receives."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.requests: list[httpx.Request] = []
        self.addresses: dict[str, str] = {}
        self.respond: Callable[[httpx.Request], httpx.Response] = lambda r: (
            httpx.Response(201, json=accepted_document())
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)

    def resolve(self, host: str) -> Sequence[str]:
        return [self.addresses.get(host, PUBLIC)]

    def register(
        self,
        endpoint: str = ENDPOINT,
        *,
        connection_id: UUID = CONN,
        redirect_uri: str = REDIRECT,
    ) -> RegisteredClient:
        return register(
            endpoint,
            redirect_uri,
            CLIENT_NAME,
            workspace_id=WS,
            connection_id=connection_id,
            resolver=self.resolve,
            clock=self.clock,
            transport=httpx.MockTransport(self.handle),
        )

    def refused(self, code: str, endpoint: str = ENDPOINT) -> OAuthFailure:
        with pytest.raises(OAuthFailure) as caught:
            self.register(endpoint)
        assert_content_free(caught.value, code)
        return caught.value


def assert_content_free(exc: BaseException, code: str) -> None:
    assert str(exc) == code
    assert repr(exc) == code
    for marker in (MARKER, SECRET, TOKEN, CLIENT_NAME):
        assert marker not in str(exc) and marker not in repr(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "secrets").mkdir(mode=0o700)
    monkeypatch.setenv("RHEO_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv(ENABLED_ENV, "true")
    monkeypatch.delenv(IDENTITY_PATH_ENV, raising=False)
    return tmp_path


@pytest.fixture
def fake(root: Path) -> FakeRegistration:
    return FakeRegistration()


def oauth_dir(root: Path, connection_id: UUID = CONN) -> Path:
    return (
        root / "secrets" / "ws" / str(WS) / "connection" / str(connection_id) / "oauth"
    )


def stored_files(root: Path) -> list[Path]:
    secrets_dir = root / "secrets"
    return sorted(p for p in secrets_dir.rglob("*") if p.is_file())


def file_for(root: Path, ref: str) -> Path:
    return root / "secrets" / SecretRef.parse(ref).id


def all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(
        f"{r.name} {r.getMessage()} {r.exc_text or ''} {r.args!r}"
        for r in caplog.records
    )


# --- AC 7 (a): the exact request ---------------------------------------------------


def test_registration_is_one_json_post_of_exactly_five_fields(
    fake: FakeRegistration,
) -> None:
    fake.register()
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.method == "POST"
    assert str(request.url) == ENDPOINT
    assert request.headers["content-type"] == "application/json"
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "client_name": CLIENT_NAME,
    }


def test_the_body_is_identical_whatever_the_ambient_request(
    fake: FakeRegistration,
) -> None:
    parameters = set(inspect.signature(register).parameters)
    assert parameters == {
        "registration_endpoint",
        "redirect_uri",
        "client_name",
        "workspace_id",
        "connection_id",
        "resolver",
        "clock",
        "transport",
    }
    fake.register(connection_id=CONN)
    fake.register(connection_id=OTHER_CONN)
    assert fake.requests[0].content == fake.requests[1].content


def test_client_name_is_sent_in_no_other_request(fake: FakeRegistration) -> None:
    fake.respond = lambda r: httpx.Response(
        201,
        json=accepted_document(
            client_secret=SECRET, token_endpoint_auth_method="client_secret_post"
        ),
    )
    registered = fake.register()
    assert [CLIENT_NAME in r.content.decode() for r in fake.requests] == [True]
    for method in ("none", "client_secret_basic", "client_secret_post"):
        received = token_request(client_auth(registered, method))
        assert CLIENT_NAME not in received.content.decode()
        assert CLIENT_NAME not in str(received.headers)


# --- AC 7 (b): accepted responses ----------------------------------------------------


@pytest.mark.parametrize("status", [200, 201])
def test_a_valid_response_is_accepted_with_its_fields(
    fake: FakeRegistration, root: Path, status: int
) -> None:
    fake.respond = lambda r: httpx.Response(
        status, json=accepted_document(client_id="client-ok")
    )
    registered = fake.register()
    assert registered == RegisteredClient(
        client_id="client-ok",
        auth_method="none",
        redirect_uri=REDIRECT,
        secret_expires_at=None,
        client_secret_ref=None,
        registration_token_ref=None,
    )
    assert stored_files(root) == []


def test_echoes_are_optional_and_a_255_character_client_id_is_accepted(
    fake: FakeRegistration,
) -> None:
    longest = "c" * 255
    fake.respond = lambda r: httpx.Response(201, json={"client_id": longest})
    assert fake.register().client_id == longest


def test_echoed_grants_may_include_more_than_the_two_requested(
    fake: FakeRegistration,
) -> None:
    fake.respond = lambda r: httpx.Response(
        201,
        json=accepted_document(
            grant_types=["refresh_token", "client_credentials", "authorization_code"]
        ),
    )
    assert fake.register().client_id == "client-0001"


# --- AC 7 (c): refusals store nothing ------------------------------------------------


def _json(status: int, document: object) -> Callable[[httpx.Request], httpx.Response]:
    return lambda r: httpx.Response(status, json=document)


def _raw(status: int, body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    return lambda r: httpx.Response(
        status, content=body, headers={"Content-Type": "application/json"}
    )


REFUSALS: dict[str, tuple[Callable[[httpx.Request], httpx.Response], str]] = {
    "missing client_id": (
        _json(201, {"redirect_uris": [REDIRECT]}),
        "registration_failed",
    ),
    "empty client_id": (
        _json(201, accepted_document(client_id="")),
        "registration_failed",
    ),
    "256-character client_id": (
        _json(201, accepted_document(client_id="c" * 256)),
        "registration_failed",
    ),
    "control character in client_id": (
        _json(201, accepted_document(client_id="client\x01id")),
        "registration_failed",
    ),
    "DEL in client_id": (
        _json(201, accepted_document(client_id="client\x7fid")),
        "registration_failed",
    ),
    "non-ASCII client_id": (
        _json(201, accepted_document(client_id="client-é")),
        "registration_failed",
    ),
    "numeric client_id": (
        _json(201, accepted_document(client_id=7)),
        "registration_failed",
    ),
    "other redirect URI echoed": (
        _json(
            201,
            accepted_document(redirect_uris=["https://elsewhere.example.com/callback"]),
        ),
        "registration_failed",
    ),
    "extra redirect URI echoed": (
        _json(
            201,
            accepted_document(
                redirect_uris=[REDIRECT, "https://elsewhere.example.com/callback"]
            ),
        ),
        "registration_failed",
    ),
    "redirect URI echoed as a string": (
        _json(201, accepted_document(redirect_uris=REDIRECT)),
        "registration_failed",
    ),
    "refresh grant missing from the echo": (
        _json(201, accepted_document(grant_types=["authorization_code"])),
        "registration_failed",
    ),
    "code grant missing from the echo": (
        _json(201, accepted_document(grant_types=["refresh_token"])),
        "registration_failed",
    ),
    "400": (_json(400, {"error": "invalid_client_metadata"}), "registration_failed"),
    "500": (_json(500, accepted_document()), "registration_failed"),
    "202": (_json(202, accepted_document()), "registration_failed"),
    "oversized body": (
        _raw(201, json.dumps(accepted_document(pad="x" * (16 * 1024))).encode()),
        "registration_failed",
    ),
    "malformed JSON": (
        _raw(201, b'{"client_id": "client-0001"'),
        "registration_failed",
    ),
    "JSON array": (_json(201, [accepted_document()]), "registration_failed"),
    "non-string client secret": (
        _json(201, accepted_document(client_secret=12345)),
        "registration_failed",
    ),
    "negative secret expiry": (
        _json(
            201,
            accepted_document(client_secret=SECRET, client_secret_expires_at=-1),
        ),
        "registration_failed",
    ),
    "private_key_jwt": (
        _json(201, accepted_document(token_endpoint_auth_method="private_key_jwt")),
        "registration_unsupported",
    ),
    "secret_basic without a secret": (
        _json(201, accepted_document(token_endpoint_auth_method="client_secret_basic")),
        "registration_unsupported",
    ),
    "secret_post without a secret": (
        _json(201, accepted_document(token_endpoint_auth_method="client_secret_post")),
        "registration_unsupported",
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_each_refusal_stores_nothing(
    fake: FakeRegistration, root: Path, case: str
) -> None:
    respond, code = REFUSALS[case]
    fake.respond = respond
    fake.refused(code)
    assert len(fake.requests) == 1
    assert not oauth_dir(root).exists() or list(oauth_dir(root).rglob("*")) == []
    assert stored_files(root) == []


def test_a_refusal_with_issued_secrets_stores_neither(
    fake: FakeRegistration, root: Path
) -> None:
    fake.respond = _json(
        201,
        accepted_document(
            client_secret=SECRET,
            registration_access_token=TOKEN,
            token_endpoint_auth_method="private_key_jwt",
        ),
    )
    fake.refused("registration_unsupported")
    assert stored_files(root) == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_misconfigured_and_nothing_follows_it(
    fake: FakeRegistration, root: Path, status: int
) -> None:
    fake.respond = lambda r: httpx.Response(
        status,
        headers={"Location": "https://elsewhere.example.com/register"},
        json=accepted_document(client_secret=SECRET),
    )
    fake.refused("endpoint_misconfigured")
    assert [r.url.host for r in fake.requests] == ["register.example.com"]
    assert stored_files(root) == []


def test_a_transport_error_is_registration_failed(fake: FakeRegistration) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(MARKER)

    fake.respond = broken
    fake.refused("registration_failed")
    assert len(fake.requests) == 1


def test_a_non_https_endpoint_is_misconfigured_with_zero_requests(
    fake: FakeRegistration,
) -> None:
    fake.refused("endpoint_misconfigured", "http://register.example.com/register")
    assert fake.requests == []


# --- AC 7 (d): the auth method and the secret expiry --------------------------------


@pytest.mark.parametrize(
    ("extra", "method"),
    [
        (
            {
                "client_secret": SECRET,
                "token_endpoint_auth_method": "client_secret_post",
            },
            "client_secret_post",
        ),
        (
            {
                "client_secret": SECRET,
                "token_endpoint_auth_method": "client_secret_basic",
            },
            "client_secret_basic",
        ),
        ({"token_endpoint_auth_method": "none"}, "none"),
        ({}, "none"),
        ({"client_secret": SECRET}, "client_secret_basic"),
        ({"client_secret": SECRET, "token_endpoint_auth_method": "none"}, "none"),
    ],
)
def test_the_auth_method_is_selected_per_the_response(
    fake: FakeRegistration, extra: dict[str, Any], method: str
) -> None:
    fake.respond = _json(201, accepted_document(**extra))
    assert fake.register().auth_method == method


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"client_secret_expires_at": 1893456000}, datetime(2030, 1, 1, tzinfo=UTC)),
        ({"client_secret_expires_at": 0}, None),
        ({}, None),
    ],
)
def test_the_secret_expiry_is_stored_only_when_non_zero(
    fake: FakeRegistration, extra: dict[str, Any], expected: datetime | None
) -> None:
    fake.respond = _json(201, accepted_document(client_secret=SECRET, **extra))
    assert fake.register().secret_expires_at == expected


# --- AC 7 (e): secret files ----------------------------------------------------------


def test_issued_secrets_land_as_private_files_with_only_references_returned(
    fake: FakeRegistration, root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake.respond = _json(
        201,
        accepted_document(
            client_secret=SECRET,
            registration_access_token=TOKEN,
            token_endpoint_auth_method="client_secret_post",
            client_secret_expires_at=1893456000,
        ),
    )
    registered = fake.register()
    prefix = f"secret://file/ws/{WS}/connection/{CONN}/oauth/"
    assert registered.client_secret_ref is not None
    assert registered.registration_token_ref is not None
    assert registered.client_secret_ref.startswith(prefix)
    assert registered.registration_token_ref.startswith(prefix)
    assert registered.client_secret_ref != registered.registration_token_ref
    secret_file = file_for(root, registered.client_secret_ref)
    token_file = file_for(root, registered.registration_token_ref)
    assert secret_file.read_text() == SECRET
    assert token_file.read_text() == TOKEN
    for path in (secret_file, token_file):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stored_files(root) == sorted([secret_file, token_file])
    for text in (repr(registered), str(registered)):
        for hidden in (
            SECRET,
            TOKEN,
            registered.client_secret_ref,
            registered.registration_token_ref,
        ):
            assert hidden not in text
    for hidden in (SECRET, TOKEN):
        assert hidden not in all_log_text(caplog)


def test_a_secret_issued_with_method_none_is_stored(
    fake: FakeRegistration, root: Path
) -> None:
    fake.respond = _json(
        201, accepted_document(client_secret=SECRET, token_endpoint_auth_method="none")
    )
    registered = fake.register()
    assert registered.auth_method == "none"
    assert registered.client_secret_ref is not None
    assert file_for(root, registered.client_secret_ref).read_text() == SECRET


def _failing_create(
    monkeypatch: pytest.MonkeyPatch, fail_on: int, error: Exception
) -> list[int]:
    real = SecretStore.create
    calls: list[int] = []

    def create(
        self: SecretStore, ref: SecretRef, value: SecretValue, scope: Any
    ) -> None:
        calls.append(1)
        if len(calls) == fail_on:
            raise error
        real(self, ref, value, scope)

    monkeypatch.setattr(SecretStore, "create", create)
    return calls


@pytest.mark.parametrize("fail_on", [1, 2])
@pytest.mark.parametrize("kind", ["refusal", "oserror"])
def test_a_failed_secret_write_refuses_and_leaves_no_file(
    fake: FakeRegistration,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_on: int,
    kind: str,
) -> None:
    path_text = str(root / "secrets" / "ws")
    error: Exception = (
        SecretRefusal("secret_write_failed", f"could not write {path_text}")
        if kind == "refusal"
        else OSError(28, f"No space left on device: {path_text}")
    )
    calls = _failing_create(monkeypatch, fail_on, error)
    fake.respond = _json(
        201,
        accepted_document(
            client_secret=SECRET,
            registration_access_token=TOKEN,
            token_endpoint_auth_method="client_secret_basic",
        ),
    )
    failure = fake.refused("registration_failed")
    assert len(calls) == fail_on
    assert path_text not in str(failure) and path_text not in repr(failure)
    assert stored_files(root) == []


# --- discard: one connection's oauth/ namespace only ---------------------------------


def _create_oauth_file(connection_id: UUID = CONN, value: bytes = b"value") -> str:
    ref = new_reference(WS, connection_id)
    store = SecretStore(Path(os.environ["RHEO_DATA_ROOT"]))
    writable = SecretStore.scope_for(
        "test-setup", f"secret://file/ws/{WS}/connection/", writable=True
    )
    store.create(SecretRef.parse(ref), SecretValue(value), writable)
    return ref


def _create_signing_key() -> str:
    """A webhook signing key in the same connection, created the way
    ``credentials.create_key`` creates one."""
    namespace = credentials.prefix(WS, CONN)
    ref = SecretRef.parse(namespace + str(uuid7()))
    store = SecretStore(Path(os.environ["RHEO_DATA_ROOT"]))
    scope = store.scope_for("connector-provisioning", namespace, writable=True)
    store.create(ref, SecretValue(b"signing-key-marker"), scope)
    return str(ref)


class DiscardSpy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[SecretRef, Any]] = []
        real = SecretStore.discard

        def spy(store: SecretStore, ref: SecretRef, scope: Any) -> None:
            self.calls.append((ref, scope))
            real(store, ref, scope)

        monkeypatch.setattr(SecretStore, "discard", spy)


def test_new_reference_is_a_fresh_leaf_in_the_connection_namespace() -> None:
    first, second = new_reference(WS, CONN), new_reference(WS, CONN)
    prefix = f"secret://file/ws/{WS}/connection/{CONN}/oauth/"
    assert first.startswith(prefix) and second.startswith(prefix)
    assert first != second
    UUID(first.removeprefix(prefix))


def test_discard_removes_only_the_named_file(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _create_oauth_file()
    kept = _create_oauth_file()
    signing = _create_signing_key()
    spy = DiscardSpy(monkeypatch)
    discard(target)
    assert not file_for(root, target).exists()
    assert file_for(root, kept).exists()
    assert file_for(root, signing).exists()
    assert len(spy.calls) == 1


def test_the_scope_discard_builds_covers_exactly_one_connection_namespace(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _create_oauth_file()
    sibling = _create_oauth_file(OTHER_CONN)
    signing = _create_signing_key()
    spy = DiscardSpy(monkeypatch)
    discard(target)
    [(ref, scope)] = spy.calls
    assert str(ref) == target
    assert scope.writable
    assert scope.prefixes == (f"secret://file/ws/{WS}/connection/{CONN}/oauth/",)
    assert scope.permits(SecretRef.parse(target))
    for outside in (
        sibling,
        signing,
        f"secret://file/ws/{WS}/connection/{CONN}/other",
        f"secret://file/ws/{WS}/settings",
    ):
        assert not scope.permits(SecretRef.parse(outside))
    # Positive control: a scope widened to the workspace would reach the signing key,
    # so the refusal above is what keeps it out of reach.
    wide = SecretStore.scope_for("test-wide", f"secret://file/ws/{WS}/", writable=True)
    assert wide.permits(SecretRef.parse(signing))


def _refused_references(root: Path) -> dict[str, str]:
    signing = _create_signing_key()
    sibling = _create_oauth_file(OTHER_CONN)
    return {
        "signing key in the same connection": signing,
        "another connection's oauth leaf with an extra segment": f"{sibling}/extra",
        "bare namespace": f"secret://file/ws/{WS}/connection/{CONN}/oauth/",
        "environment reference": "secret://env/RHEO_CONNECTOR_SECRET",
        "workspace-level reference": f"secret://file/ws/{WS}/{uuid7()}",
        "uppercase leaf": new_reference(WS, CONN).upper(),
        "trailing newline": new_reference(WS, CONN) + "\n",
        "not a string": None,  # type: ignore[dict-item]
    }


def test_discard_refuses_every_other_shape_without_touching_the_store(
    root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    references = _refused_references(root)
    before = stored_files(root)
    spy = DiscardSpy(monkeypatch)
    for name, ref in references.items():
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            discard(ref)  # type: ignore[arg-type]
        messages = [r.getMessage() for r in caplog.records]
        assert messages == ["secret_discard_refused"], name
        if isinstance(ref, str):
            assert ref not in all_log_text(caplog)
    assert spy.calls == []
    assert stored_files(root) == before


def test_a_discard_failure_is_swallowed_and_logged_content_free(
    root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = _create_oauth_file()
    oauth_dir(root).chmod(0o755)
    try:
        with caplog.at_level(logging.DEBUG):
            discard(target)
    finally:
        oauth_dir(root).chmod(0o700)
    assert file_for(root, target).exists()
    assert [r.getMessage() for r in caplog.records] == ["secret_discard_failed"]
    text = all_log_text(caplog)
    assert target not in text and str(root) not in text


# --- the operator switch and the redirect URI -----------------------------------------


def test_enabled_and_redirect_uri_follow_url_for(root: Path) -> None:
    assert oauth.enabled() is True
    expected = url_for(
        RoutingConfig.from_settings(resolve()), IDENTITY, "/connector/leads/callback"
    )
    assert oauth.redirect_uri("leads") == expected == REDIRECT
    assert list(inspect.signature(oauth.redirect_uri).parameters) == ["module_id"]


@pytest.mark.parametrize(
    "module_id", ["", "Leads", "leads/../x", "leads?x=1", "a" * 40]
)
def test_redirect_uri_refuses_a_malformed_module_id(root: Path, module_id: str) -> None:
    with pytest.raises(OAuthFailure) as caught:
        oauth.redirect_uri(module_id)
    assert_content_free(caught.value, "endpoint_misconfigured")


@pytest.mark.parametrize(
    "env",
    [{ENABLED_ENV: "false"}, {IDENTITY_PATH_ENV: "/login"}],
    ids=["switch off", "identity path not served"],
)
def test_unconfigured_refuses_register_and_redirect_uri_with_zero_requests(
    fake: FakeRegistration,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert oauth.enabled() is False
    with pytest.raises(OAuthFailure) as caught:
        oauth.redirect_uri("leads")
    assert_content_free(caught.value, "endpoint_misconfigured")
    fake.refused("endpoint_misconfigured")
    assert fake.requests == []
    assert stored_files(root) == []


def test_the_switch_is_off_by_default(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENABLED_ENV)
    assert oauth.enabled() is False


# --- AC 72 for registration: public addresses only ------------------------------------


@pytest.mark.parametrize("address", sorted(NON_PUBLIC.values()))
def test_a_non_public_endpoint_host_is_refused_with_zero_requests(
    fake: FakeRegistration, root: Path, address: str
) -> None:
    fake.addresses["register.example.com"] = address
    fake.refused("endpoint_misconfigured")
    assert fake.requests == []
    assert stored_files(root) == []


@pytest.mark.parametrize("address", sorted(NON_PUBLIC.values()))
def test_a_literal_non_public_endpoint_is_refused_with_zero_requests(
    fake: FakeRegistration, address: str
) -> None:
    host = f"[{address}]" if ":" in address else address
    fake.refused("endpoint_misconfigured", f"https://{host}/register")
    assert fake.requests == []


# --- AC 35 for registration: timing ---------------------------------------------------


def test_a_trickling_registration_response_ends_within_twenty_seconds(
    fake: FakeRegistration, root: Path
) -> None:
    clock = fake.clock
    fake.respond = lambda r: httpx.Response(201, stream=Trickle(clock))
    fake.refused("registration_failed")
    # Chunks arrive at 4, 8, 12 and 16 s; the first after the 15 s budget ends it.
    assert clock.now == 16.0
    assert clock.now <= 20.0
    assert len(fake.requests) == 1
    assert stored_files(root) == []


def test_the_registration_read_timeout_is_five_seconds(fake: FakeRegistration) -> None:
    fake.register()
    assert fake.requests[0].extensions["timeout"] == {
        "connect": 5.0,
        "read": 5.0,
        "write": 5.0,
        "pool": 5.0,
    }


# --- client authentication: what the token endpoint receives (AC 80) ------------------


def client_auth(registered: RegisteredClient, method: str) -> ClientAuth:
    return ClientAuth(
        client_id=registered.client_id,
        auth_method=method,  # type: ignore[arg-type]
        client_secret_ref=registered.client_secret_ref,
        secret_expires_at=registered.secret_expires_at,
    )


def token_request(client: ClientAuth, *, now: datetime | None = None) -> httpx.Request:
    """Build a token request with ``client``'s authentication and send it to a
    counting fake token endpoint; return what that endpoint received."""
    received: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json={})

    form = {"grant_type": "refresh_token", "refresh_token": "refresh-marker"}
    headers: dict[str, str] = {}
    apply_client_authentication(
        client, form, headers, now=now or datetime(2026, 10, 10, tzinfo=UTC)
    )
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        http.post(TOKEN_ENDPOINT, data=form, headers=headers)
    [request] = received
    return request


def registered_with_secret(fake: FakeRegistration, **extra: Any) -> RegisteredClient:
    fake.respond = _json(
        201,
        accepted_document(
            client_id="client id:1",
            client_secret=SECRET,
            registration_access_token=TOKEN,
            **extra,
        ),
    )
    return fake.register()


def form_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


def test_method_none_sends_the_client_id_in_the_body_and_no_secret(
    fake: FakeRegistration,
) -> None:
    registered = registered_with_secret(fake, token_endpoint_auth_method="none")
    received = token_request(client_auth(registered, "none"))
    assert form_of(received)["client_id"] == ["client id:1"]
    assert "client_secret" not in form_of(received)
    assert "authorization" not in received.headers
    assert SECRET not in received.content.decode() + str(received.headers)


def test_secret_basic_sends_http_basic_and_no_secret_in_the_body(
    fake: FakeRegistration,
) -> None:
    registered = registered_with_secret(fake)
    assert registered.auth_method == "client_secret_basic"
    received = token_request(client_auth(registered, "client_secret_basic"))
    pair = f"{quote_plus('client id:1')}:{quote_plus(SECRET)}"
    expected = base64.b64encode(pair.encode()).decode()
    assert received.headers["authorization"] == f"Basic {expected}"
    assert SECRET not in received.content.decode()
    assert "client_secret" not in form_of(received)


def test_secret_basic_form_urlencodes_both_halves(
    fake: FakeRegistration,
) -> None:
    awkward = "s3cret+/:= marker"
    fake.respond = _json(
        201, accepted_document(client_id="id:with space", client_secret=awkward)
    )
    received = token_request(client_auth(fake.register(), "client_secret_basic"))
    scheme, encoded = received.headers["authorization"].split(" ")
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == (
        "id%3Awith+space:s3cret%2B%2F%3A%3D+marker"
    )


def test_secret_post_sends_both_in_the_body(fake: FakeRegistration) -> None:
    registered = registered_with_secret(
        fake, token_endpoint_auth_method="client_secret_post"
    )
    received = token_request(client_auth(registered, "client_secret_post"))
    assert form_of(received)["client_id"] == ["client id:1"]
    assert form_of(received)["client_secret"] == [SECRET]
    assert "authorization" not in received.headers


@pytest.mark.parametrize(
    "method", ["none", "client_secret_basic", "client_secret_post"]
)
def test_the_registration_access_token_is_never_sent(
    fake: FakeRegistration, method: str
) -> None:
    registered = registered_with_secret(fake)
    received = token_request(client_auth(registered, method))
    assert TOKEN not in received.content.decode()
    assert TOKEN not in str(received.headers)
    for value in received.headers.values():
        if value.startswith("Basic "):
            assert TOKEN not in base64.b64decode(value[6:]).decode()


class ResolveSpy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real = SecretStore.resolve

        def spy(store: SecretStore, ref: SecretRef, scope: Any) -> SecretValue:
            self.calls += 1
            return real(store, ref, scope)

        monkeypatch.setattr(SecretStore, "resolve", spy)


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
def test_an_expired_secret_is_rejected_before_it_is_read(
    fake: FakeRegistration, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    registered = registered_with_secret(fake, token_endpoint_auth_method=method)
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    spy = ResolveSpy(monkeypatch)
    for expired_at in (now - timedelta(seconds=1), now):
        client = ClientAuth(
            client_id=registered.client_id,
            auth_method=registered.auth_method,
            client_secret_ref=registered.client_secret_ref,
            secret_expires_at=expired_at,
        )
        form: dict[str, str] = {}
        headers: dict[str, str] = {}
        with pytest.raises(OAuthFailure) as caught:
            apply_client_authentication(client, form, headers, now=now)
        assert_content_free(caught.value, "client_rejected")
        assert caught.value.terminal is True
        assert form == {} and headers == {}
    assert spy.calls == 0
    # The same client with an expiry still ahead authenticates.
    future = ClientAuth(
        client_id=registered.client_id,
        auth_method=registered.auth_method,
        client_secret_ref=registered.client_secret_ref,
        secret_expires_at=now + timedelta(seconds=1),
    )
    received = token_request(future, now=now)
    assert spy.calls == 1
    assert SECRET in (
        received.content.decode()
        + base64.b64decode(
            received.headers.get("authorization", "Basic ").split(" ")[1] or ""
        ).decode()
    )


def test_an_expiry_does_not_block_method_none(fake: FakeRegistration) -> None:
    registered = registered_with_secret(fake, token_endpoint_auth_method="none")
    client = ClientAuth(
        client_id=registered.client_id,
        auth_method="none",
        client_secret_ref=registered.client_secret_ref,
        secret_expires_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    received = token_request(client)
    assert form_of(received)["client_id"] == ["client id:1"]


def _unreadable(root: Path, ref: str, how: str) -> None:
    path = file_for(root, ref)
    if how == "missing":
        path.unlink()
    else:
        path.chmod(0o644)


@pytest.mark.parametrize("how", ["missing", "mode 0644"])
@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
def test_an_unreadable_secret_is_client_rejected_with_nothing_sent(
    fake: FakeRegistration, root: Path, method: str, how: str
) -> None:
    registered = registered_with_secret(fake, token_endpoint_auth_method=method)
    assert registered.client_secret_ref is not None
    _unreadable(root, registered.client_secret_ref, how)
    form: dict[str, str] = {}
    headers: dict[str, str] = {}
    with pytest.raises(OAuthFailure) as caught:
        apply_client_authentication(
            client_auth(registered, method),
            form,
            headers,
            now=datetime(2026, 10, 10, tzinfo=UTC),
        )
    failure = caught.value
    assert_content_free(failure, "client_rejected")
    assert failure.terminal is True
    assert form == {} and headers == {}
    for hidden in (registered.client_secret_ref, str(root)):
        assert hidden not in str(failure) and hidden not in repr(failure)


def test_a_secret_method_without_a_reference_is_client_rejected() -> None:
    client = ClientAuth(
        client_id="client-0001",
        auth_method="client_secret_post",
        client_secret_ref=None,
        secret_expires_at=None,
    )
    with pytest.raises(OAuthFailure) as caught:
        apply_client_authentication(
            client, {}, {}, now=datetime(2026, 10, 10, tzinfo=UTC)
        )
    assert_content_free(caught.value, "client_rejected")
    assert caught.value.terminal is True


def test_client_auth_repr_hides_the_reference(fake: FakeRegistration) -> None:
    registered = registered_with_secret(fake)
    client = client_auth(registered, "client_secret_basic")
    assert registered.client_secret_ref is not None
    assert registered.client_secret_ref not in repr(client)
    assert SECRET not in repr(client)


# --- I1: content-free failures -------------------------------------------------------


@pytest.mark.parametrize(
    "respond",
    [
        lambda r: httpx.Response(
            400,
            headers={"X-Detail": MARKER},
            json={"error": "invalid_redirect_uri", "error_description": MARKER},
        ),
        lambda r: httpx.Response(
            201,
            headers={"Content-Type": "application/json", "X-Detail": MARKER},
            content=f'{{"client_id": "{MARKER}", "client_secret": "{SECRET}'.encode(),
        ),
        lambda r: httpx.Response(
            201, json=accepted_document(client_id=f"{MARKER}\x00")
        ),
    ],
    ids=["error body and header", "truncated JSON", "invalid client id"],
)
def test_a_failing_registration_carries_no_upstream_text(
    fake: FakeRegistration,
    caplog: pytest.LogCaptureFixture,
    respond: Callable[[httpx.Request], httpx.Response],
) -> None:
    caplog.set_level(logging.DEBUG)
    fake.respond = respond
    failure = fake.refused("registration_failed")
    assert failure.__cause__ is None
    assert failure.__context__ is None
    assert MARKER not in all_log_text(caplog)


def test_a_missing_secret_failure_has_no_chain(
    fake: FakeRegistration, root: Path
) -> None:
    registered = registered_with_secret(fake)
    assert registered.client_secret_ref is not None
    file_for(root, registered.client_secret_ref).unlink()
    with pytest.raises(OAuthFailure) as caught:
        token_request(client_auth(registered, "client_secret_basic"))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_issued_secrets_stay_out_of_every_log_record_and_exception(
    fake: FakeRegistration, root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    registered = registered_with_secret(
        fake, token_endpoint_auth_method="client_secret_post"
    )
    token_request(client_auth(registered, "client_secret_post"))
    assert registered.client_secret_ref is not None
    file_for(root, registered.client_secret_ref).unlink()
    with pytest.raises(OAuthFailure) as caught:
        token_request(client_auth(registered, "client_secret_post"))
    text = all_log_text(caplog)
    for hidden in (SECRET, TOKEN):
        assert hidden not in text
        assert hidden not in str(caught.value) and hidden not in repr(caught.value)
