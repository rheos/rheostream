"""Control-plane repositories: typed functions over the control tables, the only place
control-plane SQL lives.

0b1 shipped the ``account``, ``identity``, ``workspace`` and ``membership``
repositories. This run (0b2, C7a) added ``session``, ``session_secret``,
``session_grant`` and ``identity_provider``, with their callers and their tests.
C8 (09) adds ``access_token`` and ``access_token_operation`` below, in this same
file; their DDL was already whole in the control revision ``0001_control_plane``
from 0b1.

Every function takes the caller's ``Connection`` and runs inside the caller's
transaction; none commits. Ids are minted here with ``uuid7()`` in the creating
transaction (A2). Timestamps are ``timestamptz`` and are written from the process
clock in UTC.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

import psycopg.errors
from rheo_contracts import Role
from sqlalchemy import Connection, delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError

from rheo_core.refs import uuid7
from rheo_core.storage import control_tables as t
from rheo_core.storage.backend import (
    ACCOUNT_MISSING,
    IDENTITY_EXISTS,
    SLUG_TAKEN,
    WORKSPACE_MISSING,
    WORKSPACE_STATE,
    StorageRefusal,
)
from rheo_core.storage.control_tables import WorkspaceState


def _now() -> datetime:
    return datetime.now(UTC)


def _constraint_name(exc: IntegrityError) -> str:
    orig = exc.orig
    if isinstance(orig, psycopg.Error):
        return orig.diag.constraint_name or ""
    return ""


# --- account --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountRow:
    id: UUID
    display_name: str
    created_at: datetime
    disabled_at: datetime | None


def _account(row: RowMapping) -> AccountRow:
    return AccountRow(
        id=row["id"],
        display_name=row["display_name"],
        created_at=row["created_at"],
        disabled_at=row["disabled_at"],
    )


def insert_account(conn: Connection, *, display_name: str) -> AccountRow:
    """Insert one person. The id is minted here."""
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("an account needs a non-empty display name")
    row = AccountRow(uuid7(), display_name, _now(), None)
    conn.execute(
        insert(t.account).values(
            id=row.id,
            display_name=row.display_name,
            created_at=row.created_at,
            disabled_at=None,
        )
    )
    return row


def get_account(conn: Connection, account_id: UUID) -> AccountRow | None:
    found = (
        conn.execute(select(t.account).where(t.account.c.id == account_id))
        .mappings()
        .first()
    )
    return None if found is None else _account(found)


# --- identity -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IdentityRow:
    id: UUID
    account_id: UUID
    provider_id: str
    provider_subject: str
    email: str | None
    email_verified: bool
    created_at: datetime


def _identity(row: RowMapping) -> IdentityRow:
    return IdentityRow(
        id=row["id"],
        account_id=row["account_id"],
        provider_id=row["provider_id"],
        provider_subject=row["provider_subject"],
        email=row["email"],
        email_verified=row["email_verified"],
        created_at=row["created_at"],
    )


def insert_identity(
    conn: Connection,
    *,
    account_id: UUID,
    provider_id: str,
    provider_subject: str,
    email: str | None = None,
    email_verified: bool = False,
) -> IdentityRow:
    """One login identity for an existing account.

    ``account_missing`` when no such account; ``identity_exists`` when
    ``(provider_id, provider_subject)`` is already bound.
    """
    if not provider_id or not provider_subject:
        raise ValueError("an identity needs a provider id and a provider subject")
    row = IdentityRow(
        uuid7(),
        account_id,
        provider_id,
        provider_subject,
        email,
        email_verified,
        _now(),
    )
    try:
        conn.execute(
            insert(t.identity).values(
                id=row.id,
                account_id=row.account_id,
                provider_id=row.provider_id,
                provider_subject=row.provider_subject,
                email=row.email,
                email_verified=row.email_verified,
                created_at=row.created_at,
            )
        )
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.ForeignKeyViolation):
            raise StorageRefusal(
                ACCOUNT_MISSING, f"account {account_id} does not exist"
            ) from None
        if isinstance(exc.orig, psycopg.errors.UniqueViolation):
            raise StorageRefusal(
                IDENTITY_EXISTS,
                f"identity {provider_id}/{provider_subject} is already bound",
            ) from None
        raise
    return row


def get_identity(
    conn: Connection, *, provider_id: str, provider_subject: str
) -> IdentityRow | None:
    found = (
        conn.execute(
            select(t.identity).where(
                t.identity.c.provider_id == provider_id,
                t.identity.c.provider_subject == provider_subject,
            )
        )
        .mappings()
        .first()
    )
    return None if found is None else _identity(found)


# --- workspace ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkspaceRow:
    id: UUID
    slug: str
    display_name: str
    database_name: str
    state: WorkspaceState
    created_at: datetime
    state_changed_at: datetime
    state_detail: str | None


def _workspace(row: RowMapping) -> WorkspaceRow:
    return WorkspaceRow(
        id=row["id"],
        slug=row["slug"],
        display_name=row["display_name"],
        database_name=row["database_name"],
        state=WorkspaceState(row["state"]),
        created_at=row["created_at"],
        state_changed_at=row["state_changed_at"],
        state_detail=row["state_detail"],
    )


def insert_workspace_if_absent(
    conn: Connection,
    *,
    workspace_id: UUID,
    slug: str,
    display_name: str,
    database_name: str,
    state_detail: str,
) -> tuple[WorkspaceRow, bool]:
    """Insert the registry row in state ``provisioning``, or leave an existing row as
    it is (a retry of a crashed create). Returns the row now present and whether this
    call inserted it, so the caller can tell a fresh create from a retry.

    ``slug_taken`` when another workspace holds the slug. ``database_name`` is
    computed by provisioning and never accepted from any input; this function is the
    only writer of that column.
    """
    now = _now()
    statement = (
        pg_insert(t.workspace)
        .values(
            id=workspace_id,
            slug=slug,
            display_name=display_name,
            database_name=database_name,
            state=WorkspaceState.PROVISIONING.value,
            created_at=now,
            state_changed_at=now,
            state_detail=state_detail,
        )
        .on_conflict_do_nothing(index_elements=[t.workspace.c.id])
        .returning(t.workspace.c.id)
    )
    try:
        # RETURNING yields a row only when the insert happened; ``rowcount`` is not
        # a reliable signal for an ``ON CONFLICT DO NOTHING`` insert.
        inserted = conn.execute(statement).first() is not None
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.UniqueViolation) and (
            "slug" in _constraint_name(exc)
        ):
            raise StorageRefusal(
                SLUG_TAKEN, f"slug {slug!r} belongs to another workspace"
            ) from None
        raise
    row = get_workspace(conn, workspace_id)
    if row is None:  # pragma: no cover - the insert or the existing row is visible
        raise StorageRefusal(WORKSPACE_MISSING, f"workspace {workspace_id} vanished")
    return row, inserted


def get_workspace(conn: Connection, workspace_id: UUID) -> WorkspaceRow | None:
    found = (
        conn.execute(select(t.workspace).where(t.workspace.c.id == workspace_id))
        .mappings()
        .first()
    )
    return None if found is None else _workspace(found)


def list_workspaces(
    conn: Connection, *, state: WorkspaceState | None = None
) -> tuple[WorkspaceRow, ...]:
    """Every registry row, oldest first, optionally only those in ``state``."""
    statement = select(t.workspace).order_by(t.workspace.c.created_at, t.workspace.c.id)
    if state is not None:
        statement = statement.where(t.workspace.c.state == state.value)
    return tuple(_workspace(row) for row in conn.execute(statement).mappings())


def set_workspace_state(
    conn: Connection,
    workspace_id: UUID,
    *,
    state: WorkspaceState,
    state_detail: str | None,
    expected_states: frozenset[WorkspaceState] | None = None,
) -> None:
    """Write ``state`` and ``state_detail``; ``workspace_missing`` when no row.

    ``expected_states``, when given, makes this a compare-and-set: the ``UPDATE``
    only applies while the row's current state is one of ``expected_states``
    (issue #23). On a mismatch the row is re-selected to disambiguate: absent is
    still ``workspace_missing``; present but outside the set is ``workspace_state``,
    naming the row's actual state — this is what stops a lagging retry walker from
    stamping ``provisioning`` back over a leader's ``active``.
    """
    statement = (
        update(t.workspace)
        .where(t.workspace.c.id == workspace_id)
        .values(
            state=WorkspaceState(state).value,
            state_detail=state_detail,
            state_changed_at=_now(),
        )
    )
    if expected_states is not None:
        statement = statement.where(
            t.workspace.c.state.in_([s.value for s in expected_states])
        )
    result = conn.execute(statement)
    if result.rowcount == 1:
        return
    row = get_workspace(conn, workspace_id)
    if row is None:
        raise StorageRefusal(WORKSPACE_MISSING, f"workspace {workspace_id} has no row")
    raise StorageRefusal(
        WORKSPACE_STATE,
        f"workspace {workspace_id} is {row.state.value!r}, not one of "
        f"{sorted(s.value for s in expected_states or ())}",
        workspace_state=row.state.value,
    )


# --- membership -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MembershipRow:
    account_id: UUID
    workspace_id: UUID
    role: Role
    created_at: datetime


def _membership(row: RowMapping) -> MembershipRow:
    return MembershipRow(
        account_id=row["account_id"],
        workspace_id=row["workspace_id"],
        role=Role(row["role"]),
        created_at=row["created_at"],
    )


def insert_membership_if_absent(
    conn: Connection, *, account_id: UUID, workspace_id: UUID, role: Role
) -> MembershipRow:
    """The membership row, inserted if absent. Only ``owner`` and ``member`` are
    membership roles; ``operator`` and ``service`` are boundary roles, never rows.

    ``account_missing`` / ``workspace_missing`` when a foreign key is unmet.
    """
    if Role(role).value not in t.MEMBERSHIP_ROLES:
        raise ValueError(f"{role.value!r} is not a membership role")
    statement = (
        pg_insert(t.membership)
        .values(
            account_id=account_id,
            workspace_id=workspace_id,
            role=role.value,
            created_at=_now(),
        )
        .on_conflict_do_nothing(
            index_elements=[t.membership.c.account_id, t.membership.c.workspace_id]
        )
    )
    try:
        conn.execute(statement)
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.ForeignKeyViolation):
            if "account" in _constraint_name(exc):
                raise StorageRefusal(
                    ACCOUNT_MISSING, f"account {account_id} does not exist"
                ) from None
            raise StorageRefusal(
                WORKSPACE_MISSING, f"workspace {workspace_id} does not exist"
            ) from None
        raise
    row = get_membership(conn, account_id=account_id, workspace_id=workspace_id)
    if row is None:  # pragma: no cover - the insert or the existing row is visible
        raise StorageRefusal(WORKSPACE_MISSING, "membership row vanished")
    return row


def get_membership(
    conn: Connection, *, account_id: UUID, workspace_id: UUID
) -> MembershipRow | None:
    found = (
        conn.execute(
            select(t.membership).where(
                t.membership.c.account_id == account_id,
                t.membership.c.workspace_id == workspace_id,
            )
        )
        .mappings()
        .first()
    )
    return None if found is None else _membership(found)


def list_memberships(
    conn: Connection, *, account_id: UUID
) -> tuple[MembershipRow, ...]:
    """Every membership row for ``account_id``, across every workspace it belongs
    to -- oldest first (``created_at``, then ``workspace_id`` to break a tie),
    the same determinism ``list_workspaces`` uses above. This is what lets a
    caller build the account's full workspace list, including whichever one is
    currently active; it does not filter on or otherwise treat any workspace as
    special."""
    statement = (
        select(t.membership)
        .where(t.membership.c.account_id == account_id)
        .order_by(t.membership.c.created_at, t.membership.c.workspace_id)
    )
    return tuple(_membership(row) for row in conn.execute(statement).mappings())


# --- session (C7a, run 0b2) -------------------------------------------------------
# Local refusal states for the defensive rowcount checks below: these functions are
# only ever called with an id the caller just created or fetched in the same code
# path, so a miss here means an internal invariant broke, not a reachable API state.
# Distinct names from ``rheo_core.boundary.context.SESSION_MISSING`` (a different,
# user/API-facing refusal for "no session_secret hash matched"), so a log line or a
# test failure never has to guess which layer said so.

SESSION_ROW_MISSING: Final = "session_row_missing"
SESSION_SECRET_EXISTS: Final = "session_secret_exists"


@dataclass(frozen=True, slots=True)
class SessionRow:
    id: UUID
    account_id: UUID
    active_workspace_id: UUID | None
    active_workspace_changed_at: datetime | None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


def _session(row: RowMapping) -> SessionRow:
    return SessionRow(
        id=row["id"],
        account_id=row["account_id"],
        active_workspace_id=row["active_workspace_id"],
        active_workspace_changed_at=row["active_workspace_changed_at"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


def insert_session(conn: Connection, account_id: UUID) -> SessionRow:
    """A fresh session row for ``account_id``: no active workspace, not revoked.

    ``expires_at`` is ``NOT NULL`` and this repository takes no value for it, so it
    starts equal to ``created_at`` — a placeholder the caller
    (``rheo_core.sessions.service.create_session``) immediately supersedes with
    ``touch_session`` in the same transaction, using the deployment's current session
    policy. Nothing external ever observes the placeholder, because nothing commits
    between the two calls.
    """
    now = _now()
    row = SessionRow(uuid7(), account_id, None, None, now, now, now, None)
    conn.execute(
        insert(t.session).values(
            id=row.id,
            account_id=row.account_id,
            active_workspace_id=None,
            active_workspace_changed_at=None,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
            revoked_at=None,
        )
    )
    return row


def get_session(conn: Connection, session_id: UUID) -> SessionRow | None:
    found = (
        conn.execute(select(t.session).where(t.session.c.id == session_id))
        .mappings()
        .first()
    )
    return None if found is None else _session(found)


def touch_session(
    conn: Connection, session_id: UUID, *, last_seen_at: datetime, expires_at: datetime
) -> None:
    """Bump ``last_seen_at`` and ``expires_at`` to the caller's computed values."""
    result = conn.execute(
        update(t.session)
        .where(t.session.c.id == session_id)
        .values(last_seen_at=last_seen_at, expires_at=expires_at)
    )
    if result.rowcount != 1:
        raise StorageRefusal(SESSION_ROW_MISSING, f"session {session_id} has no row")


def revoke_session(conn: Connection, session_id: UUID) -> None:
    """Set ``revoked_at``; idempotent in effect (a caller never re-revokes)."""
    result = conn.execute(
        update(t.session).where(t.session.c.id == session_id).values(revoked_at=_now())
    )
    if result.rowcount != 1:
        raise StorageRefusal(SESSION_ROW_MISSING, f"session {session_id} has no row")


def set_active_workspace(
    conn: Connection, session_id: UUID, workspace_id: UUID
) -> None:
    """Set ``active_workspace_id`` and stamp ``active_workspace_changed_at``."""
    result = conn.execute(
        update(t.session)
        .where(t.session.c.id == session_id)
        .values(active_workspace_id=workspace_id, active_workspace_changed_at=_now())
    )
    if result.rowcount != 1:
        raise StorageRefusal(SESSION_ROW_MISSING, f"session {session_id} has no row")


# --- session_secret (C7a, run 0b2) -------------------------------------------------


def insert_session_secret(
    conn: Connection, *, secret_hash: bytes, session_id: UUID, host: str
) -> None:
    """One host-scoped secret hash for ``session_id``. ``(session_id, host)`` is
    unique; a second mint for the same host is ``session_secret_exists``."""
    try:
        conn.execute(
            insert(t.session_secret).values(
                secret_hash=secret_hash,
                session_id=session_id,
                host=host,
                created_at=_now(),
            )
        )
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.ForeignKeyViolation):
            raise StorageRefusal(
                SESSION_ROW_MISSING, f"session {session_id} does not exist"
            ) from None
        if isinstance(exc.orig, psycopg.errors.UniqueViolation):
            raise StorageRefusal(
                SESSION_SECRET_EXISTS,
                f"session {session_id} already has a secret for host {host!r}",
            ) from None
        raise


