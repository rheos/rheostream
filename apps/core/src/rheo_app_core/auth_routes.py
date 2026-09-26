"""The public ``/auth/*`` routes (C7b): the CSRF-guarded OAuth login, the
subdomain-mode continue flow, logout, and the active-workspace switch.

Mounted on ``public_app`` (0a's ``app``, aliased in ``main.py``), served on every
application host — the route handlers never branch on which FastAPI *instance*
answered (there is only one), only on which *host* the request arrived on
(``Host`` header, normalised the same way at every call site: stripped of its
port, lowercased).

Every route resolves its own :class:`~rheo_core.routing.RoutingConfig` fresh, from
current deployment settings, on every request — never cached across requests, the
same "resolve at the moment of use" discipline the rest of this codebase applies
to secrets and settings; an operator's routing change takes effect on the very
next request.

The identity provider is a FastAPI dependency (:func:`resolve_identity_provider`)
rather than a module-level singleton so tests can substitute
``harness.identity.FixedIdentityProvider`` through ``app.dependency_overrides``
without this production module ever importing test-only code.
"""

import hashlib
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote, unquote, urlencode, urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from rheo_core.boundary.context import (
    INVALID_GRANT,
    INVALID_RETURN,
    INVALID_STATE,
    NOT_A_MEMBER,
    SESSION_MISSING,
    Refusal,
)
from rheo_core.identity import resolve_or_create
from rheo_core.identity.boundary import IdentityProvider
from rheo_core.identity.providers.github import GitHubProvider
from rheo_core.routing import (
    IDENTITY,
    SHELL,
    RoutingConfig,
    RoutingMode,
    identity_path,
    is_application_host,
    url_for,
)
from rheo_core.secrets import SecretStore
from rheo_core.sessions import (
    CONTINUE_COOKIE,
    OAUTH_STATE_COOKIE,
    SESSION_COOKIE,
    build_cookie,
    consume_grant,
    create_session,
    mint_host_secret,
    revoke,
    switch_workspace,
    write_grant,
)
from rheo_core.settings import resolve
from rheo_core.storage.control_plane import SessionRow, get_session_by_secret_hash
from rheo_core.storage.data_root import resolve_data_root
from rheo_core.storage.postgres import get_backend

from rheo_app_core.routing import routing_config

router = APIRouter()

_STATE_NONCE_BYTES = 24
ORIGIN_NOT_ALLOWED = "origin_not_allowed"
IDENTITY_PROVIDER_UNAVAILABLE = "identity_provider_unavailable"


class IdentityProviderUnavailable(Exception):
    """No identity provider is enabled, so ``/auth/login`` and ``/auth/callback``
    have nothing to sign anyone in with.

    A deployment state, not a bug: the flagship's first deploy runs with GitHub
    disabled on purpose. :func:`identity_provider_unavailable_handler` turns it into
    a typed ``503`` rather than letting FastAPI answer a bare ``500`` (issue #119)."""


# --- shared helpers -----------------------------------------------------------------


def normalize_host(host: str) -> str:
    """Strip a port and lowercase a host read from any header — every call site in
    this listener does this itself; nothing upstream is trusted to have done it.

    Trailing dots are stripped too: ``example.test.`` is the fully qualified
    spelling of ``example.test``, the same name to DNS, and a compare that kept the
    dot would let one host answer as two (issue #127: ``Host: mcp.example.test.``
    fell past the ``mcp`` host's router to FastAPI's routes)."""
    return host.rsplit(":", 1)[0].strip().rstrip(".").lower()


def _current_host(request: Request) -> str:
    host = request.headers.get("host") or request.url.hostname or ""
    return normalize_host(host)


def _identity_host(config: RoutingConfig) -> str:
    """The identity host for ``config``, in either mode.

    Path mode has one host total, so it *is* the identity host trivially; the
    ``/auth/continue`` handler branches only on comparing the current request's
    host against this value, which is why path mode needs no second
    implementation (``00-index.md`` § the C7 split; ``fit-check.md``).
    """
    if config.mode is RoutingMode.PATH:
        return config.base_host
    return f"{config.surfaces.identity.host}.{config.base_host}"


