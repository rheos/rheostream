"""The OAuth connector flow, HTTP-free (issue #287): registration, the authorization
request and the consent decision. Code exchange and refresh follow in this module.

**One step, one control-plane transaction, one event row.** Every public step opens
``get_backend().control_engine.begin()`` once and writes its ``oauth_event`` row in
that same transaction.

**A refusal is returned, never raised.** Each step returns ``OAuthResult[T]``, either
its value or an :class:`OAuthError`. The error is built and returned *inside* the
``begin()`` block, so whatever the step wrote before refusing (a ``refused`` event
row, a decided authorization) commits with it. Only an unexpected exception rolls the
step back.

``error_description`` is a fixed string per error word and never echoes input. Event
rows carry ids and an outcome word only: no secret, no client-supplied text.
"""

import hashlib
import re
import secrets
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import Connection

from rheo_core.oauth.surface import OAuthSurface
from rheo_core.storage import oauth_store
from rheo_core.storage.control_plane import (
    SessionRow,
    get_workspace,
    list_memberships,
)
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.oauth_store import OAuthAuthorizationRow
from rheo_core.storage.postgres import get_backend

REQUEST_LIFETIME: Final = timedelta(minutes=10)
"""A pending authorization lives this long (the ``rheo_oauth_request`` Max-Age)."""
CODE_LIFETIME: Final = timedelta(seconds=60)
"""An authorization code lives this long (the ``session_grant`` bound)."""
EXPIRED_AUTHORIZATION_RETENTION: Final = timedelta(days=1)
"""Unredeemed authorization rows older than this are deleted at registration."""
REGISTRATION_WINDOW: Final = timedelta(hours=1)
STATE_MAX_LENGTH: Final = 1024
CLIENT_NAME_MAX_LENGTH: Final = 100
DEFAULT_CLIENT_NAME: Final = "unnamed client"

TOKEN_ENDPOINT_AUTH_METHOD: Final = "none"
GRANT_TYPES: Final = ("authorization_code", "refresh_token")
RESPONSE_TYPES: Final = ("code",)

_CODE_CHALLENGE: Final = re.compile(r"[A-Za-z0-9_-]{43,128}")

# Error words (RFC 6749, RFC 7591, and this flow's own page states).
INVALID_CLIENT_METADATA: Final = "invalid_client_metadata"
INVALID_REDIRECT_URI: Final = "invalid_redirect_uri"
TEMPORARILY_UNAVAILABLE: Final = "temporarily_unavailable"
INVALID_CLIENT: Final = "invalid_client"
INVALID_REQUEST: Final = "invalid_request"
UNSUPPORTED_RESPONSE_TYPE: Final = "unsupported_response_type"
INVALID_TARGET: Final = "invalid_target"
AUTHORIZATION_EXPIRED: Final = "authorization_expired"
NOT_A_MEMBER: Final = "not_a_member"
WORKSPACE_UNSELECTED: Final = "workspace_unselected"
ACCESS_DENIED: Final = "access_denied"

_DESCRIPTIONS: Final[Mapping[str, str]] = {
    INVALID_CLIENT_METADATA: "The registration body must be a JSON object.",
    INVALID_REDIRECT_URI: "A redirect URI is not one this server allows.",
    TEMPORARILY_UNAVAILABLE: "Registration is not available right now.",
    INVALID_CLIENT: "The client is not registered.",
    INVALID_REQUEST: "The authorization request is malformed.",
    UNSUPPORTED_RESPONSE_TYPE: "Only the code response type is supported.",
    INVALID_TARGET: "The requested resource is not this server.",
    AUTHORIZATION_EXPIRED: "This sign-in request has expired or was already used.",
    NOT_A_MEMBER: "The chosen workspace is not one of yours.",
    WORKSPACE_UNSELECTED: "Your account has no active workspace to connect.",
    ACCESS_DENIED: "The request was denied.",
}

# Event words and outcomes (the oauth_event CHECKs).
CLIENT_REGISTERED: Final = "client_registered"
AUTHORIZATION_GRANTED: Final = "authorization_granted"
AUTHORIZATION_DENIED: Final = "authorization_denied"
SUCCEEDED: Final = "succeeded"
REFUSED: Final = "refused"


