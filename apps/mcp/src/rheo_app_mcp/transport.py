"""The streamable-HTTP MCP server: the wire protocol around ``tools.py``.

``apps/mcp`` is "served by the ``core`` process over streamable HTTP at the
``mcp`` surface (subdomain mode ``mcp.<base>/``; path mode ``/mcp``)"
(``docs/architecture/runtime-and-mcp.md`` § The MCP facade). This module builds
that server and nothing else: :func:`build_mcp_app` returns a Starlette
application, and every request that reaches a handler has already had its bearer
resolved by :class:`_BearerGate`.

**Three things this module deliberately does not do.**

1. **It does not decide anything about a token.** ``session.resolve_context`` ->
   ``rheo_core.boundary.factories.context_from_token`` is the one place a bearer
   is checked, and it already distinguishes malformed, wrong-kind, expired,
   revoked, scope-invalid and unavailable-workspace. The gate calls it and acts
   on what comes back. Re-deriving any part of that here would be a second
   implementation of the boundary, drifting from the first the day either moves.
2. **It does not re-implement listing or dispatch.** ``list_tools``/``call_tool``
   in ``tools.py`` are the seam; the handlers below translate their results into
   the SDK's result types and back, and translate nothing else.
3. **It does not mount itself.** ``apps/core``'s ``main.py`` imports this package
   only to record the composition-root import edge, and says so; wiring the
   transport into that process's request-serving path is a later step. A test
   drives :func:`build_mcp_app`'s return value through an ASGI transport with no
   process, which is the same way ``tests/test_healthz.py`` drives ``app``.

**Why the gate is ASGI middleware and not an MCP-layer check.** "A malformed,
wrong-kind, expired, revoked, or scope-invalid token is refused at the transport
with a distinct state and no tool runs" (criterion 9, restated for the façade in
``runtime-and-mcp.md``). Refusing inside a tool handler would satisfy the first
half and not the second: the MCP session would already be initialised and the
handler already entered. Refusing in front of the ASGI application means the
request never reaches the SDK at all, which is a property a test can assert by
watching a call counter stay at zero rather than by reading this paragraph.

**Why the resolved context travels in the ASGI scope.** The gate resolves once
and puts the :class:`~rheo_contracts.WorkspaceContext` in ``scope["state"]``; the
handlers read it back off the per-message request the transport attaches. The
alternative -- resolving again inside each handler -- would mean the context a
tool runs under is not provably the one the gate approved, and a token revoked
between the two resolutions would give two different answers to one request.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Final

import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from rheo_contracts import ToolDeclaration, WorkspaceContext
from rheo_core.boundary.context import TOKEN_MALFORMED, Refusal
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from rheo_app_mcp.session import resolve_context
from rheo_app_mcp.tools import call_tool, list_tools

MCP_PATH: Final = "/mcp"
"""The path-mode surface. Subdomain mode serves the same application at ``/``;
which one a deployment uses is a routing decision above this module."""

SERVER_NAME: Final = "rheo-stream"

CONTEXT_STATE_KEY: Final = "rheo_workspace_context"
"""The ``scope["state"]`` key the gate writes and the handlers read. A request
that reaches a handler always carries it, because the gate is the only way in."""

AUTHORIZATION_HEADER: Final = b"authorization"
_BEARER_PREFIX: Final = "bearer "

REFUSAL_STATUS: Final = 401
"""The same status ``api_routes.py`` answers a token refusal with
(``_TOKEN_REFUSAL_STATUS``). Every bearer surface refuses alike."""

NO_BEARER_DETAIL: Final = "no bearer token was presented"
"""Verbatim from ``api_routes.py``'s own missing-bearer branch, so the two bearer
surfaces say the same thing about the same condition."""

CONTEXT_MISSING: Final = "context_missing"
"""**Not a refusal state, and never sent on the wire**: the message of a
``RuntimeError`` raised when a handler runs with no gate-resolved context in its
request. Unreachable while the gate is the only entry point, and raised rather
than defaulted so a future mounting that bypasses the gate fails loudly instead
of serving tools to an unauthenticated caller. A named constant only so the test
that drives the handlers ungated can assert on it rather than on prose."""


def _bearer_from(scope: Scope) -> str | None:
    """The bearer value in this request's ``Authorization`` header, or ``None``.

    ``Scope`` is a ``MutableMapping[str, Any]``, so the header pairs are annotated
    here rather than trusted: an untyped ``value.decode()`` would hand ``Any`` all
    the way back to the caller and quietly disable every check on the result.
    """
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", ())
    for key, value in headers:
        if key.lower() == AUTHORIZATION_HEADER:
            header = value.decode("latin-1")
            if header[: len(_BEARER_PREFIX)].lower() == _BEARER_PREFIX:
                return header[len(_BEARER_PREFIX) :].strip()
            return None
    return None


async def _refuse(send: Send, state: str, detail: str | None) -> None:
    """Answer :data:`REFUSAL_STATUS` with the refusal's own state, and stop.

    **The state vocabulary is the shipped one; the body shape is not, and the
    reason is a boundary rather than a preference.** Every ``state`` this sends is
    a ``rheo_core.boundary.context`` constant -- the refusal's own, or
    :data:`~rheo_core.boundary.context.TOKEN_MALFORMED` for a missing bearer,
    which is exactly what ``apps/core``'s ``api_routes.py`` answers the identical
    condition with, down to the detail text. Nothing here invents a state.

    The *envelope* is a local ``{state, detail}`` object rather than
    ``api_routes.envelope()`` because that function lives in ``apps/core``, which
    this package does not and must not depend on: ``rheo-app-mcp`` does not
    declare ``rheo-app-core``, and a façade importing the core process's HTTP
    layer would invert the dependency criterion 20's scan exists to keep straight.
    The two surfaces also answer different protocols -- REST there, JSON-RPC here
    -- so a shared body shape would have to be carved down to the fields both can
    carry, which is the pair of fields below. Sharing the vocabulary without
    sharing the container is the part that is actually load-bearing: a closed set
    of state names stays closed.

    ``detail`` is the boundary's own, already documented as safe to show and never
    carrying a secret.
    """
    payload: dict[str, str] = {"state": state}
    if detail is not None:
        payload["detail"] = detail
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": REFUSAL_STATUS,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _BearerGate:
    """Resolve the bearer, or refuse before the request reaches the MCP server."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan and websocket scopes carry no bearer and no tool call.
            await self.app(scope, receive, send)
            return
        bearer = _bearer_from(scope)
        if bearer is None or not bearer:
            await _refuse(send, TOKEN_MALFORMED, NO_BEARER_DETAIL)
            return
        resolved = resolve_context(bearer)
        if isinstance(resolved, Refusal):
            await _refuse(send, resolved.state, resolved.detail)
            return
        state = scope.setdefault("state", {})
        state[CONTEXT_STATE_KEY] = resolved
        await self.app(scope, receive, send)


