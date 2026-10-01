"""The OAuth connector surface's pure half (issue #287): AC-4's metadata document,
the protected-resource document, ``resource_matches`` and the unconfigured reasons.

Seams under test: ``rheo_core.oauth.oauth_surface`` and the documents and matcher it
returns, in both routing modes, from the shared routing fixtures. Expected URLs are
literals for the fixture hosts, so a change to the derivation cannot agree with
itself.
"""

import json
from pathlib import Path

import pytest
from rheo_core.oauth import OAuthSurface, OAuthUnconfigured, oauth_surface
from rheo_core.routing import MCP, RoutingConfig, url_for
from rheo_core.settings import ResolvedSettings, resolve
from rheo_core.settings.schema import freeze_value

FIXTURES = Path(__file__).parent / "fixtures" / "routing"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def _routing(mode: str) -> RoutingConfig:
    text = (FIXTURES / f"{mode}-mode.json").read_text(encoding="utf-8")
    return RoutingConfig.model_validate(json.loads(text))


def _settings(**overrides: object) -> ResolvedSettings:
    """Package defaults, the feature on and GitHub configured, then ``overrides``
    (keys spelled with ``__`` for ``.``)."""
    values = {key: freeze_value(value) for key, value in resolve().items()}
    values["identity.oauth.enabled"] = True
    values["identity.providers.github.enabled"] = True
    values["identity.providers.github.client_id"] = "example-client"
    for key, value in overrides.items():
        values[key.replace("__", ".")] = freeze_value(value)
    return ResolvedSettings(values)


def _surface(mode: str, **overrides: object) -> OAuthSurface:
    surface = oauth_surface(_settings(**overrides), _routing(mode))
    assert isinstance(surface, OAuthSurface), surface
    return surface


EXPECTED = {
    "subdomain": {
        "resource": "https://mcp.example.test/",
        "issuer": "https://mcp.example.test",
        "authorization_endpoint": "https://auth.example.test/auth/oauth/authorize",
        "token_endpoint": "https://auth.example.test/auth/oauth/token",
        "registration_endpoint": "https://auth.example.test/auth/oauth/register",
        "protected_resource_metadata_url": (
            "https://mcp.example.test/.well-known/oauth-protected-resource"
        ),
        "authorization_server_metadata_url": (
            "https://mcp.example.test/.well-known/oauth-authorization-server"
        ),
    },
    "path": {
        "resource": "https://example.test/mcp/",
        "issuer": "https://example.test/mcp",
        "authorization_endpoint": "https://example.test/auth/oauth/authorize",
        "token_endpoint": "https://example.test/auth/oauth/token",
        "registration_endpoint": "https://example.test/auth/oauth/register",
        "protected_resource_metadata_url": (
            "https://example.test/.well-known/oauth-protected-resource/mcp"
        ),
        "authorization_server_metadata_url": (
            "https://example.test/.well-known/oauth-authorization-server/mcp"
        ),
    },
}


@pytest.mark.parametrize("mode", ["subdomain", "path"])
def test_advertised_urls_follow_url_for(mode: str) -> None:
    surface = _surface(mode)
    for field, expected in EXPECTED[mode].items():
        assert getattr(surface, field) == expected, field
    # The canonical resource is url_for's own string, not a parallel spelling.
    assert surface.resource == url_for(_routing(mode), MCP, "/")


@pytest.mark.parametrize("mode", ["subdomain", "path"])
def test_authorization_server_metadata_field_set(mode: str) -> None:
    """AC-4: exactly the FR 4 fields with those values, compared as a set."""
    document = _surface(mode).authorization_server_metadata()
    expected = EXPECTED[mode]
    assert set(document) == {
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "response_types_supported",
        "grant_types_supported",
        "code_challenge_methods_supported",
        "token_endpoint_auth_methods_supported",
        "authorization_response_iss_parameter_supported",
    }
    assert document["issuer"] == expected["issuer"]
    assert document["authorization_endpoint"] == expected["authorization_endpoint"]
    assert document["token_endpoint"] == expected["token_endpoint"]
    assert document["registration_endpoint"] == expected["registration_endpoint"]
    assert document["response_types_supported"] == ["code"]
    assert set(document["grant_types_supported"]) == {  # type: ignore[call-overload]
        "authorization_code",
        "refresh_token",
    }
    assert document["code_challenge_methods_supported"] == ["S256"]
    assert document["token_endpoint_auth_methods_supported"] == ["none"]
    assert document["authorization_response_iss_parameter_supported"] is True
    assert "client_id_metadata_document_supported" not in document
    assert "scopes_supported" not in document


@pytest.mark.parametrize("mode", ["subdomain", "path"])
def test_protected_resource_metadata(mode: str) -> None:
    document = _surface(mode).protected_resource_metadata()
    assert document == {
        "resource": EXPECTED[mode]["resource"],
        "authorization_servers": [EXPECTED[mode]["issuer"]],
        "bearer_methods_supported": ["header"],
    }