@dataclass(frozen=True, slots=True)
class OAuthError:
    """A refusal. ``redirect`` says whether the route may send it to the client's
    redirect URI (with ``state`` and ``iss``) or must show it as a page."""

    error: str
    status: int
    redirect: bool
    redirect_uri: str | None = None
    state: str | None = None
    iss: str | None = None

    @property
    def error_description(self) -> str:
        return _DESCRIPTIONS[self.error]


type OAuthResult[T] = T | OAuthError


def _page(error: str, status: int = 400) -> OAuthError:
    return OAuthError(error=error, status=status, redirect=False)


def _sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


# --- registration (RFC 7591) ---------------------------------------------------------


def _sanitize_client_name(value: object) -> str:
    """Control and other non-printing characters stripped, cut to 100, a default when
    nothing is left. Applied here because the table has no length CHECK."""
    if not isinstance(value, str):
        return DEFAULT_CLIENT_NAME
    kept = "".join(ch for ch in value if not unicodedata.category(ch).startswith("C"))
    kept = kept.strip()[:CLIENT_NAME_MAX_LENGTH].strip()
    return kept or DEFAULT_CLIENT_NAME


def _refuse_registration(
    conn: Connection, now: datetime, error: str, status: int
) -> OAuthError:
    oauth_store.insert_event(
        conn, event=CLIENT_REGISTERED, outcome=REFUSED, occurred_at=now
    )
    return _page(error, status)


def register_client(
    surface: OAuthSurface, *, metadata: object, source: str, now: datetime
) -> OAuthResult[dict[str, object]]:
    """Register a public client and return RFC 7591's 201 body with the *effective*
    metadata (never a secret).

    Order: count ``source``'s registrations in the last hour, clean up abandoned
    clients and stale authorization rows, refuse on the per-source limit or the
    counted-client cap, then validate.

    The per-source limit counts the ``oauth_client`` rows that still exist, so
    cleanup never deletes a client inside that hour: its cutoff is the earlier of
    ``now - abandoned_client_minutes`` and ``now - 1 h``. With
    ``abandoned_client_minutes`` below 60 an abandoned client therefore lingers up
    to the hour, but it stops counting toward the cap at its configured age
    (``count_counted_clients`` keeps the configured ``abandoned_before``)."""
    lifetimes = surface.lifetimes
    with get_backend().control_engine.begin() as conn:
        window_start = now - REGISTRATION_WINDOW
        recent = oauth_store.count_registrations_from(
            conn, source=source, since=window_start
        )
        abandoned_before = now - timedelta(minutes=lifetimes.abandoned_client_minutes)
        oauth_store.delete_abandoned_clients(
            conn, now=now, abandoned_before=min(abandoned_before, window_start)
        )
        oauth_store.delete_expired_authorizations(
            conn, before=now - EXPIRED_AUTHORIZATION_RETENTION
        )
        if recent >= lifetimes.registrations_per_source_per_hour:
            return _refuse_registration(conn, now, TEMPORARILY_UNAVAILABLE, 429)
        counted = oauth_store.count_counted_clients(
            conn, now=now, abandoned_before=abandoned_before
        )
        if counted >= lifetimes.max_clients:
            return _refuse_registration(conn, now, TEMPORARILY_UNAVAILABLE, 429)
        if not isinstance(metadata, Mapping):
            return _refuse_registration(conn, now, INVALID_CLIENT_METADATA, 400)
        requested = metadata.get("redirect_uris")
        allowed = set(surface.redirect_uris)
        if (
            not isinstance(requested, list)
            or not requested
            or not all(isinstance(uri, str) and uri in allowed for uri in requested)
        ):
            return _refuse_registration(conn, now, INVALID_REDIRECT_URI, 400)
        client = oauth_store.insert_client(
            conn,
            client_id=secrets.token_urlsafe(16),
            client_name=_sanitize_client_name(metadata.get("client_name")),
            registered_from=source,
            created_at=now,
            redirect_uris=requested,
        )
        oauth_store.insert_event(
            conn,
            event=CLIENT_REGISTERED,
            outcome=SUCCEEDED,
            occurred_at=now,
            client_id=client.client_id,
        )
        return {
            "client_id": client.client_id,
            "client_id_issued_at": int(now.timestamp()),
            "client_name": client.client_name,
            "redirect_uris": list(client.redirect_uris),
            "token_endpoint_auth_method": TOKEN_ENDPOINT_AUTH_METHOD,
            "grant_types": list(GRANT_TYPES),
            "response_types": list(RESPONSE_TYPES),
        }


