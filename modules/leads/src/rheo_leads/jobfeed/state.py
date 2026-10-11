"""The collector's own state rows."""

import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from rheo_contracts import SafetyClass, WorkspaceContext
from rheo_core.audit.records import insert_audit_record
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.work.schedules import ensure_module_schedule
from sqlalchemy import Connection, Text, any_, cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY, array, insert

from rheo_leads.intake.accept import health_write
from rheo_leads.jobfeed.normalize import EVENT_ID_SEPARATOR
from rheo_leads.pipeline_common import lock
from rheo_leads.references import ref
from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import tables as t

_log = logging.getLogger(__name__)
_SEQUENCE = re.compile(r"[0-9]+")
_MODULE_ID: Final = "leads"

SCHEDULE_NAME: Final = "jobfeed_collect"
"""The collector's schedule row, ``(leads, jobfeed_collect)``."""

RUN_CLAIM_WINDOW: Final = timedelta(minutes=60)
"""How long a run's claim holds: no run starts before ``run_claimed_at`` plus this."""

BACKOFF_BASE: Final = timedelta(hours=2)
BACKOFF_CAP: Final = timedelta(hours=24)
AUTHORIZATION_LOST: Final = "leads.jobfeed.authorization_lost"
"""The terminal transition's audit operation name."""
UNIT_CLOSE_FAILED: Final = "jobfeed_unit_close_failed"
"""Logged, alone, when a short unit failed while closing after its commit."""
_EMPTY_REQUEST_DIGEST: Final = hashlib.sha256(b"{}").digest()

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


# --- a run's short units and its run-ending writes ------------------------------------


@dataclass(frozen=True)
class Claimed:
    """What a won claim returned: the reference to refresh and the generation every
    later guard of the run compares against."""

    token_ref: str = field(repr=False)
    generation: int


def claim_run(
    ctx: WorkspaceContext, *, connection_id: UUID, generation: int, now: datetime
) -> Claimed | None:
    """Claim the next run in a unit of its own that commits at once, or ``None``.

    Only a connected connection whose last claim is older than
    :data:`RUN_CLAIM_WINDOW` (or that was never claimed) is claimed, so two attempts
    claim once and no run starts within the window of the previous start. The
    connection must also still be at ``generation``, the one the caller read its
    client with: a reconnect that stored new credentials in between is left for the
    next run rather than refreshed as the client read before it.
    """
    table = j.jobfeed_connection
    claimed = table.c.run_claimed_at
    with open_unit_of_work(ctx) as unit:
        row = unit.connection.execute(
            update(table)
            .where(
                table.c.connection_id == connection_id,
                table.c.authorization_state == "connected",
                table.c.credential_generation == generation,
                claimed.is_(None) | (claimed < now - RUN_CLAIM_WINDOW),
            )
            .values(run_claimed_at=now)
            .returning(table.c.token_ref, table.c.credential_generation)
        ).one_or_none()
        unit.commit()
    if row is None or row.token_ref is None:
        return None
    return Claimed(token_ref=row.token_ref, generation=row.credential_generation)


def switch_token(
    ctx: WorkspaceContext,
    *,
    connection_id: UUID,
    old_ref: str,
    new_ref: str,
    generation: int,
    now: datetime,
) -> bool:
    """The rotation's compare-and-set, in a unit of its own: whether it won.

    It switches to ``new_ref`` only while the connection still references
    ``old_ref`` at ``generation``; a revoke or reconnect that committed first makes
    it lose. It raises only before its commit; once committed, a failure while the
    unit closes is logged as a fixed code and the outcome still returned, so the
    caller never retires a file the connection now references.
    """
    table = j.jobfeed_connection
    won: bool | None = None
    try:
        with open_unit_of_work(ctx) as unit:
            switched = unit.connection.execute(
                update(table)
                .where(
                    table.c.connection_id == connection_id,
                    table.c.token_ref == old_ref,
                    table.c.credential_generation == generation,
                )
                .values(
                    token_ref=new_ref,
                    credential_generation=generation + 1,
                    updated_at=now,
                )
            )
            outcome = switched.rowcount == 1
            unit.commit()
            won = outcome
    except Exception:
        if won is None:
            raise
        _log.warning(UNIT_CLOSE_FAILED)
    return bool(won)


def current_token(
    ctx: WorkspaceContext, *, connection_id: UUID
) -> tuple[str | None, int] | None:
    """The connection's current ``(token_ref, credential_generation)``, read in a
    fresh unit; ``None`` when the connection is gone."""
    table = j.jobfeed_connection
    with open_unit_of_work(ctx) as unit:
        row = unit.connection.execute(
            select(table.c.token_ref, table.c.credential_generation).where(
                table.c.connection_id == connection_id
            )
        ).one_or_none()
        unit.commit()
    return None if row is None else (row.token_ref, row.credential_generation)


ClientFiles = tuple[str | None, str | None]