def get_session_by_secret_hash(
    conn: Connection, *, secret_hash: bytes, host: str
) -> SessionRow | None:
    """The session behind a host-scoped secret's hash: the join
    ``session_secret`` -> ``session`` in one round trip, not two."""
    found = (
        conn.execute(
            select(t.session)
            .select_from(t.session_secret)
            .join(t.session, t.session.c.id == t.session_secret.c.session_id)
            .where(
                t.session_secret.c.secret_hash == secret_hash,
                t.session_secret.c.host == host,
            )
        )
        .mappings()
        .first()
    )
    return None if found is None else _session(found)


# --- session_grant (C7a, run 0b2) --------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionGrantRow:
    code_hash: bytes
    session_id: UUID
    target_host: str
    nonce_hash: bytes
    expires_at: datetime
    used_at: datetime | None


def _session_grant(row: RowMapping) -> SessionGrantRow:
    return SessionGrantRow(
        code_hash=bytes(row["code_hash"]),
        session_id=row["session_id"],
        target_host=row["target_host"],
        nonce_hash=bytes(row["nonce_hash"]),
        expires_at=row["expires_at"],
        used_at=row["used_at"],
    )


def insert_session_grant(
    conn: Connection,
    *,
    code_hash: bytes,
    session_id: UUID,
    target_host: str,
    nonce_hash: bytes,
    expires_at: datetime,
) -> SessionGrantRow:
    row = SessionGrantRow(
        code_hash, session_id, target_host, nonce_hash, expires_at, None
    )
    try:
        conn.execute(
            insert(t.session_grant).values(
                code_hash=row.code_hash,
                session_id=row.session_id,
                target_host=row.target_host,
                nonce_hash=row.nonce_hash,
                expires_at=row.expires_at,
                used_at=None,
            )
        )
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.ForeignKeyViolation):
            raise StorageRefusal(
                SESSION_ROW_MISSING, f"session {session_id} does not exist"
            ) from None
        raise
    return row