# --- the authorization request ------------------------------------------------------


def begin_authorization(
    surface: OAuthSurface, params: Mapping[str, str], *, now: datetime
) -> OAuthResult[str]:
    """Validate an ``/authorize`` request and hold it server-side. Returns the
    ``rheo_oauth_request`` cookie nonce (the row stores only its SHA-256).

    Errors before the redirect URI is trusted are pages; after, they redirect with
    ``state`` and ``iss`` (an over-long ``state`` is not echoed back)."""
    with get_backend().control_engine.begin() as conn:
        client = oauth_store.get_client(conn, params.get("client_id", ""))
        if client is None:
            return _page(INVALID_CLIENT)
        redirect_uri = params.get("redirect_uri")
        if redirect_uri is None or redirect_uri not in client.redirect_uris:
            return _page(INVALID_REDIRECT_URI)
        state = params.get("state")

        def redirected(error: str, *, echo_state: bool = True) -> OAuthError:
            return OAuthError(
                error=error,
                status=302,
                redirect=True,
                redirect_uri=redirect_uri,
                state=state if echo_state else None,
                iss=surface.issuer,
            )

        if params.get("response_type") != "code":
            return redirected(UNSUPPORTED_RESPONSE_TYPE)
        challenge = params.get("code_challenge")
        if (
            challenge is None
            or _CODE_CHALLENGE.fullmatch(challenge) is None
            or params.get("code_challenge_method") != "S256"
        ):
            return redirected(INVALID_REQUEST)
        resource = params.get("resource")
        if resource is None or not surface.resource_matches(resource):
            return redirected(INVALID_TARGET)
        if state is not None and len(state) > STATE_MAX_LENGTH:
            return redirected(INVALID_REQUEST, echo_state=False)
        nonce = secrets.token_bytes(32).hex()
        oauth_store.insert_authorization(
            conn,
            request_hash=_sha256(nonce),
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            state=state,
            created_at=now,
            expires_at=now + REQUEST_LIFETIME,
        )
        return nonce


def _pending(
    conn: Connection, request_nonce: str | None, now: datetime, *, for_update: bool
) -> OAuthAuthorizationRow | None:
    if not request_nonce:
        return None
    row = oauth_store.get_authorization_by_request_hash(
        conn, _sha256(request_nonce), for_update=for_update
    )
    if row is None or row.decided_at is not None or now >= row.expires_at:
        return None
    return row


def pending(request_nonce: str | None, now: datetime) -> OAuthAuthorizationRow | None:
    """The undecided row behind the cookie nonce, live while ``now < expires_at``;
    ``None`` for a missing cookie, an unknown nonce, a decided or an expired row."""
    with get_backend().control_engine.connect() as conn:
        return _pending(conn, request_nonce, now, for_update=False)


# --- consent -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkspaceChoice:
    workspace_id: UUID
    display_name: str


@dataclass(frozen=True, slots=True)
class ConsentChoices:
    """The workspaces a grant may bind to, and the one preselected (``None`` only
    when there are none)."""

    workspaces: tuple[WorkspaceChoice, ...]
    default: UUID | None


def _choices(
    conn: Connection, account_id: UUID, active_workspace_id: UUID | None
) -> ConsentChoices:
    workspaces: list[WorkspaceChoice] = []
    for membership in list_memberships(conn, account_id=account_id):
        workspace = get_workspace(conn, membership.workspace_id)
        if workspace is not None and workspace.state is WorkspaceState.ACTIVE:
            workspaces.append(WorkspaceChoice(workspace.id, workspace.display_name))
    ids = [choice.workspace_id for choice in workspaces]
    if not ids:
        default = None
    elif active_workspace_id in ids:
        default = active_workspace_id
    else:
        default = ids[0]
    return ConsentChoices(workspaces=tuple(workspaces), default=default)