def _routing_config() -> RoutingConfig:
    """Module surfaces included: without them a module's host is not an
    application host here, and ``/auth/continue`` refuses its ``return``
    (issue #119)."""
    return routing_config()


def _return_host(return_target: str) -> str:
    return normalize_host(urlsplit(return_target).hostname or "")


def _path_and_query(url: str) -> str:
    """The same-host tail of ``url``: its path, and its query string if any."""
    parts = urlsplit(url)
    tail = parts.path or "/"
    return f"{tail}?{parts.query}" if parts.query else tail


def _refused(state: str, *, status_code: int) -> JSONResponse:
    return JSONResponse({"state": state}, status_code=status_code)


def _clear(name: str, config: RoutingConfig) -> str:
    """A ``Set-Cookie`` that overwrites ``name`` with an empty value: the same
    attributes ``build_cookie`` already gives it (07's function; not to be
    reimplemented here), so no stored hash or nonce can ever match an empty
    string again."""
    return build_cookie(name, "", scheme=config.scheme)


def _session_row_from_cookie(request: Request, host: str) -> SessionRow | None:
    """The live (unexpired, unrevoked) session row behind the ``rheo_session``
    cookie on ``host``, or ``None`` — for missing, malformed, unknown, expired or
    revoked alike. Deliberately not :func:`context_from_session`: that factory
    also demands a selected, active workspace and membership, which would make a
    freshly signed-in account unable to reach the shell to pick one in the first
    place."""
    secret_hex = request.cookies.get(SESSION_COOKIE)
    if not secret_hex:
        return None
    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError:
        return None
    backend = get_backend()
    with backend.control_engine.connect() as connection:
        row = get_session_by_secret_hash(
            connection, secret_hash=hashlib.sha256(secret).digest(), host=host
        )
    if row is None:
        return None
    if row.expires_at <= datetime.now(UTC) or row.revoked_at is not None:
        return None
    return row


def _check_origin(request: Request, config: RoutingConfig) -> Response | None:
    """Every state-changing ``/auth/*`` POST calls this first, before touching a
    cookie or a row. ``origin_not_allowed`` is a route-level HTTP guard, not a
    boundary :class:`Refusal` — it names no new state in
    ``rheo_core.boundary.context`` (07 already declared every constant this
    listener needs; this file does not touch that module)."""
    origin = request.headers.get("origin")
    host = normalize_host(urlsplit(origin).hostname or "") if origin else ""
    if not host or not is_application_host(config, host):
        return _refused(ORIGIN_NOT_ALLOWED, status_code=403)
    return None


def resolve_identity_provider() -> Iterator[IdentityProvider]:
    """The configured identity provider. A FastAPI **yield** dependency, not a
    singleton: tests override it (``app.dependency_overrides``) rather than this
    module ever importing a test double. ``yield`` rather than a plain ``return``
    so the httpx client this constructs per request is closed when the request
    finishes, instead of leaking a socket to the garbage collector — a bare
    ``return`` would build a fresh, never-closed client on every ``/auth/login``
    and ``/auth/callback``."""
    settings = resolve()
    if not settings.get_bool("identity.providers.github.enabled"):
        raise IdentityProviderUnavailable(
            "no identity provider is configured: "
            "identity.providers.github.enabled is false"
        )
    secret_store = SecretStore(resolve_data_root().path)
    client = httpx.Client()
    try:
        yield GitHubProvider(
            client_id=settings.get_str("identity.providers.github.client_id"),
            client_secret_ref=settings.get_str(
                "identity.providers.github.client_secret_ref"
            ),
            secret_store=secret_store,
            client=client,
        )
    finally:
        client.close()


# ``Annotated`` rather than a ``Depends(...)`` default value: the latter is a
# function call in an argument default, which is exactly the mistake bugbear's
# B008 exists to catch in general — it happens to be safe here only because
# FastAPI itself calls it once per request, but the annotated form sidesteps the
# question entirely and is the form FastAPI's own docs recommend.
IdentityProviderDep = Annotated[IdentityProvider, Depends(resolve_identity_provider)]


_HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})