def consume_session_grant(
    conn: Connection, *, code_hash: bytes, target_host: str, nonce_hash: bytes
) -> SessionGrantRow | None:
    """Validate and consume a grant code in one atomic statement: unused, unexpired,
    the right host, the right nonce. All four checks live in the one ``UPDATE ...
    WHERE`` below, so a caller cannot reorder or partially apply them, and a
    concurrent consumer can never win the same code twice. Every failure — no such
    code, already used, expired, wrong host, or a nonce mismatch — returns ``None``
    without distinguishing which: an attacker probing this boundary and a user
    following a stale link must look identical from here up. Deliberately not a
    ``StorageRefusal`` or the boundary's ``Refusal``: this file is storage, one layer
    below the boundary, and a plain ``None`` on a lookup miss is this file's own
    established idiom (``get_session``, ``get_workspace``, ...); the caller
    (``rheo_core.sessions.grants``) is what turns a miss into the named
    ``invalid_grant`` refusal.
    """
    statement = (
        update(t.session_grant)
        .where(
            t.session_grant.c.code_hash == code_hash,
            t.session_grant.c.used_at.is_(None),
            t.session_grant.c.expires_at > _now(),
            t.session_grant.c.target_host == target_host,
            t.session_grant.c.nonce_hash == nonce_hash,
        )
        .values(used_at=_now())
        .returning(*t.session_grant.c)
    )
    updated = conn.execute(statement).mappings().first()
    return None if updated is None else _session_grant(updated)


