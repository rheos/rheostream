"""Every OAuth connector SQL statement (issue #287), over the tables in
``oauth_tables.py``.

Each function takes the caller's ``Connection`` and runs inside the caller's
transaction; none commits (the ``work_index.py`` shape). The flow service runs each
step in one control-plane transaction, so a refusal that wrote something (a code
burn, a revocation, a ``refused`` event) commits with it.

**The two client predicates are the whole client lifecycle.** Nothing else decides
whether a client may be deleted or counts toward ``identity.oauth.max_clients``:

* *Abandoned* (the only thing cleanup deletes): no ``oauth_grant`` row has ever
  existed for the client, it was created before ``abandoned_before``, and none of its
  ``oauth_authorization`` rows is in flight (``expires_at > now`` or
  ``code_expires_at > now``). A client with any grant, live, expired or revoked, is
  never selected; ``RESTRICT`` on ``oauth_grant.client_id`` makes a wrong predicate
  fail loudly rather than erase one.
* *Counted*: not abandoned, and either no grant yet or at least one *live* grant. A
  live grant has ``access_token.revoked_at`` null and ``now < grant_expires_at``. The
  access token's own ``expires_at`` never enters it, so an idle connector whose
  hour-long access value lapsed is still counted and still refreshable.

``now`` is always the caller's, so one step evaluates every predicate at one instant.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Connection,
    and_,
    delete,
    exists,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import RowMapping

from rheo_core.refs import uuid7
from rheo_core.storage import control_tables as t
from rheo_core.storage import oauth_tables as o

MAX_EVENT_LIMIT: Final = 500
"""The most ``oauth_event`` rows one read returns (spec, Data Models)."""


# --- oauth_client and its redirect URIs ----------------------------------------------


@dataclass(frozen=True, slots=True)
class OAuthClientRow:
    client_id: str
    client_name: str
    registered_from: str
    created_at: datetime
    redirect_uris: tuple[str, ...]


def insert_client(
    conn: Connection,
    *,
    client_id: str,
    client_name: str,
    registered_from: str,
    created_at: datetime,
    redirect_uris: Iterable[str],
) -> OAuthClientRow:
    """One ``oauth_client`` row and one ``oauth_client_redirect_uri`` row per URI.
    The caller has already checked every URI against the allowlist."""
    uris = tuple(dict.fromkeys(redirect_uris))
    if not uris:
        raise ValueError("a client needs at least one redirect URI")
    conn.execute(
        insert(o.oauth_client).values(
            client_id=client_id,
            client_name=client_name,
            registered_from=registered_from,
            created_at=created_at,
        )
    )
    conn.execute(
        insert(o.oauth_client_redirect_uri),
        [{"client_id": client_id, "redirect_uri": uri} for uri in uris],
    )
    return OAuthClientRow(
        client_id=client_id,
        client_name=client_name,
        registered_from=registered_from,
        created_at=created_at,
        redirect_uris=uris,
    )


def get_client(conn: Connection, client_id: str) -> OAuthClientRow | None:
    found = (
        conn.execute(
            select(o.oauth_client).where(o.oauth_client.c.client_id == client_id)
        )
        .mappings()
        .first()
    )
    if found is None:
        return None
    uris: tuple[str, ...] = tuple(
        conn.execute(
            select(o.oauth_client_redirect_uri.c.redirect_uri)
            .where(o.oauth_client_redirect_uri.c.client_id == client_id)
            .order_by(o.oauth_client_redirect_uri.c.redirect_uri)
        ).scalars()
    )
    return OAuthClientRow(
        client_id=found["client_id"],
        client_name=found["client_name"],
        registered_from=found["registered_from"],
        created_at=found["created_at"],
        redirect_uris=uris,
    )


def _has_any_grant() -> ColumnElement[bool]:
    return exists().where(o.oauth_grant.c.client_id == o.oauth_client.c.client_id)


def _abandoned(*, now: datetime, abandoned_before: datetime) -> ColumnElement[bool]:
    """The abandoned-client predicate over ``oauth_client`` (module docstring)."""
    authorization = o.oauth_authorization
    in_flight = exists().where(
        authorization.c.client_id == o.oauth_client.c.client_id,
        or_(
            authorization.c.expires_at > now,
            authorization.c.code_expires_at > now,
        ),
    )
    return and_(
        ~_has_any_grant(),
        o.oauth_client.c.created_at < abandoned_before,
        ~in_flight,
    )


def _live_grant(*, now: datetime) -> ColumnElement[bool]:
    """The one *live grant* condition (module docstring) over a row that joins
    ``oauth_grant`` to its ``access_token``: not revoked and before the grant's
    absolute end. The access token's own ``expires_at`` never enters it."""
    return and_(
        t.access_token.c.revoked_at.is_(None),
        o.oauth_grant.c.grant_expires_at > now,
    )