# A constant, not a template: nothing from the request or the deployment reaches it,
# so there is nothing to escape, and it names no settings key, host or secret.
_IDENTITY_PROVIDER_UNAVAILABLE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign-in unavailable</title>
</head>
<body data-state="identity_provider_unavailable">
<main>
<h1>Sign-in is not available</h1>
<p>Sign-in is not available on this deployment because no identity provider is
enabled.</p>
<p><small>Error code: identity_provider_unavailable</small></p>
</main>
</body>
</html>
"""


def _prefers_html(request: Request) -> bool:
    """True when the ``Accept`` header ranks an HTML type above ``application/json``.

    A browser navigation lists ``text/html`` explicitly; an API client sends
    ``application/json``, ``*/*`` or nothing. Wildcards count for neither side, so
    ``*/*`` alone, a missing header and a tie all keep the JSON answer."""
    html = json = 0.0
    for part in request.headers.get("accept", "").split(","):
        media, *params = (piece.strip() for piece in part.split(";"))
        quality = 1.0
        for param in params:
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        media = media.lower()
        if media in _HTML_MEDIA_TYPES:
            html = max(html, quality)
        elif media == "application/json":
            json = max(json, quality)
    return html > json


def identity_provider_unavailable_handler(request: Request, exc: Exception) -> Response:
    """``IdentityProviderUnavailable`` as the typed ``503`` refusal.

    A browser navigation (``Accept`` preferring HTML, see :func:`_prefers_html`)
    gets a small readable page saying sign-in is unavailable (#119 item 2); every
    other client keeps the typed JSON body. Both carry ``Vary: Accept``.

    Registered on the app in ``main.py``: an ``APIRouter`` cannot hold exception
    handlers. The dependency raises before its ``yield``, so the exception reaches
    the app's handler like any route exception."""
    response: Response
    if _prefers_html(request):
        response = HTMLResponse(_IDENTITY_PROVIDER_UNAVAILABLE_PAGE, status_code=503)
    else:
        response = _refused(IDENTITY_PROVIDER_UNAVAILABLE, status_code=503)
    response.headers["vary"] = "Accept"
    return response


# --- GET /auth/login -------------------------------------------------------------


@router.get("/auth/login")
def login(
    request: Request,
    provider: IdentityProviderDep,
    return_target: str | None = Query(default=None, alias="return"),
) -> Response:
    config = _routing_config()
    target = return_target or url_for(config, SHELL, "/")
    if not is_application_host(config, _return_host(target)):
        return _refused(INVALID_RETURN, status_code=400)
    state = secrets.token_urlsafe(_STATE_NONCE_BYTES)
    redirect_target = provider.begin(state, url_for(config, IDENTITY, "/callback")).url
    response = RedirectResponse(redirect_target, status_code=302)
    # ``target`` is embedded in the cookie value, not just the query string this
    # provider happens to round-trip: a raw ``;``/``,`` in an attacker-crafted
    # ``return`` (its host already validated above, but not its path or query)
    # could otherwise truncate or smuggle a second cookie attribute. ``quote``
    # with an empty safe-set leaves only unreserved characters unescaped, so the
    # composite value can never contain one.
    cookie_value = f"{state}.{quote(target, safe='')}"
    response.headers.append(
        "set-cookie",
        build_cookie(OAUTH_STATE_COOKIE, cookie_value, scheme=config.scheme),
    )
    return response


# --- GET /auth/callback -----------------------------------------------------------


@router.get("/auth/callback")
def callback(
    request: Request,
    provider: IdentityProviderDep,
    code: str | None = None,
    state: str | None = None,
) -> Response:
    config = _routing_config()
    cookie_value = request.cookies.get(OAUTH_STATE_COOKIE)
    if not state or not cookie_value:
        return _refused(INVALID_STATE, status_code=400)
    cookie_state, _, encoded_return = cookie_value.partition(".")
    if not secrets.compare_digest(cookie_state, state):
        return _refused(INVALID_STATE, status_code=400)
    return_target = unquote(encoded_return)
    if not return_target or not is_application_host(
        config, _return_host(return_target)
    ):
        return_target = url_for(config, SHELL, "/")

    identity = provider.complete(dict(request.query_params), state)
    account = resolve_or_create(identity)
    if isinstance(account, Refusal):
        refusal_response = _refused(account.state, status_code=403)
        refusal_response.headers.append(
            "set-cookie", _clear(OAUTH_STATE_COOKIE, config)
        )
        return refusal_response

    session_row = create_session(account.id)
    host = _current_host(request)
    secret = mint_host_secret(session_row.id, host)
    response: Response = RedirectResponse(return_target, status_code=302)
    response.headers.append(
        "set-cookie", build_cookie(SESSION_COOKIE, secret.hex(), scheme=config.scheme)
    )
    response.headers.append("set-cookie", _clear(OAUTH_STATE_COOKIE, config))
    return response


# --- GET /auth/continue -----------------------------------------------------


@router.get("/auth/continue")
def continue_flow(
    request: Request,
    return_target: str = Query(alias="return"),
    nonce: str | None = None,
    code: str | None = None,
) -> Response:
    config = _routing_config()
    return_host = _return_host(return_target)
    # The first thing this route does on either role, before touching a cookie or
    # writing a grant: without this, the identity host — the one holding the
    # session cookie — would build a redirect target directly from an
    # attacker-controlled ``return`` with no check at all (B18).
    if not return_host or not is_application_host(config, return_host):
        return _refused(INVALID_RETURN, status_code=400)

    current_host = _current_host(request)
    identity_host = _identity_host(config)

    if current_host == identity_host:
        row = _session_row_from_cookie(request, current_host)
        if row is None:
            login_target = (
                f"{identity_path(config, '/login')}?"
                f"{urlencode({'return': return_target})}"
            )
            return RedirectResponse(login_target, status_code=302)
        if return_host == current_host:
            # No cross-host hop is needed: this host already holds the session
            # (always true in path mode, since every application host is this
            # one host — the "same code path" the split description asks for).
            return RedirectResponse(_path_and_query(return_target), status_code=302)
        if not nonce:
            return _refused(INVALID_GRANT, status_code=400)
        session_id: UUID = row.id
        grant_code = write_grant(session_id, return_host, nonce.encode("utf-8"))
        target = (
            f"{config.scheme}://{return_host}{identity_path(config, '/continue')}?"
            f"{urlencode({'code': grant_code, 'return': return_target})}"
        )
        return RedirectResponse(target, status_code=302)

    # An application host: consume the grant this host's own ``rheo_continue``
    # nonce was bound to.
    continue_cookie = request.cookies.get(CONTINUE_COOKIE)
    if not code or not continue_cookie:
        return _refused(INVALID_GRANT, status_code=400)
    grant = consume_grant(code, current_host, continue_cookie.encode("utf-8"))
    if isinstance(grant, Refusal):
        return _refused(grant.state, status_code=400)
    secret = mint_host_secret(grant.session_id, current_host)
    response = RedirectResponse(_path_and_query(return_target), status_code=302)
    response.headers.append(
        "set-cookie", build_cookie(SESSION_COOKIE, secret.hex(), scheme=config.scheme)
    )
    response.headers.append("set-cookie", _clear(CONTINUE_COOKIE, config))
    return response


# --- POST /auth/logout ------------------------------------------------------


@router.post("/auth/logout")
def logout(request: Request) -> Response:
    config = _routing_config()
    refusal = _check_origin(request, config)
    if refusal is not None:
        return refusal
    host = _current_host(request)
    row = _session_row_from_cookie(request, host)
    if row is not None:
        revoke(row.id)
    response = RedirectResponse(url_for(config, SHELL, "/login"), status_code=302)
    response.headers.append("set-cookie", _clear(SESSION_COOKIE, config))
    return response


# --- POST /auth/session/workspace -------------------------------------------


class WorkspaceSwitchInput(BaseModel):
    target_workspace_id: UUID


@router.post("/auth/session/workspace")
def session_workspace(request: Request, body: WorkspaceSwitchInput) -> Response:
    config = _routing_config()
    refusal = _check_origin(request, config)
    if refusal is not None:
        return refusal
    host = _current_host(request)
    row = _session_row_from_cookie(request, host)
    if row is None:
        return _refused(SESSION_MISSING, status_code=401)
    outcome = switch_workspace(row.id, body.target_workspace_id)
    if isinstance(outcome, Refusal):
        assert outcome.state == NOT_A_MEMBER
        return _refused(outcome.state, status_code=403)
    return JSONResponse(
        {"state": "ok", "active_workspace_id": str(body.target_workspace_id)}
    )