# --- identity_provider (C7a, run 0b2) -----------------------------------------------


@dataclass(frozen=True, slots=True)
class IdentityProviderRow:
    provider_id: str
    enabled: bool
    client_id: str
    client_secret_ref: str


def _identity_provider(row: RowMapping) -> IdentityProviderRow:
    return IdentityProviderRow(
        provider_id=row["provider_id"],
        enabled=row["enabled"],
        client_id=row["client_id"],
        client_secret_ref=row["client_secret_ref"],
    )


def upsert_identity_provider(
    conn: Connection,
    *,
    provider_id: str,
    enabled: bool,
    client_id: str,
    client_secret_ref: str,
) -> IdentityProviderRow:
    """Insert or fully replace the one row for ``provider_id``."""
    row = IdentityProviderRow(provider_id, enabled, client_id, client_secret_ref)
    values = {
        "enabled": row.enabled,
        "client_id": row.client_id,
        "client_secret_ref": row.client_secret_ref,
    }
    statement = (
        pg_insert(t.identity_provider)
        .values(provider_id=row.provider_id, **values)
        .on_conflict_do_update(
            index_elements=[t.identity_provider.c.provider_id], set_=values
        )
    )
    conn.execute(statement)
    return row


def get_identity_provider(
    conn: Connection, provider_id: str
) -> IdentityProviderRow | None:
    found = (
        conn.execute(
            select(t.identity_provider).where(
                t.identity_provider.c.provider_id == provider_id
            )
        )
        .mappings()
        .first()
    )
    return None if found is None else _identity_provider(found)