RESOURCE_CASES = {
    "subdomain": [
        ("https://mcp.example.test/", True),
        ("https://mcp.example.test", True),
        ("https://mcp.example.test/mcp", False),
        ("https://mcp.example.test/mcp/", False),
        ("https://mcp.example.test//", False),
        ("https://MCP.example.test/", False),
        ("https://mcp.example.test/?x=1", False),
        ("http://mcp.example.test/", False),
        ("https://mcp.example.test:443/", False),
        ("", False),
    ],
    "path": [
        ("https://example.test/mcp/", True),
        ("https://example.test/mcp", False),
        ("https://example.test/mcp/x", False),
        ("https://example.test/MCP/", False),
        ("https://example.test/mcp/?x=1", False),
        ("https://example.test/", False),
        ("https://example.test", False),
    ],
}


@pytest.mark.parametrize(
    ("mode", "value", "accepted"),
    [
        (mode, value, ok)
        for mode, cases in RESOURCE_CASES.items()
        for value, ok in cases
    ],
)
def test_resource_matches(mode: str, value: str, accepted: bool) -> None:
    assert _surface(mode).resource_matches(value) is accepted


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"identity__oauth__enabled": False}, "disabled"),
        ({"identity__providers__github__enabled": False}, "identity_provider_disabled"),
        ({"identity__providers__github__client_id": ""}, "identity_provider_disabled"),
        ({"identity__oauth__redirect_uris": ()}, "allowlist_empty"),
        (
            {"identity__oauth__redirect_uris": ("http://claude.example.com/cb",)},
            "setting_invalid",
        ),
        ({"identity__oauth__access_token_minutes": 4}, "setting_invalid"),
        ({"identity__oauth__grant_days": 0}, "setting_invalid"),
        ({"identity__oauth__max_clients": 0}, "setting_invalid"),
        ({"identity__oauth__abandoned_client_minutes": 14}, "setting_invalid"),
        ({"identity__oauth__refresh_grace_seconds": -1}, "setting_invalid"),
    ],
)
@pytest.mark.parametrize("mode", ["subdomain", "path"])
def test_unconfigured_reasons(
    mode: str, overrides: dict[str, object], reason: str
) -> None:
    assert oauth_surface(_settings(**overrides), _routing(mode)) == OAuthUnconfigured(
        reason  # type: ignore[arg-type]
    )


def test_package_defaults_are_unconfigured() -> None:
    """Shipped dark: the package default is off."""
    assert oauth_surface(resolve(), _routing("subdomain")) == OAuthUnconfigured(
        "disabled"
    )


def test_defaults_carry_the_allowlist_and_lifetimes() -> None:
    surface = _surface("subdomain")
    assert surface.redirect_uris == (CLAUDE_CALLBACK,)
    lifetimes = surface.lifetimes
    assert (
        lifetimes.access_token_minutes,
        lifetimes.grant_days,
        lifetimes.refresh_grace_seconds,
        lifetimes.max_clients,
        lifetimes.registrations_per_source_per_hour,
        lifetimes.abandoned_client_minutes,
    ) == (60, 30, 10, 100, 30, 60)


def test_loopback_test_redirect_is_allowed() -> None:
    surface = _surface(
        "path", identity__oauth__redirect_uris=("http://127.0.0.1:8765/callback",)
    )
    assert surface.redirect_uris == ("http://127.0.0.1:8765/callback",)


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost:x@evil.example.com/cb",
        "http://localhost.evil.example.com/cb",
        "http://127.0.0.1.evil.example.com/cb",
        "http://user@localhost/cb",
        "https://user:pass@claude.example.com/cb",
        "https://claude.example.com@evil.example.com/cb",
        "https:///cb",
        "https://claude.example.com/cb#frag",
        "http://localhost:notaport/cb",
        "ftp://localhost/cb",
        "localhost/cb",
    ],
)
def test_lookalike_and_userinfo_redirects_are_setting_invalid(uri: str) -> None:
    settings = _settings(identity__oauth__redirect_uris=(uri,))
    assert oauth_surface(settings, _routing("path")) == OAuthUnconfigured(
        "setting_invalid"
    )


@pytest.mark.parametrize(
    "uri",
    [
        CLAUDE_CALLBACK,
        "http://localhost/callback",
        "http://localhost:8765/callback",
        "http://127.0.0.1/callback",
    ],
)
def test_valid_redirects_configure(uri: str) -> None:
    surface = _surface("path", identity__oauth__redirect_uris=(uri,))
    assert surface.redirect_uris == (uri,)


@pytest.mark.parametrize("mode", ["subdomain", "path"])
@pytest.mark.parametrize("base_host", ['ex"ample.test', "exämple.test"])
def test_a_header_unsafe_pointer_is_setting_invalid(mode: str, base_host: str) -> None:
    """A base host that would put a ``"`` or a non-ASCII character into the
    protected-resource URL (and so into the ``mcp`` gate's ``WWW-Authenticate``
    quoted string) leaves the surface unconfigured, so the gate stays bare."""
    text = (FIXTURES / f"{mode}-mode.json").read_text(encoding="utf-8")
    data = json.loads(text)
    data["base_host"] = base_host
    data["public_host"] = base_host
    routing = RoutingConfig.model_validate(data)
    assert oauth_surface(_settings(), routing) == OAuthUnconfigured("setting_invalid")
