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

**The browser routes.** ``GET /auth/oauth/authorize`` validates the request, holds
it server-side and moves the browser off the parameter-bearing URL: a ``303`` to the
consent path with the ``rheo_oauth_request`` cookie, the only handle the rest of the
flow uses (FR 17). ``GET /auth/oauth/consent`` sends a browser with no session
through the existing ``/auth/login`` (its ``return`` is the consent URL, so the
GitHub callback is unchanged) and otherwise renders the consent page.
``POST /auth/oauth/consent`` checks ``Origin`` first, then that the cookie's
pending row is the one the page showed (``request_id``), then the session, and
only then decides. Every page here (consent and errors, a 404 included) is a
static document with every interpolated value ``html.escape``d, served with
:data:`PAGE_HEADERS` so it can neither be framed nor cached nor load anything.
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from html import escape
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from rheo_core.boundary.context import SESSION_MISSING
from rheo_core.oauth import OAuthSurface, oauth_surface
from rheo_core.oauth.service import (
    AUTHORIZATION_EXPIRED,
    INVALID_REQUEST,
    NOT_A_MEMBER,
    WORKSPACE_UNSELECTED,
    Approval,
    ConsentView,
    OAuthError,
    approve,
    begin_authorization,
    consent_view,
    deny,
    exchange_code,
    pending,
    refresh,
    register_client,
)
from rheo_core.oauth.surface import SERVED_IDENTITY_PATH
from rheo_core.routing import (
    IDENTITY,
    RoutingConfig,
    RoutingMode,
    identity_path,
    url_for,
)
from rheo_core.sessions import build_cookie
from rheo_core.sessions.cookies import OAUTH_REQUEST_COOKIE
from rheo_core.settings import resolve
from starlette.concurrency import run_in_threadpool

from rheo_app_core.auth_routes import (
    _check_origin,
    _clear,
    _current_host,
    _session_row_from_cookie,
    identity_host,
    normalize_host,
)
from rheo_app_core.routing import routing_config

router = APIRouter()

_ROUTE_PREFIX: Final = SERVED_IDENTITY_PATH
REGISTER_PATH: Final = f"{_ROUTE_PREFIX}/oauth/register"
TOKEN_PATH: Final = f"{_ROUTE_PREFIX}/oauth/token"
AUTHORIZE_PATH: Final = f"{_ROUTE_PREFIX}/oauth/authorize"
CONSENT_PATH: Final = f"{_ROUTE_PREFIX}/oauth/consent"
_CONSENT_SUFFIX: Final = "/oauth/consent"
_LOGIN_SUFFIX: Final = "/login"

DECISION_ALLOW: Final = "allow"
DECISION_DENY: Final = "deny"
NOT_FOUND: Final = "not_found"

PAGE_HEADERS: Final[Mapping[str, str]] = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
"""On the consent page and every error page: nothing may load, frame or cache it."""

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


def _now() -> datetime:
    """The routes' clock (one read per request); tests pin it."""
    return datetime.now(UTC)


def _not_found() -> Response:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _resolved(request: Request) -> tuple[OAuthSurface, RoutingConfig] | None:
    """The configured surface and routing for this request, or ``None`` when the
    feature is unconfigured or (subdomain mode) the request is not on the identity
    host. Path mode has one host, so any host is it."""
    config = routing_config()
    surface = oauth_surface(resolve(), config)
    if not isinstance(surface, OAuthSurface):
        return None
    if config.mode is not RoutingMode.PATH and _current_host(request) != normalize_host(
        identity_host(config)
    ):
        return None
    return surface, config


def surface_or_404(request: Request) -> OAuthSurface | Response:
    """The configured surface for this request, or the 404 to answer with."""
    resolved = _resolved(request)
    if resolved is None:
        return _not_found()
    return resolved[0]


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
        now=_now(),
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
    outcome = await run_in_threadpool(step, surface, form, now=_now())
    if isinstance(outcome, OAuthError):
        return _oauth_error(outcome)
    return JSONResponse(outcome, headers={**_NO_STORE, "Pragma": "no-cache"})


