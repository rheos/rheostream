"""The ``mcp`` surface's pieces that need no database (issue #127).

The allow-list the mount derives from the routing configuration, the rule that
decides which requests are the surface, the transport's refusal to run without
DNS-rebinding protection, and ``approval_id`` as a structured field on both the
HTTP envelope and the MCP tool result. The end-to-end half, through the core app
with its lifespan, is ``tests/postgres/test_mcp_mount.py``.
"""

import json
import uuid
from types import SimpleNamespace
from typing import Any

import mcp.types as types
import pytest
import rheo_app_mcp.transport as transport
from mcp.server.transport_security import TransportSecuritySettings
from rheo_app_core.api_routes import envelope
from rheo_app_core.auth_routes import normalize_host
from rheo_app_core.mcp_mount import (
    LOOPBACK_WILDCARD_HOSTS,
    McpMount,
    mcp_hosts,
    mcp_origins,
)
from rheo_app_mcp.transport import (
    MCP_PATH,
    build_mcp_app,
    transport_security_for,
)
from rheo_core.operations import OperationOutcome
from rheo_core.operations.dispatch import OperationError
from rheo_core.routing import RoutingConfig

APPROVAL_REQUIRED = "approval_required"


def _config(
    mode: str,
    *,
    public_host: str = "",
    mcp_path: str = "/mcp",
    mcp_host: str = "mcp",
    base_host: str = "example.test",
    scheme: str = "https",
    modules: dict[str, Any] | None = None,
) -> Any:
    surfaces = {
        "shell": {"host": "circuit", "path": "/"},
        "identity": {"host": "auth", "path": "/auth", "fixed_path": True},
        "api": {"host": "api", "path": "/api"},
        "mcp": {"host": mcp_host, "path": mcp_path},
        "docs": {"host": "docs", "external": True},
        "integration": {"host": "tuttle", "external": True, "reserved": True},
        "modules": modules or {},
    }
    return RoutingConfig.model_validate(
        {
            "mode": mode,
            "scheme": scheme,
            "base_host": base_host,
            "public_host": public_host,
            "surfaces": surfaces,
        }
    )


# --- the allow-list -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "public_host", "expected"),
    [
        ("path", "", ("example.test",)),
        ("path", "example.test:8443", ("example.test", "example.test:8443")),
        ("subdomain", "", ("mcp.example.test",)),
        (
            "subdomain",
            "example.test:8443",
            ("mcp.example.test", "mcp.example.test:8443"),
        ),
    ],
)
def test_mcp_hosts_are_exactly_the_surface_hosts(
    mode: str, public_host: str, expected: tuple[str, ...]
) -> None:
    assert mcp_hosts(_config(mode, public_host=public_host)) == expected


def test_a_real_name_gets_exact_hosts_and_origins_and_no_wildcard() -> None:
    config = _config("subdomain", public_host="example.test:8443")
    settings = transport_security_for(mcp_hosts(config), origins=mcp_origins(config))
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["mcp.example.test", "mcp.example.test:8443"]
    assert settings.allowed_origins == [
        "https://mcp.example.test",
        "https://mcp.example.test:8443",
    ]
    assert not any(host.endswith(":*") for host in settings.allowed_hosts)
    assert not any(origin.endswith(":*") for origin in settings.allowed_origins)


def test_a_loopback_base_host_admits_any_loopback_port_like_the_sdk() -> None:
    """``localhost``/``127.0.0.1`` add the SDK's own loopback wildcards, under both
    schemes; a real name never gets them (above)."""
    config = _config("path", base_host="localhost", scheme="http")
    hosts = mcp_hosts(config)
    assert hosts == ("localhost", *LOOPBACK_WILDCARD_HOSTS)
    origins = mcp_origins(config)
    for host in LOOPBACK_WILDCARD_HOSTS:
        assert f"http://{host}" in origins
        assert f"https://{host}" in origins
    subdomain = _config("subdomain", base_host="localhost", scheme="http")
    assert mcp_hosts(subdomain) == ("mcp.localhost", "mcp.localhost:*")


def test_transport_security_refuses_an_empty_host_list() -> None:
    with pytest.raises(ValueError):
        transport_security_for([], origins=[])


def test_build_mcp_app_refuses_disabled_rebinding_protection() -> None:
    with pytest.raises(ValueError, match="DNS-rebinding"):
        build_mcp_app(
            consumers=None,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )


# --- which requests are the surface -------------------------------------------------


def _scope(path: str, host: str = "example.test") -> dict[str, Any]:
    return {"type": "http", "path": path, "headers": [(b"host", host.encode())]}


def test_path_mode_claims_the_surface_path_with_and_without_its_slash() -> None:
    mount = McpMount.for_config(_config("path"), app=object())  # type: ignore[arg-type]
    assert mount.claims(_scope("/mcp")) is True
    assert mount.claims(_scope("/mcp/")) is True
    assert mount.claims(_scope("/mcp/other")) is None
    assert mount.claims(_scope("/mcpx")) is None
    assert mount.claims(_scope("/api/v1/operations/x")) is None


