"""The connector OAuth callback on the identity host: the one route in this module.

When a module connects to a remote resource as an OAuth client
(``rheo_core.connectors.oauth``), the owner's browser consents at the provider and
is sent back here: ``GET <identity prefix>/connector/{module_id}/callback``. The
registered redirect URI is ``oauth.redirect_uri(module_id)``, built by ``url_for``
from the routing settings, so the URL the provider calls is exactly this route.
There is no client document: the client identity comes from dynamic registration,
so nothing else is served under ``/connector``.

**404 unless configured, and on the identity host.** :func:`_configured` asks
``oauth.enabled()`` on every request (the operator switch
``connectors.oauth_client.enabled`` and ``routing.identity.path`` equal to the served
prefix) and, in subdomain mode, compares the request's host with the identity host,
the rule ``oauth_routes`` applies. Otherwise the answer is the 404 an unrouted path
gets, so a deployment that has not opted in shows no trace of the callback.

**The route path.** Registered under :data:`SERVED_IDENTITY_PATH`, as
``oauth_routes`` registers its paths; ``surface_guard`` maps a relocated identity
prefix onto it, and ``oauth.enabled()`` is false under any other prefix, so the
route and the registered redirect URI always agree.

**What is read.** Only ``state``, ``code``, ``error`` and ``iss`` from the query,
each at most :data:`MAX_PARAMETER_LENGTH` characters and present at most once; any
other parameter (``error_description`` included) is never read. An over-long or
repeated value is the refusal page with nothing dispatched.

**Authentication.** Only the owner's existing session cookie on this host, resolved
by ``context_from_session`` (entry ``web``). An unknown module, a module that
declares no ``authorization_completion``, or no valid session is the refusal page
with nothing dispatched and nothing consumed. Otherwise the module's declared
completion operation is dispatched through the normal dispatcher with the bounded
parameters that were present. That operation is owner-only (checked when the
manifest is built) and consumes its single-use, account-bound state, so a repeat or
a prefetch of the same URL is refused by it; this route adds no CSRF machinery of
its own.

**What is answered.** One of two fixed documents and nothing else: the success page
only when the operation succeeded with a result whose ``state`` is ``connected``,
the refusal page for every other outcome, refusal or exception. No request value,
refusal text or provider text reaches a response; neither page redirects. Both are
served with ``oauth_routes.PAGE_HEADERS`` (``no-store``, no framing, nothing loads).
"""

import logging
from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from rheo_contracts import Entry, WorkspaceContext
from rheo_core.boundary.factories import context_from_session
from rheo_core.connectors import oauth
from rheo_core.modules import loaded_manifests
from rheo_core.oauth.surface import SERVED_IDENTITY_PATH
from rheo_core.operations import dispatch
from rheo_core.routing import RoutingMode
from rheo_core.sessions.cookies import SESSION_COOKIE

from rheo_app_core.auth_routes import _current_host, identity_host, normalize_host
from rheo_app_core.oauth_routes import PAGE_HEADERS
from rheo_app_core.routing import routing_config
from rheo_app_core.startup import CONSUMERS

router = APIRouter()

_ROUTE_PREFIX: Final = SERVED_IDENTITY_PATH
CALLBACK_PATH: Final = f"{_ROUTE_PREFIX}/connector/{{module_id}}/callback"

CALLBACK_PARAMETERS: Final = ("state", "code", "error", "iss")
"""The only query parameters the callback reads."""

MAX_PARAMETER_LENGTH: Final = 2048
"""The longest value accepted for any one of :data:`CALLBACK_PARAMETERS`. A state
is 43 characters and an issuer is a URL; a provider's authorization code is well
under this."""

CONNECTED: Final = "connected"
"""The completion result ``state`` that selects the success page."""

UNEXPECTED_ERROR: Final = "connector_callback_unexpected_error %s"

CONNECTED_TEXT: Final = "Job feed connected"
REFUSED_TEXT: Final = "Authorization could not be completed; start again from Leads"


def _fixed_document(text: str) -> str:
    """A static page whose title and only paragraph are ``text`` (a constant)."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{text}</title>\n"
        "</head>\n"
        "<body>\n"
        "<main>\n"
        f"<p>{text}</p>\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


CONNECTED_PAGE: Final = _fixed_document(CONNECTED_TEXT)
REFUSED_PAGE: Final = _fixed_document(REFUSED_TEXT)

_log = logging.getLogger("rheo_app_core.connector_oauth_routes")


def _not_found() -> Response:
    # The body FastAPI gives an unrouted path, so an unconfigured callback is one.
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _connected() -> Response:
    return HTMLResponse(CONNECTED_PAGE, status_code=200, headers=dict(PAGE_HEADERS))


def _refused() -> Response:
    return HTMLResponse(REFUSED_PAGE, status_code=400, headers=dict(PAGE_HEADERS))


def _configured(request: Request) -> bool:
    """Whether the connector client is configured and (subdomain mode) this request
    is on the identity host. Path mode has one host, so any host is it."""
    if not oauth.enabled():
        return False
    config = routing_config()
    if config.mode is RoutingMode.PATH:
        return True
    return _current_host(request) == normalize_host(identity_host(config))


def _bounded_parameters(request: Request) -> dict[str, str] | None:
    """The present callback parameters, or ``None`` when one is repeated or longer
    than :data:`MAX_PARAMETER_LENGTH`. Every other parameter is left unread."""
    found: dict[str, str] = {}
    for name in CALLBACK_PARAMETERS:
        values = request.query_params.getlist(name)
        if not values:
            continue
        if len(values) > 1 or len(values[0]) > MAX_PARAMETER_LENGTH:
            return None
        found[name] = values[0]
    return found


def _session_context(request: Request) -> WorkspaceContext | None:
    """The owner's web context from the session cookie on this host, or ``None``."""
    secret_hex = request.cookies.get(SESSION_COOKIE)
    if not secret_hex:
        return None
    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError:
        return None
    ctx = context_from_session(secret, _current_host(request), Entry.WEB)
    return ctx if isinstance(ctx, WorkspaceContext) else None


def _complete(ctx: WorkspaceContext, operation: str, payload: dict[str, str]) -> bool:
    """Dispatch ``operation``; true only for a success whose result is connected."""
    outcome = dispatch(ctx, operation, payload, consumers=CONSUMERS)
    if not outcome.ok or outcome.result is None:
        return False
    return getattr(outcome.result, "state", None) == CONNECTED


@router.get(CALLBACK_PATH, include_in_schema=False)
def callback(request: Request, module_id: str) -> Response:
    if not _configured(request):
        return _not_found()
    payload = _bounded_parameters(request)
    if payload is None:
        return _refused()
    manifest = loaded_manifests().get(module_id)
    operation = None if manifest is None else manifest.authorization_completion
    if operation is None:
        return _refused()
    ctx = _session_context(request)
    if ctx is None:
        return _refused()
    connected = False
    unexpected: str | None = None
    try:
        connected = _complete(ctx, operation, payload)
    except Exception as error:  # any failure is the refusal page, nothing more
        unexpected = type(error).__name__
    if unexpected is not None:
        _log.warning(UNEXPECTED_ERROR, unexpected)
    return _connected() if connected else _refused()


__all__ = [
    "CALLBACK_PARAMETERS",
    "CALLBACK_PATH",
    "CONNECTED_PAGE",
    "CONNECTED_TEXT",
    "MAX_PARAMETER_LENGTH",
    "REFUSED_PAGE",
    "REFUSED_TEXT",
    "router",
]