# --- access_token (C8, run 0b2) --------------------------------------------------

ACCESS_TOKEN_MISSING: Final = "access_token_missing"
"""Local refusal for the defensive rowcount checks below: ``revoke_access_token``/
``touch_access_token_last_used`` are only ever called with an id a caller just read
in the same transaction, so a miss here means an internal invariant broke, not a
reachable API state -- the same reasoning ``session``'s ``SESSION_ROW_MISSING``
documents for itself above."""


@dataclass(frozen=True, slots=True)
class AccessTokenRow:
    id: UUID
    token_hash: bytes
    account_id: UUID
    workspace_id: UUID
    kind: str
    issued_from: str
    set_name: str | None
    purpose: str | None
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None


def _access_token(row: RowMapping) -> AccessTokenRow:
    return AccessTokenRow(
        id=row["id"],
        token_hash=bytes(row["token_hash"]),
        account_id=row["account_id"],
        workspace_id=row["workspace_id"],
        kind=row["kind"],
        issued_from=row["issued_from"],
        set_name=row["set_name"],
        purpose=row["purpose"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        last_used_at=row["last_used_at"],
    )


def insert_access_token(
    conn: Connection,
    *,
    account_id: UUID,
    workspace_id: UUID,
    kind: str,
    issued_from: str,
    token_hash: bytes,
    set_name: str | None,
    purpose: str | None,
    expires_at: datetime,
) -> AccessTokenRow:
    """One ``access_token`` row. ``expires_at`` is the caller's
    (``rheo_core.tokens.issue``'s) job: it alone knows the resolved, floored
    ``identity.token_max_days.<kind>`` policy for the target workspace.
    ``account_missing``/``workspace_missing`` on a foreign-key miss."""
    row = AccessTokenRow(
        id=uuid7(),
        token_hash=token_hash,
        account_id=account_id,
        workspace_id=workspace_id,
        kind=kind,
        issued_from=issued_from,
        set_name=set_name,
        purpose=purpose,
        created_at=_now(),
        expires_at=expires_at,
        revoked_at=None,
        last_used_at=None,
    )
    try:
        conn.execute(
            insert(t.access_token).values(
                id=row.id,
                token_hash=row.token_hash,
                account_id=row.account_id,
                workspace_id=row.workspace_id,
                kind=row.kind,
                issued_from=row.issued_from,
                set_name=row.set_name,
                purpose=row.purpose,
                created_at=row.created_at,
                expires_at=row.expires_at,
                revoked_at=None,
                last_used_at=None,
            )
        )
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.ForeignKeyViolation):
            constraint = _constraint_name(exc)
            if "account" in constraint:
                raise StorageRefusal(
                    ACCOUNT_MISSING, f"account {account_id} does not exist"
                ) from None
            raise StorageRefusal(
                WORKSPACE_MISSING, f"workspace {workspace_id} does not exist"
            ) from None
        raise
    return row


