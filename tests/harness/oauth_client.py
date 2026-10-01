"""A test-double OAuth connector client over the real ASGI app (issue #287).

:class:`OAuthTestClient` speaks to ``rheo_app_core.main.app`` the way a connector
client does: it registers at the surface's ``registration_endpoint``, redeems and
refreshes at its ``token_endpoint`` and calls the ``mcp`` surface at its
``resource``, every URL taken from the :class:`~rheo_core.oauth.OAuthSurface` the
test resolved, never typed out. It records every secret it is handed or makes
(access and refresh values, codes, PKCE verifiers and challenges) in
:attr:`OAuthTestClient.issued`, so a later test can scan logs, rows and bodies for
any of them.

``httpx2`` rather than ``httpx``: one client drives both the FastAPI routes and the
MCP transport, and the MCP SDK's transport takes only ``httpx2`` (see the dev
dependency note in ``pyproject.toml``).

:func:`oauth_client` runs the app's lifespan (the ``mcp`` mount exists only inside
one) and sets the ASGI client address, which is the registration ``source`` the
per-source limit counts; tests pass a fresh fictional address so the shared control
plane's other registrations do not count against them.
"""

import base64
import contextlib
import hashlib
import html
import re
import secrets
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import httpx2
from rheo_app_core.main import app, lifespan
from rheo_core.oauth import OAuthSurface

FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
"""The shipped default allowlist entry (a public constant)."""
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@dataclass
class IssuedSecrets:
    """Every secret value this client saw, by kind."""

    access: list[str] = field(default_factory=list)
    refresh: list[str] = field(default_factory=list)
    code: list[str] = field(default_factory=list)
    verifier: list[str] = field(default_factory=list)
    challenge: list[str] = field(default_factory=list)

    def all(self) -> list[str]:
        return [
            *self.access,
            *self.refresh,
            *self.code,
            *self.verifier,
            *self.challenge,
        ]