# --- pages -------------------------------------------------------------------------

_PAGE_HEADINGS: Final[Mapping[str, str]] = {
    AUTHORIZATION_EXPIRED: "This connection request has ended",
    WORKSPACE_UNSELECTED: "No workspace to connect",
    NOT_A_MEMBER: "That workspace is not available",
    SESSION_MISSING: "You are not signed in",
    NOT_FOUND: "Not found",
}
_DEFAULT_HEADING: Final = "The connection request was refused"

_ROUTE_PAGE_MESSAGES: Final[Mapping[str, str]] = {
    AUTHORIZATION_EXPIRED: "This sign-in request has expired or was already used.",
    INVALID_REQUEST: "The connection request is malformed.",
    SESSION_MISSING: "Sign in again, then start the connection from the app.",
    NOT_FOUND: "There is nothing at this address.",
}
"""Messages for the page refusals this module makes itself; a service refusal
carries its own fixed ``error_description``."""


def _document(title: str, state: str, body: str) -> str:
    """The page skeleton: the head of the identity provider's unavailable page,
    ``data-state`` on ``<body>``. ``body`` is already-escaped markup."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n"
        "</head>\n"
        f'<body data-state="{escape(state)}">\n'
        "<main>\n"
        f"{body}"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


def _page(document: str, status: int) -> Response:
    return HTMLResponse(document, status_code=status, headers=dict(PAGE_HEADERS))


def _error_page(error: str, status: int, message: str | None = None) -> Response:
    """An error page: the error word in ``data-state`` and the ``<small>`` code."""
    heading = _PAGE_HEADINGS.get(error, _DEFAULT_HEADING)
    text = message if message is not None else _ROUTE_PAGE_MESSAGES[error]
    body = (
        f"<h1>{escape(heading)}</h1>\n"
        f"<p>{escape(text)}</p>\n"
        f"<p><small>Error code: {escape(error)}</small></p>\n"
    )
    return _page(_document(heading, error, body), status)


def _not_found_page() -> Response:
    return _error_page(NOT_FOUND, 404)


def _with_query(uri: str, params: Mapping[str, str | None]) -> str:
    """``uri`` with ``params`` (``None`` values dropped) appended to its query."""
    query = urlencode(
        {key: value for key, value in params.items() if value is not None}
    )
    return f"{uri}{'&' if '?' in uri else '?'}{query}"


def _redirect(location: str, status: int = 302) -> RedirectResponse:
    return RedirectResponse(location, status_code=status, headers=dict(_NO_STORE))


def _refusal(error: OAuthError) -> Response:
    """A service refusal: a page, or (once the redirect URI is trusted) a redirect
    to it with ``error``, ``state`` and ``iss``."""
    if error.redirect and error.redirect_uri is not None:
        return _redirect(
            _with_query(
                error.redirect_uri,
                {"error": error.error, "state": error.state, "iss": error.iss},
            )
        )
    return _error_page(error.error, error.status, error.error_description)


def _days(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


def _terms_markup(operations: tuple[str, ...], days: int) -> str:
    items = "".join(f"<li>{escape(name)}</li>\n" for name in operations)
    return (
        "<p>It will be able to use these tools:</p>\n"
        f"<ul>\n{items}</ul>\n"
        f"<p>The connection lasts {escape(_days(days))} unless you revoke it "
        "sooner.</p>\n"
    )


def _consent_page(view: ConsentView, request_id: UUID) -> Response:
    """The consent page for ``view``. Every value from the client, the account or a
    workspace is escaped; the only markup is this function's own."""
    redirect_host = urlsplit(view.redirect_uri).hostname or view.redirect_uri
    names = {
        choice.workspace_id: choice.display_name for choice in view.choices.workspaces
    }
    parts = [
        "<h1>Allow this app to connect?</h1>\n",
        "<p>App name (chosen by the app, not verified): "
        f"<strong>{escape(view.client_name)}</strong></p>\n",
        "<p>After you decide, you are sent back to: "
        f"<strong>{escape(redirect_host)}</strong></p>\n",
        f"<p>Signed in as: <strong>{escape(view.account_display_name)}</strong></p>\n",
        '<form method="post">\n',
        f'<input type="hidden" name="request_id" value="{escape(str(request_id))}">\n',
    ]
    if len(view.choices.workspaces) >= 2:
        options = "".join(
            f'<option value="{escape(str(choice.workspace_id))}"'
            f"{' selected' if choice.workspace_id == view.choices.default else ''}>"
            f"{escape(choice.display_name)}</option>\n"
            for choice in view.choices.workspaces
        )
        parts.append(
            '<p><label for="workspace_id">Workspace to connect</label>\n'
            f'<select id="workspace_id" name="workspace_id">\n{options}</select></p>\n'
        )
    else:
        only = view.choices.workspaces[0]
        parts.append(
            "<p>Workspace to connect: "
            f"<strong>{escape(only.display_name)}</strong></p>\n"
            f'<input type="hidden" name="workspace_id" '
            f'value="{escape(str(only.workspace_id))}">\n'
        )
    distinct = {(terms.operations, terms.days) for terms in view.terms}
    if len(distinct) == 1:
        operations, days = distinct.pop()
        parts.append(_terms_markup(operations, days))
    else:
        for terms in view.terms:
            parts.append(
                f"<h2>In {escape(names.get(terms.workspace_id, ''))}</h2>\n"
                + _terms_markup(terms.operations, terms.days)
            )
    parts.append(
        '<p style="display:flex;gap:1rem">'
        f'<button type="submit" name="decision" value="{DECISION_ALLOW}">Allow'
        "</button>\n"
        f'<button type="submit" name="decision" value="{DECISION_DENY}">Deny'
        "</button></p>\n"
        "</form>\n"
    )
    title = "Allow this app to connect?"
    return _page(_document(title, "consent", "".join(parts)), 200)


