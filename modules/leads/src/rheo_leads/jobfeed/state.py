"""The collector's own state rows."""

from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import Connection, delete, select, update

from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import tables as t

RUN_CLAIM_WINDOW: Final = timedelta(minutes=60)
"""How long a run's claim holds: no run starts before ``run_claimed_at`` plus this."""

COLLECTOR_HEALTH_CODES: Final = frozenset(
    {
        "authorization_required",
        "token_refresh_failed",
        "account_selection_required",
        "upstream_unavailable",
        "upstream_rate_limited",
        "collection_partial",
        "endpoint_misconfigured",
        "registration_failed",
    }
)
"""The health codes the collector writes. Clearing them leaves intake's own alone."""


@dataclass(frozen=True)
class JobfeedConnection:
    """One ``jobfeed_connection`` row; ``singleton`` is constant and left out.

    The three ``*_ref`` values are secret-store references, never secret values.
    """

    connection_id: UUID
    pipeline_id: UUID
    authorization_state: str
    token_ref: str | None
    credential_generation: int
    resource_url: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    revocation_endpoint: str | None
    client_id: str | None
    client_auth_method: str | None
    client_redirect_uri: str | None
    client_secret_expires_at: datetime | None
    client_secret_ref: str | None
    registration_token_ref: str | None
    organization_id: str | None
    run_claimed_at: datetime | None
    last_completed_at: datetime | None
    consecutive_failures: int
    not_before: datetime | None
    fetched: int
    imported: int
    skipped_dup: int
    refreshed: int
    filtered: int
    skipped_untracked: int
    errors: int
    detail_reads: int
    pages: int
    created_at: datetime
    updated_at: datetime


_FIELDS = tuple(f.name for f in fields(JobfeedConnection))


def read_connection(
    conn: Connection, *, for_update: bool = False
) -> JobfeedConnection | None:
    """The workspace's collector connection, or ``None`` before the first connect.
    ``for_update`` locks the row until the transaction ends."""
    query = select(j.jobfeed_connection)
    if for_update:
        query = query.with_for_update()
    row = conn.execute(query).mappings().one_or_none()
    if row is None:
        return None
    return JobfeedConnection(**{name: row[name] for name in _FIELDS})


def next_run_not_before(connection: JobfeedConnection) -> datetime | None:
    """The earliest instant the next run may start: the later of the backoff's
    ``not_before`` and the last claim plus :data:`RUN_CLAIM_WINDOW`, or ``None``
    when neither applies."""
    claimed = connection.run_claimed_at
    bounds = [
        bound
        for bound in (
            connection.not_before,
            None if claimed is None else claimed + RUN_CLAIM_WINDOW,
        )
        if bound is not None
    ]
    return max(bounds) if bounds else None


def account_choices(
    conn: Connection, connection_id: UUID
) -> list[tuple[str, str | None]]:
    """The stored account choices as ``(organization_id, label)``, in received
    order."""
    table = j.jobfeed_account_choice
    return [
        (row.organization_id, row.label)
        for row in conn.execute(
            select(table.c.organization_id, table.c.label)
            .where(table.c.connection_id == connection_id)
            .order_by(table.c.ordinal)
        )
    ]


def forget_posting(
    uow: UnitOfWork, *, posting_id: str, connection_id: UUID | None = None
) -> None:
    """Delete the dedupe row for ``posting_id`` in the caller's transaction: on every
    connection when ``connection_id`` is ``None``, else on that connection only.

    The row is derived from content an erasure removes. Delivery receipts are left
    alone: their event ids keep the posting's sequence from ever restarting.
    """
    statement = delete(j.jobfeed_posting).where(
        j.jobfeed_posting.c.posting_id == posting_id
    )
    if connection_id is not None:
        statement = statement.where(j.jobfeed_posting.c.connection_id == connection_id)
    uow.connection.execute(statement)


def clear_collector_health(conn: Connection, connection_id: UUID) -> None:
    """Clear the connection's last error when it is one of the collector's codes."""
    conn.execute(
        update(t.connection_health)
        .where(
            t.connection_health.c.connection_id == connection_id,
            t.connection_health.c.last_error.in_(sorted(COLLECTOR_HEALTH_CODES)),
        )
        .values(last_error=None, last_error_at=None)
    )


@dataclass(frozen=True)
class PendingAuthorization:
    """One consumed ``jobfeed_authorization`` row; ``singleton`` is left out.

    ``client_registered`` says whether the start that wrote it registered the client
    (and so owns the two secret files) or reused the connection's stored one.
    """

    id: UUID
    state_hash: bytes = field(repr=False)
    code_verifier: str = field(repr=False)
    account_id: UUID
    connection_id: UUID
    pipeline_id: UUID
    mode: str
    resource_url: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    revocation_endpoint: str | None
    redirect_uri: str
    client_id: str
    client_auth_method: str
    client_secret_expires_at: datetime | None
    client_secret_ref: str | None = field(repr=False)
    registration_token_ref: str | None = field(repr=False)
    client_registered: bool
    created_at: datetime
    expires_at: datetime


_PENDING_FIELDS = tuple(f.name for f in fields(PendingAuthorization))


def take_pending(conn: Connection, state_hash: bytes) -> PendingAuthorization | None:
    """Lock, copy and delete the pending row whose state hash is ``state_hash``.

    Does not commit; the caller commits before acting on the copy, so nothing that
    happens afterwards can put the row back.
    """
    table = j.jobfeed_authorization
    row = (
        conn.execute(
            select(table).where(table.c.state_hash == state_hash).with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    pending = PendingAuthorization(**{name: row[name] for name in _PENDING_FIELDS})
    conn.execute(delete(table).where(table.c.id == pending.id))
    return pending