def _holds_live_grant(*, now: datetime) -> ColumnElement[bool]:
    return exists().where(
        o.oauth_grant.c.client_id == o.oauth_client.c.client_id,
        t.access_token.c.id == o.oauth_grant.c.token_id,
        _live_grant(now=now),
    )


def delete_abandoned_clients(
    conn: Connection, *, now: datetime, abandoned_before: datetime
) -> int:
    """Delete every abandoned client (its redirect URIs and authorization rows
    cascade). Returns how many were deleted."""
    result = conn.execute(
        delete(o.oauth_client).where(
            _abandoned(now=now, abandoned_before=abandoned_before)
        )
    )
    return result.rowcount


def count_counted_clients(
    conn: Connection, *, now: datetime, abandoned_before: datetime
) -> int:
    """How many clients count toward ``identity.oauth.max_clients``."""
    statement = (
        select(func.count())
        .select_from(o.oauth_client)
        .where(
            ~_abandoned(now=now, abandoned_before=abandoned_before),
            or_(~_has_any_grant(), _holds_live_grant(now=now)),
        )
    )
    return conn.execute(statement).scalar_one()


def count_registrations_from(conn: Connection, *, source: str, since: datetime) -> int:
    """Clients registered from ``source`` at or after ``since`` that still exist."""
    statement = (
        select(func.count())
        .select_from(o.oauth_client)
        .where(
            o.oauth_client.c.registered_from == source,
            o.oauth_client.c.created_at >= since,
        )
    )
    return conn.execute(statement).scalar_one()


# --- oauth_authorization ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OAuthAuthorizationRow:
    id: UUID
    request_hash: bytes
    client_id: str
    redirect_uri: str
    code_challenge: str
    state: str | None
    created_at: datetime
    expires_at: datetime
    account_id: UUID | None
    workspace_id: UUID | None
    decided_at: datetime | None
    code_hash: bytes | None
    code_expires_at: datetime | None
    code_used_at: datetime | None
    token_id: UUID | None