def _context_of(ctx: ServerRequestContext[Any, Any]) -> WorkspaceContext:
    """The gate-resolved context for the request this handler is answering."""
    request = ctx.request
    resolved = getattr(getattr(request, "state", None), CONTEXT_STATE_KEY, None)
    if not isinstance(resolved, WorkspaceContext):
        raise RuntimeError(f"{CONTEXT_MISSING}: this request bypassed the bearer gate")
    return resolved


def _as_tool(declaration: ToolDeclaration) -> types.Tool:
    """One registered declaration as the SDK's tool type.

    ``input_schema`` is generated from the declaration's own input model rather
    than written out here, so a model that gains a field describes itself to a
    client without an edit in this file. The description names the operation
    because ``ToolDeclaration`` carries no description of its own in this tree
    (``manifest.py``); inventing prose for it here would put a second, unreviewed
    source of tool documentation in the transport layer.
    """
    return types.Tool(
        name=declaration.name,
        description=f"Calls the {declaration.operation} operation.",
        input_schema=declaration.input_model.model_json_schema(),
    )


async def _on_list_tools(
    ctx: ServerRequestContext[Any, Any],
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    """``tools/list``: exactly ``list_tools(ctx)``, rendered for the wire."""
    workspace_ctx = _context_of(ctx)
    return types.ListToolsResult(
        tools=[_as_tool(decl) for decl in list_tools(workspace_ctx)]
    )


async def _on_call_tool(
    ctx: ServerRequestContext[Any, Any],
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    """``tools/call``: exactly ``call_tool(ctx, name, arguments)``, rendered.

    A refused or failed outcome comes back as a result with ``is_error`` set and
    the outcome's own state in the payload, not as a JSON-RPC protocol error: the
    call itself was well formed and the server answered it. Which state it was is
    the part a caller acts on, so it is in the structured content rather than only
    in prose.
    """
    workspace_ctx = _context_of(ctx)
    outcome = call_tool(workspace_ctx, params.name, params.arguments or {})
    payload: dict[str, Any] = {"state": outcome.state}
    if outcome.result is not None:
        payload["result"] = outcome.result.model_dump(mode="json")
    if outcome.error is not None:
        payload["error"] = {
            "error_code": outcome.error.error_code,
            "error_text": outcome.error.error_text,
        }
    if outcome.operation_id is not None:
        payload["operation_id"] = str(outcome.operation_id)
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(payload))],
        structured_content=payload,
        is_error=not outcome.ok,
    )


def build_server() -> Server[Any]:
    """The lowlevel MCP server, with the two handlers this façade answers."""
    return Server(
        SERVER_NAME,
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )


def build_mcp_app(
    *,
    path: str = MCP_PATH,
    json_response: bool = False,
    host: str = "127.0.0.1",
) -> Starlette:
    """The gated streamable-HTTP application, ready to mount or to drive in-process.

    A factory rather than a module-level singleton: constructing the SDK's session
    manager has real setup behind it, and ``apps/core``'s ``main.py`` imports this
    package for the composition-root edge alone. An application built at import
    time would make that edge do work nobody asked for.

    ``host`` is passed through to the SDK, which turns it into the DNS-rebinding
    protection a browser-reachable endpoint needs; it is named here so a
    deployment can widen it deliberately rather than discovering the default by
    being blocked.
    """
    server = build_server()
    app = server.streamable_http_app(
        streamable_http_path=path,
        json_response=json_response,
        host=host,
    )
    app.add_middleware(_BearerGate)
    return app


__all__ = [
    "CONTEXT_MISSING",
    "NO_BEARER_DETAIL",
    "CONTEXT_STATE_KEY",
    "MCP_PATH",
    "REFUSAL_STATUS",
    "SERVER_NAME",
    "build_mcp_app",
    "build_server",
]