def consent_choices(account_id: UUID, session: SessionRow) -> ConsentChoices:
    """The account's memberships whose workspace is active, defaulting to the
    session's active workspace when it is among them, else the first."""
    with get_backend().control_engine.connect() as conn:
        return _choices(conn, account_id, session.active_workspace_id)


@dataclass(frozen=True, slots=True)
class Approval:
    """What the route redirects with: ``redirect_uri?code&state&iss``."""

    code: str
    redirect_uri: str
    state: str | None


@dataclass(frozen=True, slots=True)
class Denial:
    """What the route redirects with: ``redirect_uri?error=access_denied&state&iss``."""

    redirect_uri: str
    state: str | None


def approve(
    row_id: UUID,
    request_nonce: str | None,
    account_id: UUID,
    workspace_id: UUID | None,
    *,
    now: datetime,
) -> OAuthResult[Approval]:
    """Approve the pending row the cookie names, binding it to ``workspace_id``.

    The row is re-read through :func:`pending`'s rule ``FOR UPDATE``; unless that
    yields the row with id ``row_id`` the answer is ``authorization_expired`` and
    nothing is written, so a decided, expired or other-browser row is never
    approved and two racing Allows issue one code. An account with no active
    workspace gets ``workspace_unselected`` with the row marked decided; a workspace
    outside the choices gets ``not_a_member`` with nothing written."""
    with get_backend().control_engine.begin() as conn:
        row = _pending(conn, request_nonce, now, for_update=True)
        if row is None or row.id != row_id:
            return _page(AUTHORIZATION_EXPIRED)
        choices = _choices(conn, account_id, None)
        if not choices.workspaces:
            oauth_store.record_authorization_decision(
                conn, row.id, decided_at=now, account_id=account_id, workspace_id=None
            )
            oauth_store.insert_event(
                conn,
                event=AUTHORIZATION_GRANTED,
                outcome=REFUSED,
                occurred_at=now,
                client_id=row.client_id,
                account_id=account_id,
            )
            return _page(WORKSPACE_UNSELECTED, 403)
        if workspace_id not in {choice.workspace_id for choice in choices.workspaces}:
            return _page(NOT_A_MEMBER, 403)
        code = secrets.token_urlsafe(32)
        oauth_store.record_authorization_decision(
            conn,
            row.id,
            decided_at=now,
            account_id=account_id,
            workspace_id=workspace_id,
            code_hash=_sha256(code),
            code_expires_at=now + CODE_LIFETIME,
        )
        oauth_store.insert_event(
            conn,
            event=AUTHORIZATION_GRANTED,
            outcome=SUCCEEDED,
            occurred_at=now,
            client_id=row.client_id,
            account_id=account_id,
            workspace_id=workspace_id,
        )
        return Approval(code=code, redirect_uri=row.redirect_uri, state=row.state)


def deny(
    row_id: UUID, request_nonce: str | None, account_id: UUID, *, now: datetime
) -> OAuthResult[Denial]:
    """Deny the pending row the cookie names: the same re-read and
    ``authorization_expired`` refusal as :func:`approve`, then ``decided_at`` and a
    committed ``authorization_denied`` row."""
    with get_backend().control_engine.begin() as conn:
        row = _pending(conn, request_nonce, now, for_update=True)
        if row is None or row.id != row_id:
            return _page(AUTHORIZATION_EXPIRED)
        oauth_store.record_authorization_decision(
            conn, row.id, decided_at=now, account_id=account_id, workspace_id=None
        )
        oauth_store.insert_event(
            conn,
            event=AUTHORIZATION_DENIED,
            outcome=REFUSED,
            occurred_at=now,
            client_id=row.client_id,
            account_id=account_id,
        )
        return Denial(redirect_uri=row.redirect_uri, state=row.state)