def pkce_pair() -> tuple[str, str]:
    """A fresh RFC 7636 verifier and its S256 challenge, computed here, not by the
    server."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class OAuthTestClient:
    """The connector side of the flow, against one configured surface."""

    def __init__(self, http: httpx2.AsyncClient, surface: OAuthSurface) -> None:
        self.http = http
        self.surface = surface
        self.issued = IssuedSecrets()
        self.callback_urls: list[str] = []

    # --- recording -----------------------------------------------------------------

    def new_pkce(self) -> tuple[str, str]:
        """A verifier/challenge pair, recorded."""
        verifier, challenge = pkce_pair()
        self.issued.verifier.append(verifier)
        self.issued.challenge.append(challenge)
        return verifier, challenge

    def record_code(self, code: str) -> None:
        self.issued.code.append(code)

    def _record_tokens(self, response: httpx2.Response) -> None:
        if response.status_code != 200:
            return
        body = response.json()
        if isinstance(body.get("access_token"), str):
            self.issued.access.append(body["access_token"])
        if isinstance(body.get("refresh_token"), str):
            self.issued.refresh.append(body["refresh_token"])

    # --- registration and the token endpoint ---------------------------------------

    async def register(self, metadata: object) -> httpx2.Response:
        """``POST`` ``metadata`` as JSON to the registration endpoint."""
        return await self.http.post(self.surface.registration_endpoint, json=metadata)

    async def register_raw(
        self, body: bytes, *, content_type: str = "application/json"
    ) -> httpx2.Response:
        return await self.http.post(
            self.surface.registration_endpoint,
            content=body,
            headers={"Content-Type": content_type},
        )

    async def token(
        self,
        form: Mapping[str, str] | None = None,
        *,
        body: bytes | None = None,
        content_type: str = FORM_CONTENT_TYPE,
        url: str | None = None,
    ) -> httpx2.Response:
        """``POST`` to the token endpoint: ``form`` encoded, or a raw ``body`` for
        the malformed cases. Successful answers' values are recorded."""
        payload = body if body is not None else urlencode(dict(form or {})).encode()
        response = await self.http.post(
            url or self.surface.token_endpoint,
            content=payload,
            headers={"Content-Type": content_type},
        )
        self._record_tokens(response)
        return response

    async def redeem(
        self, code: str, client_id: str, redirect_uri: str, verifier: str
    ) -> httpx2.Response:
        return await self.token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
        )

    async def refresh(self, refresh_token: str, client_id: str) -> httpx2.Response:
        return await self.token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            }
        )

    # --- the mcp surface -----------------------------------------------------------

    async def mcp_call(
        self,
        access_token: str | None,
        method: str = "tools/list",
        params: Mapping[str, object] | None = None,
    ) -> httpx2.Response:
        """One JSON-RPC request to the surface's resource, with the bearer."""
        headers = dict(MCP_HEADERS)
        if access_token is not None:
            headers["Authorization"] = f"Bearer {access_token}"
        return await self.http.post(
            self.surface.resource,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": dict(params or {}),
            },
            headers=headers,
        )

    # --- browser half: the authorize, login and consent chain ----------------------
    # The same ``http`` client is the browser: its cookie jar holds the host-only
    # ``rheo_oauth_request`` and ``rheo_session`` cookies exactly as a browser
    # would. The identity provider is whatever the test overrode
    # ``auth_routes.resolve_identity_provider`` with (``FixedIdentityProvider``),
    # so the login hop never leaves the app.

    @property
    def consent_endpoint(self) -> str:
        """The consent URL, a sibling of the advertised authorization endpoint."""
        return urljoin(self.surface.authorization_endpoint, "consent")

    @property
    def identity_origin(self) -> str:
        """The ``Origin`` a browser on the identity host sends."""
        parts = urlsplit(self.surface.authorization_endpoint)
        return f"{parts.scheme}://{parts.netloc}"

    def authorize_params(
        self,
        client_id: str,
        *,
        redirect_uri: str = CLAUDE_CALLBACK,
        state: str | None = "opaque-state",
        challenge: str | None = None,
        **overrides: str | None,
    ) -> dict[str, str]:
        """A well-formed authorization request (PKCE S256, ``resource``) with a
        freshly recorded challenge unless ``challenge`` is given; an ``overrides``
        value of ``None`` drops that parameter."""
        if challenge is None:
            _, challenge = self.new_pkce()
        params: dict[str, str | None] = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": self.surface.resource,
            "state": state,
            **overrides,
        }
        return {key: value for key, value in params.items() if value is not None}

    async def authorize(self, params: Mapping[str, str]) -> httpx2.Response:
        """``GET`` the authorization endpoint; redirects are not followed."""
        return await self.http.get(self.surface.authorization_endpoint, params=params)

    async def follow(self, response: httpx2.Response) -> httpx2.Response:
        """``GET`` the ``Location`` of a redirect, resolved against its URL."""
        location = urljoin(str(response.url), response.headers["location"])
        return await self.http.get(location)

    async def to_consent(self, params: Mapping[str, str]) -> httpx2.Response:
        """Authorize, then :meth:`follow_chain`."""
        return await self.follow_chain(await self.authorize(params))

    async def follow_chain(self, response: httpx2.Response) -> httpx2.Response:
        """Follow the redirects a browser follows on the identity host (consent,
        the ``/auth/login`` hop, the provider's callback, back to consent) until a
        response that is not a redirect, or a redirect off the identity host (the
        client's redirect URI). Every provider callback URL seen is kept in
        :attr:`callback_urls` (without its query)."""
        hops = 0
        while response.status_code in (302, 303) and hops < 6:
            location = urljoin(str(response.url), response.headers["location"])
            if not location.startswith(self.identity_origin):
                break
            if urlsplit(location).path.endswith("/callback"):
                self.callback_urls.append(location.split("?", 1)[0])
            response = await self.http.get(location)
            hops += 1
        return response

    async def decide(
        self,
        decision: str,
        *,
        request_id: str,
        account_id: str | None = None,
        workspace_id: str | None = None,
        origin: str | None = "",
    ) -> httpx2.Response:
        """``POST`` the consent form. ``origin`` ``""`` (the default) is the
        identity host's own; ``None`` sends no ``Origin`` header."""
        form = {"request_id": request_id, "decision": decision}
        if account_id is not None:
            form["account_id"] = account_id
        if workspace_id is not None:
            form["workspace_id"] = workspace_id
        headers = {"Content-Type": FORM_CONTENT_TYPE}
        if origin is not None:
            headers["Origin"] = origin or self.identity_origin
        return await self.http.post(
            self.consent_endpoint, content=urlencode(form).encode(), headers=headers
        )

    def code_from(self, response: httpx2.Response) -> str:
        """The ``code`` on an approval redirect, recorded."""
        code = redirect_query(response)["code"]
        self.record_code(code)
        return code

    async def obtain_code(
        self, client_id: str, *, workspace_id: str | None = None
    ) -> tuple[str, str]:
        """Authorize through consent and Allow; returns ``(code, verifier)``."""
        verifier, challenge = self.new_pkce()
        page = await self.to_consent(
            self.authorize_params(client_id, challenge=challenge)
        )
        assert page.status_code == 200, page.text
        fields = consent_fields(page.text)
        allowed = await self.decide(
            "allow",
            request_id=fields["request_id"],
            account_id=fields["account_id"],
            workspace_id=workspace_id or fields["workspace_id"],
        )
        assert allowed.status_code == 302, allowed.text
        return self.code_from(allowed), verifier

    async def connect(
        self, *, client_name: str = "Example connector"
    ) -> tuple[str, httpx2.Response]:
        """Register, authorize, consent, Allow and redeem: ``(client_id, token
        response)``."""
        registered = await self.register(
            {"redirect_uris": [CLAUDE_CALLBACK], "client_name": client_name}
        )
        assert registered.status_code == 201, registered.text
        client_id = str(registered.json()["client_id"])
        code, verifier = await self.obtain_code(client_id)
        token = await self.redeem(code, client_id, CLAUDE_CALLBACK, verifier)
        return client_id, token