def _uuid_or_none(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _clearing_request_cookie(response: Response, config: RoutingConfig) -> Response:
    response.headers.append("set-cookie", _clear(OAUTH_REQUEST_COOKIE, config))
    return response


# --- GET /auth/oauth/authorize ---------------------------------------------------


@router.get(AUTHORIZE_PATH, include_in_schema=False)
def authorize(request: Request) -> Response:
    resolved = _resolved(request)
    if resolved is None:
        return _not_found_page()
    surface, config = resolved
    pairs = request.query_params.multi_items()
    params = dict(pairs)
    if len(params) != len(pairs):
        # RFC 6749 s3.1: a parameter sent twice; nothing about the redirect URI is
        # trusted yet, so this is a page.
        return _error_page(INVALID_REQUEST, 400)
    outcome = begin_authorization(surface, params, now=_now())
    if isinstance(outcome, OAuthError):
        return _refusal(outcome)
    response = _redirect(identity_path(config, _CONSENT_SUFFIX), status=303)
    response.headers.append(
        "set-cookie", build_cookie(OAUTH_REQUEST_COOKIE, outcome, scheme=config.scheme)
    )
    return response


# --- GET /auth/oauth/consent -----------------------------------------------------


@router.get(CONSENT_PATH, include_in_schema=False)
def consent(request: Request) -> Response:
    resolved = _resolved(request)
    if resolved is None:
        return _not_found_page()
    surface, config = resolved
    nonce = request.cookies.get(OAUTH_REQUEST_COOKIE)
    now = _now()
    row = pending(nonce, now)
    if row is None:
        return _error_page(AUTHORIZATION_EXPIRED, 400)
    session = _session_row_from_cookie(request, _current_host(request))
    if session is None:
        return_target = url_for(config, IDENTITY, _CONSENT_SUFFIX)
        login = (
            f"{identity_path(config, _LOGIN_SUFFIX)}?"
            f"{urlencode({'return': return_target})}"
        )
        return _redirect(login)
    view = consent_view(surface, row, session)
    if not view.choices.workspaces:
        # Decided here (``approve`` marks the row and records the refusal), so the
        # request cannot be completed later once a workspace exists.
        outcome = approve(row.id, nonce, session.account_id, None, now=now)
        error = (
            outcome
            if isinstance(outcome, OAuthError)
            else OAuthError(error=WORKSPACE_UNSELECTED, status=403, redirect=False)
        )
        return _clearing_request_cookie(_refusal(error), config)
    return _consent_page(view, row.id)


# --- POST /auth/oauth/consent ----------------------------------------------------


_CONSENT_FIELDS: Final = frozenset({"request_id", "workspace_id", "decision"})


def _decide(
    request: Request, surface: OAuthSurface, config: RoutingConfig, form: dict[str, str]
) -> Response:
    """The decision, after the ``Origin`` check and the form parse: the cookie's
    pending row must be the one the page showed, then a live session, then
    ``approve`` or ``deny``."""
    nonce = request.cookies.get(OAUTH_REQUEST_COOKIE)
    now = _now()
    row = pending(nonce, now)
    if row is None or _uuid_or_none(form.get("request_id")) != row.id:
        return _error_page(AUTHORIZATION_EXPIRED, 400)
    session = _session_row_from_cookie(request, _current_host(request))
    if session is None:
        return _error_page(SESSION_MISSING, 401)
    decision = form.get("decision")
    if decision == DECISION_ALLOW:
        approval = approve(
            row.id,
            nonce,
            session.account_id,
            _uuid_or_none(form.get("workspace_id")),
            now=now,
        )
        if isinstance(approval, OAuthError):
            response = _refusal(approval)
            if approval.error == WORKSPACE_UNSELECTED:
                # The row is decided; the handle is spent.
                _clearing_request_cookie(response, config)
            return response
        location = _approved_location(approval, surface)
    elif decision == DECISION_DENY:
        denial = deny(row.id, nonce, session.account_id, now=now)
        if isinstance(denial, OAuthError):
            return _refusal(denial)
        location = _with_query(
            denial.redirect_uri,
            {"error": "access_denied", "state": denial.state, "iss": surface.issuer},
        )
    else:
        return _error_page(INVALID_REQUEST, 400)
    return _clearing_request_cookie(_redirect(location), config)


def _approved_location(approval: Approval, surface: OAuthSurface) -> str:
    return _with_query(
        approval.redirect_uri,
        {"code": approval.code, "state": approval.state, "iss": surface.issuer},
    )


@router.post(CONSENT_PATH, include_in_schema=False)
async def consent_decision(request: Request) -> Response:
    resolved = _resolved(request)
    if resolved is None:
        return _not_found_page()
    surface, config = resolved
    refused_origin = _check_origin(request, config)
    if refused_origin is not None:
        return refused_origin
    if not _is_form(request):
        return _error_page(INVALID_REQUEST, 400)
    raw = await _bounded_body(request)
    form = None if raw is None else _parse_form(raw)
    if form is None or not set(form) <= _CONSENT_FIELDS:
        return _error_page(INVALID_REQUEST, 400)
    return await run_in_threadpool(_decide, request, surface, config, form)


__all__ = [
    "AUTHORIZE_PATH",
    "CONSENT_PATH",
    "DECISION_ALLOW",
    "DECISION_DENY",
    "FORM_MEDIA_TYPE",
    "PAGE_HEADERS",
    "REGISTER_PATH",
    "TOKEN_PATH",
    "UNSUPPORTED_GRANT_TYPE",
    "router",
    "surface_or_404",
]
