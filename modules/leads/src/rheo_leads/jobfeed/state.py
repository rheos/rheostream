"""The collector's own state rows."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import Connection, Text, any_, cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY, array, insert

from rheo_leads.jobfeed.normalize import EVENT_ID_SEPARATOR
from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import tables as t

_SEQUENCE = re.compile(r"[0-9]+")

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


@dataclass(frozen=True)
class StoredPosting:
    """A posting's dedupe row: its last sequence number and stored field digests
    (the text :func:`rheo_leads.jobfeed.delivery.format_digests` writes)."""

    seq: int
    field_digests: str


def read_postings(
    uow: UnitOfWork, *, connection_id: UUID, posting_ids: Sequence[str]
) -> dict[str, StoredPosting]:
    """The connection's dedupe rows for ``posting_ids``, by posting id; one read."""
    if not posting_ids:
        return {}
    table = j.jobfeed_posting
    return {
        row.posting_id: StoredPosting(seq=row.seq, field_digests=row.field_digests)
        for row in uow.connection.execute(
            select(table.c.posting_id, table.c.seq, table.c.field_digests).where(
                table.c.connection_id == connection_id,
                table.c.posting_id.in_(sorted(set(posting_ids))),
            )
        )
    }


def _receipt_sequence(event_id: str) -> int | None:
    """The integer second segment of a receipt's event id, or ``None``."""
    parts = event_id.split(EVENT_ID_SEPARATOR)
    if len(parts) < 2 or _SEQUENCE.fullmatch(parts[1]) is None:
        return None
    return int(parts[1])


def next_seq(
    uow: UnitOfWork, *, connection_id: UUID, posting_ids: Sequence[str]
) -> dict[str, int]:
    """The sequence number each posting's next delivery uses. Never one used before.

    With a dedupe row it is the row's ``seq`` plus one. Without one (a posting never
    delivered, or one whose row an approved erasure removed) it is one more than the
    largest integer second segment among this connection's receipt event ids for that
    posting, else 1: erasure leaves every receipt in place, so this sees every number
    ever used. That fallback is one batched read of receipt identifiers only; no
    payload, digest or other content is read.
    """
    wanted = sorted(set(posting_ids))
    stored = read_postings(uow, connection_id=connection_id, posting_ids=wanted)
    result = {posting_id: row.seq + 1 for posting_id, row in stored.items()}
    missing = [posting_id for posting_id in wanted if posting_id not in stored]
    if not missing:
        return result
    receipt = t.delivery_receipt
    highest = dict.fromkeys(missing, 0)
    found: list[str] = list(
        uow.connection.execute(
            select(receipt.c.source_event_id).where(
                receipt.c.connection_id == connection_id,
                func.split_part(receipt.c.source_event_id, EVENT_ID_SEPARATOR, 1)
                == any_(cast(array(missing), ARRAY(Text))),
            )
        ).scalars()
    )
    for event_id in found:
        posting_id = event_id.split(EVENT_ID_SEPARATOR, 1)[0]
        sequence = _receipt_sequence(event_id)
        if posting_id in highest and sequence is not None:
            highest[posting_id] = max(highest[posting_id], sequence)
    result.update({posting_id: top + 1 for posting_id, top in highest.items()})
    return result


def upsert_posting(
    uow: UnitOfWork,
    *,
    connection_id: UUID,
    posting_id: str,
    seq: int,
    merged_digests: str,
    last_event_id: str,
    now: datetime,
    keep_digests: bool,
) -> None:
    """Record ``seq`` as the posting's last used number, in the caller's transaction.

    ``keep_digests`` leaves an existing row's stored digests as they are (the number
    is consumed but the content was not accepted); a new row then stores
    ``merged_digests``, which the caller passes as the digests it already held.
    """
    table = j.jobfeed_posting
    updates: dict[str, object] = {
        "seq": seq,
        "last_event_id": last_event_id,
        "last_delivered_at": now,
    }
    if not keep_digests:
        updates["field_digests"] = merged_digests
    uow.connection.execute(
        insert(table)
        .values(
            connection_id=connection_id,
            posting_id=posting_id,
            seq=seq,
            field_digests=merged_digests,
            last_event_id=last_event_id,
            first_delivered_at=now,
            last_delivered_at=now,
        )
        .on_conflict_do_update(
            index_elements=[table.c.connection_id, table.c.posting_id], set_=updates
        )
    )


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