def clear_rejected_client(
    ctx: WorkspaceContext,
    *,
    connection_id: UUID,
    client_id: str,
    generation: int,
    now: datetime,
) -> ClientFiles | None:
    """The token endpoint rejected the stored client during a run: clear every
    client column in a unit of its own, under the lifecycle lock.

    Only while the connection is still the one the run claimed (connected, at
    ``generation``) and still stores ``client_id``; otherwise nothing changes and
    ``None`` is returned. On success it returns the client's two file references,
    for the caller to discard once this has committed; it raises only before its
    commit.
    """
    table = j.jobfeed_connection
    retired: ClientFiles | None = None
    committed = False
    try:
        with open_unit_of_work(ctx) as unit:
            lock(unit)
            current = read_connection(unit.connection, for_update=True)
            if (
                current is None
                or current.connection_id != connection_id
                or current.authorization_state != "connected"
                or current.credential_generation != generation
                or current.client_id != client_id
            ):
                return None
            unit.connection.execute(
                update(table)
                .where(table.c.connection_id == connection_id)
                .values(
                    client_id=None,
                    client_auth_method=None,
                    client_redirect_uri=None,
                    client_secret_expires_at=None,
                    client_secret_ref=None,
                    registration_token_ref=None,
                    updated_at=now,
                )
            )
            files = (current.client_secret_ref, current.registration_token_ref)
            unit.commit()
            committed = True
            retired = files
    except Exception:
        if not committed:
            raise
        _log.warning(UNIT_CLOSE_FAILED)
    return retired


def claimed_connection(
    uow: UnitOfWork, *, connection_id: UUID, generation: int
) -> JobfeedConnection | None:
    """The guard every run-ending write passes first, on the caller's transaction:
    the lifecycle lock, then the connection row locked, returned only while it is
    still the one the run claimed (connected, at ``generation``). ``None`` means an
    owner revoked or reconnected meanwhile, and the run writes nothing at all."""
    lock(uow)
    current = read_connection(uow.connection, for_update=True)
    if (
        current is None
        or current.connection_id != connection_id
        or current.authorization_state != "connected"
        or current.credential_generation != generation
    ):
        return None
    return current


def backoff(failures: int, retry_after: int | None) -> timedelta:
    """How long the next run waits after ``failures`` consecutive failures: the
    larger of the base doubled per failure and Retry-After, each capped."""
    exponent = min(max(failures, 1) - 1, 16)
    doubled = min(BACKOFF_BASE * (1 << exponent), BACKOFF_CAP)
    asked = min(timedelta(seconds=max(retry_after or 0, 0)), BACKOFF_CAP)
    return max(doubled, asked)


def write_transient(
    uow: UnitOfWork,
    current: JobfeedConnection,
    *,
    code: str,
    now: datetime,
    retry_after: int | None = None,
) -> None:
    """A failed run that may be retried later: the health code, one more
    consecutive failure and the next run's earliest start."""
    failures = current.consecutive_failures + 1
    table = j.jobfeed_connection
    uow.connection.execute(
        update(table)
        .where(table.c.connection_id == current.connection_id)
        .values(
            consecutive_failures=failures,
            not_before=now + backoff(failures, retry_after),
            updated_at=now,
        )
    )
    health_write(uow, current.connection_id, error=code)


def write_terminal(
    uow: UnitOfWork,
    current: JobfeedConnection,
    conn_ctx: WorkspaceContext,
    *,
    now: datetime,
) -> None:
    """Authorization is lost: the connection needs a reconnect, its schedule goes
    off, the health code is set and one content-free audit row is written as the
    connection."""
    table = j.jobfeed_connection
    uow.connection.execute(
        update(table)
        .where(table.c.connection_id == current.connection_id)
        .values(authorization_state="authorization_required", updated_at=now)
    )
    ensure_module_schedule(
        uow.connection,
        module_id=_MODULE_ID,
        name=SCHEDULE_NAME,
        enabled=False,
        due_at=now,
    )
    health_write(uow, current.connection_id, error="authorization_required")
    insert_audit_record(
        uow.connection,
        occurred_at=now,
        actor_kind=conn_ctx.actor.kind.value,
        actor_id=conn_ctx.actor.id,
        entry=conn_ctx.entry.value,
        operation_name=AUTHORIZATION_LOST,
        safety_class=SafetyClass.MUTATE.value,
        operation_id=None,
        subject_ref=ref("intake_connection", current.connection_id),
        request_digest=_EMPTY_REQUEST_DIGEST,
        outcome="succeeded",
    )


def write_success(
    uow: UnitOfWork, current: JobfeedConnection, *, now: datetime
) -> None:
    """A completed run: the backoff is reset and only the collector's own health
    codes are cleared."""
    table = j.jobfeed_connection
    uow.connection.execute(
        update(table)
        .where(table.c.connection_id == current.connection_id)
        .values(consecutive_failures=0, not_before=None, updated_at=now)
    )
    clear_collector_health(uow.connection, current.connection_id)
