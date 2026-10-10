"""Issue #290, database-free: the rules ``rheo_app_core.surface_guard`` applies, and
the guard itself over a stub application, from the shared routing fixtures.

The lifespan-driven half (the real app, real tokens, both modes over the wire) is
``tests/postgres/test_surface_guard.py``.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from rheo_app_core.surface_guard import (
    STATE_ATTRIBUTE,
    SurfaceGuard,
    SurfaceRules,
    under,
)
from rheo_core.routing import RoutingConfig
from starlette.types import Message, Receive, Scope, Send

FIXTURES = Path(__file__).parent / "fixtures" / "routing"


def _config(mode: str, *, identity_path: str | None = None) -> RoutingConfig:
    raw = json.loads((FIXTURES / f"{mode}-mode.json").read_text(encoding="utf-8"))
    if identity_path is not None:
        raw["surfaces"]["identity"]["path"] = identity_path
    return RoutingConfig.model_validate(raw)


def test_under_is_segment_wise() -> None:
    assert under("/api", "/api")
    assert under("/api/v1", "/api")
    assert not under("/apis", "/api")
    assert not under("/", "/api")


def test_subdomain_rules_name_the_api_host() -> None:
    config = _config("subdomain")
    rules = SurfaceRules.for_config(config)
    assert rules.api_host == f"{config.surfaces.api.host}.{config.base_host}"
    assert rules.identity_prefix == "/auth"


def test_path_rules_check_no_host() -> None:
    assert SurfaceRules.for_config(_config("path")).api_host is None


def _scope(path: str, host: str | None) -> Scope:
    headers = [] if host is None else [(b"host", host.encode("latin-1"))]
    return {"type": "http", "path": path, "raw_path": path.encode(), "headers": headers}


def test_refuses_only_api_paths_off_the_api_host() -> None:
    config = _config("subdomain")
    rules = SurfaceRules.for_config(config)
    api_host = f"api.{config.base_host}"
    shell_host = f"{config.surfaces.shell.host}.{config.base_host}"
    assert not rules.refuses(_scope("/api/v1/operations/x", api_host))
    assert not rules.refuses(_scope("/api/v1/operations/x", f"{api_host}:8443"))
    assert rules.refuses(_scope("/api/v1/operations/x", shell_host))
    assert rules.refuses(_scope("/api", shell_host))
    assert rules.refuses(_scope("/api/v1/operations/x", None))
    assert not rules.refuses(_scope("/apis", shell_host))
    assert not rules.refuses(_scope("/auth/login", shell_host))
    path_rules = SurfaceRules.for_config(_config("path"))
    assert not path_rules.refuses(_scope("/api/v1/operations/x", "anything.test"))


def test_identity_target_with_the_default_prefix_leaves_requests_alone() -> None:
    rules = SurfaceRules.for_config(_config("path"))
    for path in ("/auth", "/auth/login", "/id/login", "/api/x"):
        assert rules.identity_target(path) is None


@pytest.mark.parametrize("mode", ["path", "subdomain"])
def test_identity_target_with_a_moved_prefix(mode: str) -> None:
    rules = SurfaceRules.for_config(_config(mode, identity_path="/id/sign"))
    assert rules.identity_target("/id/sign") == "/auth"
    assert rules.identity_target("/id/sign/login") == "/auth/login"
    assert rules.identity_target("/id/sign/oauth/token") == "/auth/oauth/token"
    assert rules.identity_target("/auth/login") == ""
    assert rules.identity_target("/auth") == ""
    assert rules.identity_target("/id/signs") is None
    assert rules.identity_target("/authors") is None


def test_a_prefix_inside_the_served_one_still_relocates() -> None:
    """``/auth/v2`` is a legal prefix; ``/auth/v2/login`` is served and the bare
    ``/auth/login`` is not."""
    rules = SurfaceRules.for_config(_config("path", identity_path="/auth/v2"))
    assert rules.identity_target("/auth/v2/login") == "/auth/login"
    assert rules.identity_target("/auth/login") == ""


@pytest.mark.parametrize(
    ("mode", "prefix"),
    [
        ("path", "/api"),
        ("path", "/api/auth"),
        ("subdomain", "/api"),
        ("subdomain", "/healthz"),
        ("path", "/.well-known/auth"),
        ("path", "/mcp"),
        ("path", "/mcp/auth"),
    ],
)
def test_an_overlapping_identity_prefix_is_refused(mode: str, prefix: str) -> None:
    with pytest.raises(ValueError, match=r"routing\.identity\.path"):
        SurfaceRules.for_config(_config(mode, identity_path=prefix))


def test_subdomain_mode_does_not_reserve_the_mcp_path() -> None:
    """The ``mcp`` surface has its own host there, so ``/mcp`` on an application
    host is not core's and an identity prefix may use it."""
    rules = SurfaceRules.for_config(_config("subdomain", identity_path="/mcp"))
    assert rules.identity_prefix == "/mcp"


class _Recorder:
    def __init__(self) -> None:
        self.scopes: list[Scope] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.scopes.append(scope)


class _State:
    pass


async def _drive(guard: SurfaceGuard, scope: Scope) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    await guard(scope, receive, send)
    return sent


def _app_with(rules: SurfaceRules | None) -> Any:
    holder = _State()
    holder.state = _State()  # type: ignore[attr-defined]
    setattr(holder.state, STATE_ATTRIBUTE, rules)  # type: ignore[attr-defined]
    return holder


async def test_the_guard_passes_everything_through_without_rules() -> None:
    inner = _Recorder()
    scope = {**_scope("/api/v1/operations/x", "circuit.example.test"), "app": None}
    assert await _drive(SurfaceGuard(inner), scope) == []
    assert inner.scopes == [scope]


async def test_the_guard_refuses_rewrites_and_passes() -> None:
    config = _config("subdomain", identity_path="/id")
    rules = SurfaceRules.for_config(config)
    shell_host = f"{config.surfaces.shell.host}.{config.base_host}"
    owner = _app_with(rules)
    inner = _Recorder()
    guard = SurfaceGuard(inner)

    refused = await _drive(
        guard, {**_scope("/api/v1/operations/x", shell_host), "app": owner}
    )
    assert refused[0]["status"] == 404
    old = await _drive(guard, {**_scope("/auth/login", shell_host), "app": owner})
    assert old[0]["status"] == 404
    assert inner.scopes == []

    raw = {**_scope("/id/login", shell_host), "app": owner}
    raw["raw_path"] = b"/id/login%2Fx"
    assert await _drive(guard, raw) == []
    rewritten = inner.scopes[-1]
    assert rewritten["path"] == "/auth/login"
    assert rewritten["raw_path"] == b"/auth/login%2Fx"
    assert raw["path"] == "/id/login", "the caller's scope is not mutated"

    plain = {**_scope("/healthz", shell_host), "app": owner}
    assert await _drive(guard, plain) == []
    assert inner.scopes[-1] is plain