def test_subdomain_mode_claims_the_mcp_host_only() -> None:
    mount = McpMount.for_config(_config("subdomain"), app=object())  # type: ignore[arg-type]
    assert mount.claims(_scope("/", "mcp.example.test")) is True
    assert mount.claims(_scope("/", "MCP.Example.Test:8443")) is True
    assert mount.claims(_scope("/", "mcp.example.test.")) is True
    assert mount.claims(_scope("/", "mcp.example.test.:8443")) is True
    assert mount.claims(_scope("/auth/login", "mcp.example.test.")) is False
    assert mount.claims(_scope("/auth/login", "MCP.EXAMPLE.TEST")) is False
    assert mount.claims(_scope("/auth/login", "mcp.example.test")) is False
    assert mount.claims(_scope("/mcp", "circuit.example.test")) is None
    assert mount.claims(_scope("/", "circuit.example.test")) is None


@pytest.mark.parametrize("path", ["/", "", "mcp", "/mcp/"])
def test_a_path_mode_surface_path_that_would_misroute_is_refused(path: str) -> None:
    with pytest.raises(ValueError, match="routing.mcp.path"):
        McpMount.for_config(_config("path", mcp_path=path), app=object())  # type: ignore[arg-type]


def test_normalize_host_strips_port_case_and_trailing_dots() -> None:
    assert normalize_host("MCP.Example.Test.:8443") == "mcp.example.test"
    assert normalize_host("example.test..") == "example.test"
    assert normalize_host("localhost:3000") == "localhost"


@pytest.mark.parametrize("label", ["", "   ", "circuit", "auth", "api", "recallatron"])
def test_a_subdomain_mcp_label_that_collides_or_is_empty_is_refused(label: str) -> None:
    """Empty, or the shell, identity, api or a loaded module's host: the router would
    take that host's routes away, so the mount refuses at startup."""
    config = _config(
        "subdomain",
        mcp_host=label,
        modules={"recallatron": {"host": "recallatron", "path": "/recallatron"}},
    )
    with pytest.raises(ValueError, match="routing.mcp.host"):
        McpMount.for_config(config, app=object())  # type: ignore[arg-type]


def test_a_distinct_subdomain_mcp_label_is_accepted() -> None:
    config = _config(
        "subdomain",
        modules={"recallatron": {"host": "recallatron", "path": "/recallatron"}},
    )
    mount = McpMount.for_config(config, app=object())  # type: ignore[arg-type]
    assert mount.host == "mcp.example.test"


# --- approval_id, structured, on both surfaces --------------------------------------


def test_the_http_envelope_carries_approval_id_only_when_there_is_one() -> None:
    approval_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    held = envelope(
        APPROVAL_REQUIRED,
        operation_id,
        approval_id=approval_id,
        error_code=APPROVAL_REQUIRED,
        error_text=f"approval {approval_id} is pending",
    )
    assert held["approval_id"] == str(approval_id)
    assert held["operation_id"] == str(operation_id)
    assert held["error"] == {
        "error_code": APPROVAL_REQUIRED,
        "error_text": f"approval {approval_id} is pending",
    }
    plain = envelope("succeeded", None, approval_id=None, result={})
    assert "approval_id" not in plain


async def test_the_mcp_tool_result_carries_approval_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_on_call_tool`` renders the outcome's ``approval_id`` into the structured
    payload and the text copy alike. The gate and the seam are stubbed because the
    claim is about rendering: the dispatcher setting the field is
    ``tests/postgres/test_approvals.py``'s."""
    approval_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    outcome = OperationOutcome(
        APPROVAL_REQUIRED,
        error=OperationError(APPROVAL_REQUIRED, f"approval {approval_id} is pending"),
        operation_id=operation_id,
        approval_id=approval_id,
    )
    monkeypatch.setattr(transport, "_context_of", lambda ctx: SimpleNamespace())
    monkeypatch.setattr(transport, "call_tool", lambda *args, **kwargs: outcome)
    result = await transport._on_call_tool(
        SimpleNamespace(),  # type: ignore[arg-type]
        types.CallToolRequestParams(name="anything"),
        consumers=None,
    )
    assert result.is_error is True
    payload = result.structured_content
    assert isinstance(payload, dict)
    assert payload["state"] == APPROVAL_REQUIRED
    assert payload["approval_id"] == str(approval_id)
    assert payload["operation_id"] == str(operation_id)
    assert payload["error"]["error_text"] == f"approval {approval_id} is pending"
    assert json.loads(result.content[0].text) == payload  # type: ignore[union-attr]


async def test_the_mcp_tool_result_omits_approval_id_when_there_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport, "_context_of", lambda ctx: SimpleNamespace())
    monkeypatch.setattr(
        transport, "call_tool", lambda *args, **kwargs: OperationOutcome("succeeded")
    )
    result = await transport._on_call_tool(
        SimpleNamespace(),  # type: ignore[arg-type]
        types.CallToolRequestParams(name="anything"),
        consumers=None,
    )
    assert isinstance(result.structured_content, dict)
    assert "approval_id" not in result.structured_content


def test_mcp_path_is_the_one_route_the_built_app_answers() -> None:
    app = build_mcp_app(consumers=None)
    assert [getattr(route, "path", None) for route in app.routes] == [MCP_PATH]
