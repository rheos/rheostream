"""The OAuth connector's HTTP routes on the identity host (issue #287).

``POST /auth/oauth/register`` (RFC 7591, JSON) and ``POST /auth/oauth/token``
(RFC 6749 s3.2, form-encoded) are here; the browser routes (``/authorize`` and
``/consent``) join them in this module. Each is a thin wire over
``rheo_core.oauth.service``: parse the request, call the one service step, turn its
value or returned :class:`~rheo_core.oauth.service.OAuthError` into a response. The
service's transaction has exited by the time the response is built, so whatever a
refusal wrote (a burned code, a revocation, a refused event row) is committed.

**404 unless the feature is configured, and on the identity host.**
:func:`surface_or_404` resolves ``oauth_surface`` from the current settings and
routing on every request, the discipline ``auth_routes`` follows, so it agrees with
the ``mcp`` mount after any restart. Unconfigured, or (subdomain mode) a request on
any host but the identity host, answers the same 404 an unrouted path gets, so a
deployment with the feature off shows no trace of it.

**The route paths.** Served under the identity surface's declared default prefix
(``rheo_core.oauth.surface.SERVED_IDENTITY_PATH``), the prefix ``auth_routes``
serves its ``/auth/*`` routes under. The advertised URLs are
``url_for(IDENTITY, "/oauth/...")``, and ``oauth_surface`` is unconfigured when
``routing.identity.path`` names any other prefix, so the two always agree.

**The token endpoint reads only the body.** The Content-Type must be
``application/x-www-form-urlencoded``; the body is parsed with
``urllib.parse.parse_qsl(strict_parsing=True)`` (no ``python-multipart``), and a
duplicated parameter is refused (RFC 6749 s3.1). Nothing is read from the query
string, so a code, verifier or refresh value placed there is never redeemed.
Every answer carries ``Cache-Control: no-store`` (RFC 6749 s5.1). Errors are
RFC 6749 s5.2 JSON with a fixed description per error word: no request value ever
reaches a response body, an exception or a log line.

**The registration source** is the ASGI client address (``request.client.host``),
which behind a proxy is the proxy's unless the server is run with
``FORWARDED_ALLOW_IPS`` (FR 7).
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from rheo_core.oauth import OAuthSurface, oauth_surface
from rheo_core.oauth.service import (
    INVALID_REQUEST,
    OAuthError,
    exchange_code,
    refresh,
    register_client,
)
from rheo_core.oauth.surface import SERVED_IDENTITY_PATH
from rheo_core.routing import RoutingConfig, RoutingMode
from rheo_core.settings import resolve
from starlette.concurrency import run_in_threadpool

from rheo_app_core.auth_routes import normalize_host
from rheo_app_core.routing import routing_config

router = APIRouter()

_ROUTE_PREFIX: Final = SERVED_IDENTITY_PATH
REGISTER_PATH: Final = f"{_ROUTE_PREFIX}/oauth/register"
TOKEN_PATH: Final = f"{_ROUTE_PREFIX}/oauth/token"

FORM_MEDIA_TYPE: Final = "application/x-www-form-urlencoded"
UNSUPPORTED_GRANT_TYPE: Final = "unsupported_grant_type"
AUTHORIZATION_CODE: Final = "authorization_code"
REFRESH_TOKEN: Final = "refresh_token"
_MAX_FORM_FIELDS: Final = 32
"""More fields than any token request has; past this the body is refused."""

MAX_BODY_BYTES: Final = 16 * 1024
"""The largest register or token body read. Both routes are unauthenticated, and
core has no request-body limit of its own to reuse, so the bound is set here: a
real registration or token request is well under 2 KiB."""

_NO_STORE: Final = {"Cache-Control": "no-store"}

_ROUTE_DESCRIPTIONS: Final[Mapping[str, str]] = {
    INVALID_REQUEST: "The token request is malformed.",
    UNSUPPORTED_GRANT_TYPE: "Only authorization_code and refresh_token are supported.",
}
"""Fixed descriptions for the refusals this module makes itself (before the
service is reached)."""


def _not_found() -> Response:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _is_identity_host(request: Request, config: RoutingConfig) -> bool:
    """Path mode has one host, so any host is it; subdomain mode compares the
    normalized ``Host`` with the identity host."""
    if config.mode is RoutingMode.PATH:
        return True
    host = normalize_host(request.headers.get("host") or "")
    return host == normalize_host(f"{config.surfaces.identity.host}.{config.base_host}")


def surface_or_404(request: Request) -> OAuthSurface | Response:
    """The configured surface for this request, or the 404 to answer with."""
    config = routing_config()
    surface = oauth_surface(resolve(), config)
    if not isinstance(surface, OAuthSurface) or not _is_identity_host(request, config):
        return _not_found()
    return surface


def _error_body(error: str, description: str, status: int) -> Response:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers=_NO_STORE,
    )


def _oauth_error(error: OAuthError) -> Response:
    """A returned service refusal as RFC 6749 s5.2 / RFC 7591 s3.2.2 JSON."""
    return _error_body(error.error, error.error_description, error.status)


def _route_error(error: str) -> Response:
    return _error_body(error, _ROUTE_DESCRIPTIONS[error], 400)


async def _bounded_body(request: Request) -> bytes | None:
    """The body, or ``None`` once it exceeds :data:`MAX_BODY_BYTES`.

    A declared ``Content-Length`` over the limit (or not a number) is refused
    before anything is read; otherwise the stream is read chunk by chunk and
    abandoned the moment it passes the limit, so a chunked or understated body is
    never buffered whole."""
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.strip().isdigit() or int(declared) > MAX_BODY_BYTES:
            return None
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAX_BODY_BYTES:
            return None
    return bytes(received)


def _source(request: Request) -> str:
    return request.client.host if request.client is not None else ""


# --- POST /auth/oauth/register ---------------------------------------------------


@router.post(REGISTER_PATH, include_in_schema=False)
async def register(request: Request) -> Response:
    surface = surface_or_404(request)
    if isinstance(surface, Response):
        return surface
    raw = await _bounded_body(request)
    metadata: object = None
    if raw is not None:
        try:
            metadata = json.loads(raw)
        except (UnicodeDecodeError, ValueError, RecursionError):
            metadata = None
    # An oversized or non-JSON body is handed on as a non-object, so the service
    # applies the rate limit and records the refused registration as it does for
    # ``[]`` (``invalid_client_metadata``).
    outcome = await run_in_threadpool(
        register_client,
        surface,
        metadata=metadata,
        source=_source(request),
        now=datetime.now(UTC),
    )
    if isinstance(outcome, OAuthError):
        return _oauth_error(outcome)
    return JSONResponse(outcome, status_code=201, headers=_NO_STORE)


# --- POST /auth/oauth/token ------------------------------------------------------


def _is_form(request: Request) -> bool:
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    return media_type.strip().lower() == FORM_MEDIA_TYPE


def _parse_form(raw: bytes) -> dict[str, str] | None:
    """The body's parameters, or ``None`` for a body that is not strictly
    form-encoded or that names a parameter twice."""
    try:
        pairs = parse_qsl(
            raw.decode("ascii"),
            keep_blank_values=True,
            strict_parsing=True,
            errors="strict",
            max_num_fields=_MAX_FORM_FIELDS,
        )
    except (UnicodeDecodeError, ValueError):
        return None
    form = dict(pairs)
    if len(form) != len(pairs):
        return None
    return form


@router.post(TOKEN_PATH, include_in_schema=False)
async def token(request: Request) -> Response:
    surface = surface_or_404(request)
    if isinstance(surface, Response):
        return surface
    if not _is_form(request):
        return _route_error(INVALID_REQUEST)
    raw = await _bounded_body(request)
    form = None if raw is None else _parse_form(raw)
    if form is None:
        return _route_error(INVALID_REQUEST)
    grant_type = form.get("grant_type")
    if grant_type is None:
        return _route_error(INVALID_REQUEST)
    if grant_type == AUTHORIZATION_CODE:
        step = exchange_code
    elif grant_type == REFRESH_TOKEN:
        step = refresh
    else:
        return _route_error(UNSUPPORTED_GRANT_TYPE)
    outcome = await run_in_threadpool(step, surface, form, now=datetime.now(UTC))
    if isinstance(outcome, OAuthError):
        return _oauth_error(outcome)
    return JSONResponse(outcome, headers={**_NO_STORE, "Pragma": "no-cache"})


__all__ = [
    "FORM_MEDIA_TYPE",
    "REGISTER_PATH",
    "TOKEN_PATH",
    "UNSUPPORTED_GRANT_TYPE",
    "router",
    "surface_or_404",
]