def get_access_token(conn: Connection, token_id: UUID) -> AccessTokenRow | None:
    """By primary key -- ``rheo token revoke <id>`` needs a token's own
    ``workspace_id`` before it can build the operator context ``core.token.revoke``
    dispatches under."""
    found = (
        conn.execute(select(t.access_token).where(t.access_token.c.id == token_id))
        .mappings()
        .first()
    )
    return None if found is None else _access_token(found)


def get_access_token_by_hash(
    conn: Connection, token_hash: bytes
) -> AccessTokenRow | None:
    found = (
        conn.execute(
            select(t.access_token).where(t.access_token.c.token_hash == token_hash)
        )
        .mappings()
        .first()
    )
    return None if found is None else _access_token(found)


def revoke_access_token(conn: Connection, token_id: UUID) -> None:
    """Set ``revoked_at``; idempotent in effect (a caller never re-revokes)."""
    result = conn.execute(
        update(t.access_token)
        .where(t.access_token.c.id == token_id)
        .values(revoked_at=_now())
    )
    if result.rowcount != 1:
        raise StorageRefusal(
            ACCESS_TOKEN_MISSING, f"access token {token_id} has no row"
        )


def delete_access_token(conn: Connection, token_id: UUID) -> None:
    """Delete the row (snapshot rows cascade). Not a revoke."""
    result = conn.execute(delete(t.access_token).where(t.access_token.c.id == token_id))
    if result.rowcount != 1:
        raise StorageRefusal(
            ACCESS_TOKEN_MISSING, f"access token {token_id} has no row"
        )