def redirect_query(response: httpx2.Response) -> dict[str, str]:
    """The query parameters of a redirect's ``Location``."""
    return dict(parse_qsl(urlsplit(response.headers["location"]).query))


_HIDDEN = re.compile(r'<input type="hidden" name="(\w+)" value="([^"]*)">')
_OPTION = re.compile(r'<option value="([^"]*)"( selected)?>')


def consent_fields(page: str) -> dict[str, str]:
    """The consent form's ``request_id`` and preselected ``workspace_id``."""
    fields = {name: html.unescape(value) for name, value in _HIDDEN.findall(page)}
    for value, selected in _OPTION.findall(page):
        if selected or "workspace_id" not in fields:
            fields["workspace_id"] = html.unescape(value)
    return fields


def consent_options(page: str) -> list[str]:
    """The workspace ids the consent page's picker offers, in order."""
    return [html.unescape(value) for value, _ in _OPTION.findall(page)]


@contextlib.asynccontextmanager
async def oauth_client(
    surface: OAuthSurface, *, client_address: str = "192.0.2.1"
) -> AsyncIterator[OAuthTestClient]:
    """An :class:`OAuthTestClient` over ``app`` with its lifespan running, whose
    requests arrive from ``client_address`` (a TEST-NET address by default)."""
    async with lifespan(app):
        transport = httpx2.ASGITransport(app=app, client=(client_address, 40000))
        async with httpx2.AsyncClient(transport=transport) as http:
            yield OAuthTestClient(http, surface)


__all__ = [
    "CLAUDE_CALLBACK",
    "FORM_CONTENT_TYPE",
    "IssuedSecrets",
    "OAuthTestClient",
    "oauth_client",
    "consent_fields",
    "consent_options",
    "pkce_pair",
    "redirect_query",
]
