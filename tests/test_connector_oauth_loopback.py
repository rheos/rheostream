"""The connector OAuth client's loopback redirect mode: the redirect URI it builds,
the registration body and authorization URL that carry it, and the strict reader of
the address an owner pastes back.

No database: settings come from ``RHEO__`` environment variables, and registration
goes to a counting ``httpx.MockTransport``.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import httpx
import pytest
from rheo_core.connectors import oauth
from rheo_core.connectors.oauth import Discovered, OAuthFailure
from rheo_core.settings import resolve
from rheo_core.settings.schema import SettingsError

ENABLED_ENV = "RHEO__connectors__oauth_client__enabled"
REDIRECT_ENV = "RHEO__connectors__oauth_client__redirect"
PORT_ENV = "RHEO__connectors__oauth_client__loopback_port"
IDENTITY_PATH_ENV = "RHEO__routing__identity__path"
LOOPBACK = "http://127.0.0.1:33419/callback"
ENDPOINT = "https://register.example.com/register"
WS = UUID("0190f3a2-6b1e-7c11-8d2a-3f4b5c6d7e01")
CONN = UUID("0190f3a2-6b1e-7c11-8d2a-3f4b5c6d7e02")
STATE = "s" * 43
CODE = "code-value-0001"
DISCOVERED = Discovered(
    resource_url="https://mcp.example.com/mcp",
    issuer="https://auth.example.com",
    authorization_endpoint="https://auth.example.com/authorize",
    token_endpoint="https://auth.example.com/token",
    registration_endpoint=ENDPOINT,
)


@pytest.fixture
def loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "secrets").mkdir(mode=0o700)
    monkeypatch.setenv("RHEO_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv(ENABLED_ENV, "true")
    monkeypatch.setenv(REDIRECT_ENV, "loopback")
    monkeypatch.delenv(PORT_ENV, raising=False)
    monkeypatch.delenv(IDENTITY_PATH_ENV, raising=False)
    return tmp_path


def address(query: str = f"state={STATE}&code={CODE}", base: str = LOOPBACK) -> str:
    return f"{base}?{query}"


# --- the mode and the redirect URI ------------------------------------------------


def test_the_defaults_are_identity_mode_and_port_33419(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(REDIRECT_ENV, raising=False)
    monkeypatch.delenv(PORT_ENV, raising=False)
    settings = resolve()
    assert settings.get_str(oauth.REDIRECT_KEY) == "identity"
    assert settings.get_int(oauth.LOOPBACK_PORT_KEY) == 33419


def test_loopback_mode_redirects_to_the_exact_loopback_uri(loopback: Path) -> None:
    assert oauth.redirect_mode() == "loopback"
    assert oauth.enabled() and not oauth.callback_served()
    assert oauth.redirect_uri("leads") == LOOPBACK
    assert oauth.loopback_redirect_uri() == LOOPBACK


def test_the_operator_port_is_used(
    loopback: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PORT_ENV, "40123")
    assert oauth.redirect_uri("leads") == "http://127.0.0.1:40123/callback"


@pytest.mark.parametrize("port", ["1023", "65536", "0", "-1", "http"])
def test_a_port_outside_1024_to_65535_is_refused_by_the_settings(
    loopback: Path, monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    monkeypatch.setenv(PORT_ENV, port)
    with pytest.raises(SettingsError):
        resolve()


def test_an_unknown_redirect_mode_is_refused_by_the_settings(
    loopback: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REDIRECT_ENV, "anywhere")
    with pytest.raises(SettingsError):
        resolve()


def test_the_identity_path_does_not_gate_loopback_mode(
    loopback: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loopback URI reaches no route here, so a relocated identity path, which
    closes identity mode, leaves loopback mode on."""
    monkeypatch.setenv(IDENTITY_PATH_ENV, "/signin")
    assert oauth.redirect_mode() == "loopback"
    assert oauth.redirect_uri("leads") == LOOPBACK
    monkeypatch.setenv(REDIRECT_ENV, "identity")
    assert oauth.redirect_mode() is None
    assert not oauth.enabled()


def test_identity_mode_is_unchanged(
    loopback: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REDIRECT_ENV, "identity")
    assert oauth.redirect_mode() == "identity" and oauth.callback_served()
    assert urlsplit(oauth.redirect_uri("leads")).path == (
        "/auth/connector/leads/callback"
    )


def test_switched_off_loopback_mode_refuses_the_redirect_uri(
    loopback: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENABLED_ENV, "false")
    assert oauth.redirect_mode() is None
    with pytest.raises(OAuthFailure) as caught:
        oauth.redirect_uri("leads")
    assert str(caught.value) == "endpoint_misconfigured"


# --- the registration body and the authorization URL ----------------------------------