def _authorization(row: RowMapping) -> OAuthAuthorizationRow:
    code_hash = row["code_hash"]
    return OAuthAuthorizationRow(
        id=row["id"],
        request_hash=bytes(row["request_hash"]),
        client_id=row["client_id"],
        redirect_uri=row["redirect_uri"],
        code_challenge=row["code_challenge"],
        state=row["state"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        account_id=row["account_id"],
        workspace_id=row["workspace_id"],
        decided_at=row["decided_at"],
        code_hash=None if code_hash is None else bytes(code_hash),
        code_expires_at=row["code_expires_at"],
        code_used_at=row["code_used_at"],
        token_id=row["token_id"],
    )


def insert_authorization(
    conn: Connection,
    *,
    request_hash: bytes,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str | None,
    created_at: datetime,
    expires_at: datetime,
) -> OAuthAuthorizationRow:
    """A pending authorization request. The caller validated every field."""
    row = OAuthAuthorizationRow(
        id=uuid7(),
        request_hash=request_hash,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        state=state,
        created_at=created_at,
        expires_at=expires_at,
        account_id=None,
        workspace_id=None,
        decided_at=None,
        code_hash=None,
        code_expires_at=None,
        code_used_at=None,
        token_id=None,
    )
    conn.execute(
        insert(o.oauth_authorization).values(
            id=row.id,
            request_hash=request_hash,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            state=state,
            created_at=created_at,
            expires_at=expires_at,
        )
    )
    return row


def _authorization_where(
    conn: Connection, condition: ColumnElement[bool], *, for_update: bool
) -> OAuthAuthorizationRow | None:
    statement = select(o.oauth_authorization).where(condition)
    if for_update:
        statement = statement.with_for_update()
    found = conn.execute(statement).mappings().first()
    return None if found is None else _authorization(found)


def get_authorization_by_request_hash(
    conn: Connection, request_hash: bytes, *, for_update: bool = False
) -> OAuthAuthorizationRow | None:
    return _authorization_where(
        conn,
        o.oauth_authorization.c.request_hash == request_hash,
        for_update=for_update,
    )


def get_authorization_by_code_hash(
    conn: Connection, code_hash: bytes, *, for_update: bool = False
) -> OAuthAuthorizationRow | None:
    return _authorization_where(
        conn, o.oauth_authorization.c.code_hash == code_hash, for_update=for_update
    )


def record_authorization_decision(
    conn: Connection,
    authorization_id: UUID,
    *,
    decided_at: datetime,
    account_id: UUID | None,
    workspace_id: UUID | None,
    code_hash: bytes | None = None,
    code_expires_at: datetime | None = None,
) -> None:
    """Mark a still-undecided row decided: an approval passes the code's hash and
    expiry, a denial passes neither. Refuses (``ValueError``) a row that is
    already decided, so a caller that skipped its own ``FOR UPDATE`` re-read cannot
    issue a second code."""
    result = conn.execute(
        update(o.oauth_authorization)
        .where(
            o.oauth_authorization.c.id == authorization_id,
            o.oauth_authorization.c.decided_at.is_(None),
        )
        .values(
            decided_at=decided_at,
            account_id=account_id,
            workspace_id=workspace_id,
            code_hash=code_hash,
            code_expires_at=code_expires_at,
        )
    )
    if result.rowcount != 1:
        raise ValueError(f"authorization {authorization_id} is missing or decided")


def mark_code_used(
    conn: Connection, authorization_id: UUID, *, used_at: datetime
) -> None:
    """Burn the row's code. Only the first burn sets ``code_used_at``."""
    conn.execute(
        update(o.oauth_authorization)
        .where(
            o.oauth_authorization.c.id == authorization_id,
            o.oauth_authorization.c.code_used_at.is_(None),
        )
        .values(code_used_at=used_at)
    )


def set_authorization_token(
    conn: Connection, authorization_id: UUID, *, token_id: UUID
) -> None:
    """Record the access token a redemption issued, so a replay can revoke it."""
    conn.execute(
        update(o.oauth_authorization)
        .where(o.oauth_authorization.c.id == authorization_id)
        .values(token_id=token_id)
    )


def delete_expired_authorizations(conn: Connection, *, before: datetime) -> int:
    """Delete authorization rows whose request and code both ended before
    ``before`` (the service passes a day ago) and that issued nothing. A redeemed
    row (``token_id`` set) is kept: it is what lets a late replay of its code
    revoke what the code issued (FR 14). Returns how many."""
    authorization = o.oauth_authorization
    result = conn.execute(
        delete(authorization).where(
            authorization.c.token_id.is_(None),
            authorization.c.expires_at < before,
            or_(
                authorization.c.code_expires_at.is_(None),
                authorization.c.code_expires_at < before,
            ),
        )
    )
    return result.rowcount


# --- oauth_grant and oauth_refresh_token --------------------------------------------


@dataclass(frozen=True, slots=True)
class OAuthGrantRow:
    token_id: UUID
    client_id: str
    created_at: datetime
    grant_expires_at: datetime


def insert_grant(
    conn: Connection,
    *,
    token_id: UUID,
    client_id: str,
    created_at: datetime,
    grant_expires_at: datetime,
) -> OAuthGrantRow:
    conn.execute(
        insert(o.oauth_grant).values(
            token_id=token_id,
            client_id=client_id,
            created_at=created_at,
            grant_expires_at=grant_expires_at,
        )
    )
    return OAuthGrantRow(
        token_id=token_id,
        client_id=client_id,
        created_at=created_at,
        grant_expires_at=grant_expires_at,
    )


def get_grant(conn: Connection, token_id: UUID) -> OAuthGrantRow | None:
    found = (
        conn.execute(select(o.oauth_grant).where(o.oauth_grant.c.token_id == token_id))
        .mappings()
        .first()
    )
    if found is None:
        return None
    return OAuthGrantRow(
        token_id=found["token_id"],
        client_id=found["client_id"],
        created_at=found["created_at"],
        grant_expires_at=found["grant_expires_at"],
    )


@dataclass(frozen=True, slots=True)
class OAuthRefreshTokenRow:
    token_hash: bytes
    token_id: UUID
    created_at: datetime
    rotated_at: datetime | None


def insert_refresh_token(
    conn: Connection, *, token_hash: bytes, token_id: UUID, created_at: datetime
) -> OAuthRefreshTokenRow:
    conn.execute(
        insert(o.oauth_refresh_token).values(
            token_hash=token_hash, token_id=token_id, created_at=created_at
        )
    )
    return OAuthRefreshTokenRow(
        token_hash=token_hash, token_id=token_id, created_at=created_at, rotated_at=None
    )


def get_refresh_token_for_update(
    conn: Connection, token_hash: bytes
) -> OAuthRefreshTokenRow | None:
    """By hash, locked, so two concurrent refreshes of one value serialize."""
    found = (
        conn.execute(
            select(o.oauth_refresh_token)
            .where(o.oauth_refresh_token.c.token_hash == token_hash)
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if found is None:
        return None
    return OAuthRefreshTokenRow(
        token_hash=bytes(found["token_hash"]),
        token_id=found["token_id"],
        created_at=found["created_at"],
        rotated_at=found["rotated_at"],
    )


def mark_refresh_rotated(
    conn: Connection, token_hash: bytes, *, rotated_at: datetime
) -> None:
    """Rotate a live refresh row. ``ValueError`` if it is missing or rotated."""
    result = conn.execute(
        update(o.oauth_refresh_token)
        .where(
            o.oauth_refresh_token.c.token_hash == token_hash,
            o.oauth_refresh_token.c.rotated_at.is_(None),
        )
        .values(rotated_at=rotated_at)
    )
    if result.rowcount != 1:
        raise ValueError("refresh token is missing or already rotated")


# --- oauth_event --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OAuthEventRow:
    id: UUID
    occurred_at: datetime
    event: str
    outcome: str
    client_id: str | None
    account_id: UUID | None
    workspace_id: UUID | None
    token_id: UUID | None


def insert_event(
    conn: Connection,
    *,
    event: str,
    outcome: str,
    occurred_at: datetime,
    client_id: str | None = None,
    account_id: UUID | None = None,
    workspace_id: UUID | None = None,
    token_id: UUID | None = None,
) -> OAuthEventRow:
    """One content-free audit row. ``event`` and ``outcome`` are CHECKed."""
    row = OAuthEventRow(
        id=uuid7(),
        occurred_at=occurred_at,
        event=event,
        outcome=outcome,
        client_id=client_id,
        account_id=account_id,
        workspace_id=workspace_id,
        token_id=token_id,
    )
    conn.execute(
        insert(o.oauth_event).values(
            id=row.id,
            occurred_at=occurred_at,
            event=event,
            outcome=outcome,
            client_id=client_id,
            account_id=account_id,
            workspace_id=workspace_id,
            token_id=token_id,
        )
    )
    return row


def list_oauth_events(
    conn: Connection, *, workspace_id: UUID, include_unbound: bool, limit: int
) -> tuple[OAuthEventRow, ...]:
    """Newest first (``occurred_at`` DESC, then ``id`` DESC), for one workspace, plus
    the rows bound to no workspace (registrations, early refusals) when
    ``include_unbound``. ``limit`` is 1 to :data:`MAX_EVENT_LIMIT`."""
    if not 1 <= limit <= MAX_EVENT_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_EVENT_LIMIT}")
    event = o.oauth_event
    scope: ColumnElement[bool] = event.c.workspace_id == workspace_id
    if include_unbound:
        scope = or_(scope, event.c.workspace_id.is_(None))
    statement = (
        select(event)
        .where(scope)
        .order_by(event.c.occurred_at.desc(), event.c.id.desc())
        .limit(limit)
    )
    return tuple(
        OAuthEventRow(
            id=row["id"],
            occurred_at=row["occurred_at"],
            event=row["event"],
            outcome=row["outcome"],
            client_id=row["client_id"],
            account_id=row["account_id"],
            workspace_id=row["workspace_id"],
            token_id=row["token_id"],
        )
        for row in conn.execute(statement).mappings()
    )


# --- operator reads (``rheo token list``, ``rheo doctor``) ---------------------------


@dataclass(frozen=True, slots=True)
class ConnectorGrantListing:
    """What ``rheo token list`` adds to a ``connector`` token row. ``client_name`` is
    the stored (registration-sanitized) name; the CLI cuts it again for display."""

    token_id: UUID
    client_name: str
    grant_expires_at: datetime


def list_connector_grants(
    conn: Connection,
    *,
    workspace_id: UUID | None = None,
    account_id: UUID | None = None,
) -> dict[UUID, ConnectorGrantListing]:
    """Every grant's client name and absolute end, by token id, in one read (the
    listing joins it onto ``list_access_tokens`` rather than reading per row),
    narrowed by the same workspace/account filters that listing takes."""
    statement = (
        select(
            o.oauth_grant.c.token_id,
            o.oauth_grant.c.grant_expires_at,
            o.oauth_client.c.client_name,
        )
        .join(o.oauth_client, o.oauth_client.c.client_id == o.oauth_grant.c.client_id)
        .join(t.access_token, t.access_token.c.id == o.oauth_grant.c.token_id)
    )
    if workspace_id is not None:
        statement = statement.where(t.access_token.c.workspace_id == workspace_id)
    if account_id is not None:
        statement = statement.where(t.access_token.c.account_id == account_id)
    return {
        row["token_id"]: ConnectorGrantListing(
            token_id=row["token_id"],
            client_name=row["client_name"],
            grant_expires_at=row["grant_expires_at"],
        )
        for row in conn.execute(statement).mappings()
    }


@dataclass(frozen=True, slots=True)
class ConnectorGrantCounts:
    """``rheo doctor``'s ``connector grants`` figures. Counts only.

    ``active``, ``expired`` and ``revoked`` partition every grant: revoked wins over
    expired (a revoked grant is ``revoked`` whatever its end). ``ending_soon`` and
    ``missing_refresh`` are subsets of ``active`` (live grants)."""

    counted_clients: int
    active: int
    expired: int
    revoked: int
    ending_soon: int
    missing_refresh: int


def connector_grant_counts(
    conn: Connection,
    *,
    now: datetime,
    abandoned_before: datetime,
    ending_before: datetime,
) -> ConnectorGrantCounts:
    """The doctor's counts at one instant ``now``. A live grant is the module
    docstring's (not revoked, ``now < grant_expires_at``); a live grant with no
    ``oauth_refresh_token`` row whose ``rotated_at`` is null breaks the refresh-row
    invariant and is ``missing_refresh``."""
    grant, token = o.oauth_grant, t.access_token
    revoked = token.c.revoked_at.is_not(None)
    live = _live_grant(now=now)
    has_live_refresh = exists().where(
        o.oauth_refresh_token.c.token_id == grant.c.token_id,
        o.oauth_refresh_token.c.rotated_at.is_(None),
    )

    def _count(condition: ColumnElement[bool]) -> ColumnElement[int]:
        return func.count().filter(condition)

    statement = select(
        _count(live).label("active"),
        _count(and_(~revoked, grant.c.grant_expires_at <= now)).label("expired"),
        _count(revoked).label("revoked"),
        _count(and_(live, grant.c.grant_expires_at <= ending_before)).label(
            "ending_soon"
        ),
        _count(and_(live, ~has_live_refresh)).label("missing_refresh"),
    ).select_from(grant.join(token, token.c.id == grant.c.token_id))
    row = conn.execute(statement).mappings().one()
    return ConnectorGrantCounts(
        counted_clients=count_counted_clients(
            conn, now=now, abandoned_before=abandoned_before
        ),
        active=row["active"],
        expired=row["expired"],
        revoked=row["revoked"],
        ending_soon=row["ending_soon"],
        missing_refresh=row["missing_refresh"],
    )
