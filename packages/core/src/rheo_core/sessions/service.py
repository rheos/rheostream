"""Session issuance and lifecycle (C7a): creation, per-host secrets, the idle/absolute
touch, revocation, and the active-workspace switch.

``rheo_core.boundary.factories.context_from_session`` is the read side (a secret and a
host in, a context or a refusal out); this module is the write side (a session comes
into being, moves, and eventually ends).
"""

import hashlib
import logging
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Connection

from rheo_core.boundary.context import NOT_A_MEMBER, SESSION_MISSING, Refusal
from rheo_core.settings import resolve
from rheo_core.storage.control_plane import (
    SessionRow,
    get_membership,
    get_session,
    get_workspace,
    insert_session,
    insert_session_secret,
    list_memberships,
    list_recent_session_workspaces,
    revoke_session,
    set_active_workspace,
    touch_session,
)
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.postgres import get_backend

logger = logging.getLogger("rheo_core.sessions")

_HOST_SECRET_BYTES: int = 32  # 256 random bits


def _now() -> datetime:
    return datetime.now(UTC)


def _hash_secret(secret: bytes) -> bytes:
    return hashlib.sha256(secret).digest()


def _session_policy() -> tuple[int, int]:
    """``(session_idle_days, session_max_days)``, read fresh from current settings."""
    settings = resolve()
    return (
        settings.get_int("identity.session_idle_days"),
        settings.get_int("identity.session_max_days"),
    )


def _compute_expiry(*, now: datetime, created_at: datetime) -> datetime:
    """The session's effective deadline: the earlier of the idle window measured from
    ``now`` and the absolute window measured from ``created_at``. Read from current
    settings on every call (not cached on the row), so a deployment that tightens
    either limit takes effect on the session's very next touch rather than only for
    sessions created after the change.
    """
    idle_days, max_days = _session_policy()
    idle_deadline = now + timedelta(days=idle_days)
    absolute_deadline = created_at + timedelta(days=max_days)
    return min(idle_deadline, absolute_deadline)


def _is_active_workspace(connection: Connection, workspace_id: UUID) -> bool:
    """The workspace-active check ``context_from_session`` will run on this session's
    first request (``boundary.factories._active_workspace_modules``): the registry row
    exists and its state is ``active``."""
    row = get_workspace(connection, workspace_id)
    return row is not None and row.state is WorkspaceState.ACTIVE


def _default_workspace(connection: Connection, account_id: UUID) -> UUID | None:
    """The workspace a new session for ``account_id`` starts in (issue #157).

    The last-used workspace first: the one an earlier session of this account was most
    recently pointed at, where the membership still exists and the workspace is still
    active. Failing that, the oldest membership whose workspace is active. With
    neither, ``None``: the session starts unselected, and the web shows a signed-in
    "no workspace yet" state. Everything is read on the caller's connection, so the
    membership and state checks share the session insert's transaction.
    """
    for workspace_id in list_recent_session_workspaces(
        connection, account_id=account_id
    ):
        if _is_active_workspace(connection, workspace_id):
            return workspace_id
    for membership in list_memberships(connection, account_id=account_id):
        if _is_active_workspace(connection, membership.workspace_id):
            return membership.workspace_id
    return None


def create_session(account_id: UUID) -> SessionRow:
    """A fresh session for ``account_id``, pointed at its default workspace.

    The default is ``_default_workspace``'s: last used, else the oldest active
    membership, else none (``active_workspace_id`` stays NULL and
    ``context_from_session`` refuses ``workspace_unselected``). Before #157 every
    session started unselected, and since the switcher only renders inside a
    signed-in shell, a fresh sign-in could never reach one.

    ``insert_session`` writes a placeholder ``expires_at`` (its own docstring says
    why); this immediately supersedes it, in the same transaction, with the real
    initial deadline computed from current settings — so nothing external ever
    observes the placeholder.
    """
    backend = get_backend()
    with backend.control_engine.begin() as connection:
        # Chosen before the insert so the new, still-unselected row can never be
        # its own "last used" candidate.
        default_workspace_id = _default_workspace(connection, account_id)
        row = insert_session(connection, account_id)
        expiry = _compute_expiry(now=row.created_at, created_at=row.created_at)
        touch_session(
            connection, row.id, last_seen_at=row.created_at, expires_at=expiry
        )
        if default_workspace_id is not None:
            set_active_workspace(connection, row.id, default_workspace_id)
            row = get_session(connection, row.id) or row
    return replace(row, last_seen_at=row.created_at, expires_at=expiry)


def mint_host_secret(session_id: UUID, host: str) -> bytes:
    """A fresh 256-bit secret for ``session_id`` on ``host``; only its SHA-256 is
    ever stored. Returns the raw secret once — the caller sets it in a host-only
    cookie immediately and this module never stores or logs it."""
    secret = secrets.token_bytes(_HOST_SECRET_BYTES)
    backend = get_backend()
    with backend.control_engine.begin() as connection:
        insert_session_secret(
            connection,
            secret_hash=_hash_secret(secret),
            session_id=session_id,
            host=host,
        )
    return secret


def touch(session_id: UUID) -> None:
    """Bump the idle window: ``last_seen_at`` to now, ``expires_at`` to the tighter
    of the idle and absolute deadlines under current settings. A no-op if the session
    no longer exists (nothing left to touch)."""
    backend = get_backend()
    with backend.control_engine.begin() as connection:
        row = get_session(connection, session_id)
        if row is None:
            return
        now = _now()
        expiry = _compute_expiry(now=now, created_at=row.created_at)
        touch_session(connection, session_id, last_seen_at=now, expires_at=expiry)


def revoke(session_id: UUID) -> None:
    """Revoke the session. Every host's secret now resolves to a revoked session, so
    logout on any one host ends the session everywhere at once."""
    backend = get_backend()
    with backend.control_engine.begin() as connection:
        revoke_session(connection, session_id)


def switch_workspace(session_id: UUID, target_workspace_id: UUID) -> Refusal | None:
    """Move the session's active workspace, after checking membership.

    Refuses ``not_a_member`` when the session's account holds no membership in
    ``target_workspace_id`` (criterion B3); refuses ``session_missing`` for a session
    id that no longer exists (should not happen — the caller already resolved a
    context from this same secret moments earlier — but this function does not trust
    that and checks). On success, updates ``active_workspace_id`` and logs the switch:
    the session id and the target workspace id only, never a secret value.
    """
    backend = get_backend()
    with backend.control_engine.begin() as connection:
        row = get_session(connection, session_id)
        if row is None:
            return Refusal(SESSION_MISSING)
        membership = get_membership(
            connection, account_id=row.account_id, workspace_id=target_workspace_id
        )
        if membership is None:
            return Refusal(NOT_A_MEMBER)
        set_active_workspace(connection, session_id, target_workspace_id)
    logger.info(
        "session_workspace_switched",
        extra={
            "session_id": str(session_id),
            "target_workspace_id": str(target_workspace_id),
        },
    )
    return None