def touch_access_token_last_used(conn: Connection, token_id: UUID) -> None:
    result = conn.execute(
        update(t.access_token)
        .where(t.access_token.c.id == token_id)
        .values(last_used_at=_now())
    )
    if result.rowcount != 1:
        raise StorageRefusal(
            ACCESS_TOKEN_MISSING, f"access token {token_id} has no row"
        )


# --- access_token_operation (C8, run 0b2) ----------------------------------------


def insert_access_token_operations(
    conn: Connection, *, token_id: UUID, operation_names: Iterable[str]
) -> None:
    """The snapshot rows for one token, bulk-inserted in one statement. A no-op
    for an empty iterable (``issue.py`` never reaches this call with one -- it
    refuses ``set_empty`` first -- but an empty ``INSERT ... VALUES`` is not
    valid SQL, so this guards it anyway)."""
    values = [
        {"token_id": token_id, "operation_name": name} for name in operation_names
    ]
    if not values:
        return
    conn.execute(insert(t.access_token_operation), values)


def list_access_token_operations(conn: Connection, token_id: UUID) -> frozenset[str]:
    rows = conn.execute(
        select(t.access_token_operation.c.operation_name).where(
            t.access_token_operation.c.token_id == token_id
        )
    )
    return frozenset(row.operation_name for row in rows)