def test_registration_and_the_authorization_url_carry_the_loopback_uri(
    loopback: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201, json={"client_id": "client-0001", "redirect_uris": [LOOPBACK]}
        )

    def public(host: str) -> Sequence[str]:
        return ["2600::1"]

    client = oauth.register(
        ENDPOINT,
        oauth.redirect_uri("leads"),
        "Client name",
        workspace_id=WS,
        connection_id=CONN,
        resolver=public,
        transport=httpx.MockTransport(handle),
    )
    (request,) = requests
    assert json.loads(request.content)["redirect_uris"] == [LOOPBACK]
    assert client.redirect_uri == LOOPBACK
    started = oauth.new_authorization(
        DISCOVERED, client_id=client.client_id, redirect_uri=client.redirect_uri
    )
    query = parse_qs(urlsplit(started.authorization_url).query)
    assert query["redirect_uri"] == [LOOPBACK]
    assert started.authorization_url.startswith("https://auth.example.com/authorize?")


# --- the pasted address ---------------------------------------------------------------


def test_a_good_address_yields_exactly_the_four_parameters(loopback: Path) -> None:
    query = urlencode(
        {
            "code": CODE,
            "state": STATE,
            "iss": "https://auth.example.com",
            "error_description": "ignored",
            "other": "ignored",
        }
    )
    assert oauth.loopback_callback_parameters(address(query)) == {
        "code": CODE,
        "state": STATE,
        "iss": "https://auth.example.com",
    }


def test_surrounding_whitespace_is_ignored(loopback: Path) -> None:
    assert oauth.loopback_callback_parameters(f"  {address()}\n") == {
        "state": STATE,
        "code": CODE,
    }


def test_a_provider_error_is_read(loopback: Path) -> None:
    pasted = address(f"error=access_denied&state={STATE}")
    assert oauth.loopback_callback_parameters(pasted) == {
        "error": "access_denied",
        "state": STATE,
    }


@pytest.mark.parametrize(
    "base",
    [
        "https://127.0.0.1:33419/callback",
        "HTTPS://127.0.0.1:33419/callback",
        "ftp://127.0.0.1:33419/callback",
        "http://localhost:33419/callback",
        "http://127.0.0.2:33419/callback",
        "http://[::1]:33419/callback",
        "http://127.1:33419/callback",
        "http://127.0.0.1:33420/callback",
        "http://127.0.0.1/callback",
        "http://127.0.0.1:033419/callback",
        "http://127.0.0.1:33419/callback/",
        "http://127.0.0.1:33419/Callback",
        "http://127.0.0.1:33419/auth/callback",
        "http://127.0.0.1:33419",
        "http://owner@127.0.0.1:33419/callback",
        "http://owner:pw@127.0.0.1:33419/callback",
        "//127.0.0.1:33419/callback",
        "127.0.0.1:33419/callback",
    ],
)
def test_any_other_scheme_host_port_path_or_userinfo_is_refused(
    loopback: Path, base: str
) -> None:
    assert oauth.loopback_callback_parameters(address(base=base)) is None


@pytest.mark.parametrize(
    "query",
    [
        f"state={STATE}&code={CODE}#frag",
        f"state={STATE}&code={CODE}#",
        f"state={STATE}&state={STATE}&code={CODE}",
        f"state={STATE}&code={CODE}&code={CODE}",
        f"state={STATE}&code={CODE}&iss=a&iss=a",
        f"state={STATE}&code={CODE}&error=x&error=y",
        f"code={CODE}",
        f"state={STATE}",
        f"state={STATE}&iss=https://auth.example.com",
        "",
        f"state={STATE}&code={'c' * 2049}",
        f"state={'s' * 2049}&code={CODE}",
    ],
    ids=[
        "fragment",
        "empty fragment",
        "duplicate state",
        "duplicate code",
        "duplicate iss",
        "duplicate error",
        "missing state",
        "missing code and error",
        "iss only",
        "no query",
        "oversized code",
        "oversized state",
    ],
)
def test_fragments_duplicates_missing_and_oversized_values_are_refused(
    loopback: Path, query: str
) -> None:
    assert oauth.loopback_callback_parameters(address(query)) is None


def test_a_value_at_the_bound_is_accepted(loopback: Path) -> None:
    code = "c" * oauth.MAX_CALLBACK_PARAMETER_LENGTH
    assert oauth.loopback_callback_parameters(address(f"state={STATE}&code={code}"))


def test_an_address_over_4096_characters_is_refused(loopback: Path) -> None:
    pasted = address(f"state={STATE}&code={CODE}&pad={'p' * 4096}")
    assert len(pasted) > oauth.MAX_PASTED_ADDRESS_LENGTH
    assert oauth.loopback_callback_parameters(pasted) is None
    shorter = address(f"state={STATE}&code={CODE}&pad=p")
    assert oauth.loopback_callback_parameters(shorter) is not None


def test_a_duplicate_of_an_ignored_parameter_is_ignored(loopback: Path) -> None:
    pasted = address(f"state={STATE}&code={CODE}&x=1&x=2")
    assert oauth.loopback_callback_parameters(pasted) == {"state": STATE, "code": CODE}


@pytest.mark.parametrize("value", [None, 7, b"bytes", ["list"]])
def test_a_non_string_is_refused(loopback: Path, value: object) -> None:
    assert oauth.loopback_callback_parameters(value) is None


def test_the_port_check_follows_the_operator_port(
    loopback: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PORT_ENV, "40123")
    assert oauth.loopback_callback_parameters(address()) is None
    moved = address(base="http://127.0.0.1:40123/callback")
    assert oauth.loopback_callback_parameters(moved) == {"state": STATE, "code": CODE}
