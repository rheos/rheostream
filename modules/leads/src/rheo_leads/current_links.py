"""Leads' minimal projection of Current's lasting source link, and its repair.

Leads keeps four facts about where a handed-off item went: the live project ref, the
last project ref, the project state and the link revision. They come from two
places only, and both copy a strictly newer revision and nothing else:

- the ``current.source_item_link.changed`` event, consumed by :func:`on_link_changed`;
- the bounded ``leads.current_links.reconcile`` job (:func:`reconcile`), which repairs
  a missed or reordered event and a core outcome Leads has not copied yet.

The job runs two independent scans, each with its own durable cursor in
``leads.current_link_cursor``. The nonterminal scan pages pending handoffs and reads
``core.external_action.get`` first; only a proven success with its item ref goes on
to ``current.item.link_state``. The successful scan pages succeeded handoffs and reads
only ``current.item.link_state``. Both order by ``(requested_at, id)``, store the
complete last examined key, read one bounded page strictly after it, advance past
every examined row whatever its answer, and wrap to the start when a page comes back
short, so the cycles repeat and every handoff is revisited.

When Current is absent, disabled, denies the read or is not ready, the projection
is left as it was and the row is counted unavailable. Nothing here imports Current,
reads its tables, or writes anything outside Leads.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from rheo_contracts import (
    EventEnvelope,
    RecordRef,
    RecordRefMalformed,
    WorkspaceContext,
)
from rheo_core.boundary.context import Refusal
from rheo_core.boundary.factories import context_for_event_consumer
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.transaction import call_in_transaction
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.work.cancellation import CancellationToken
from sqlalchemy import ColumnElement, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert

from rheo_leads import handoff_routing as routing
from rheo_leads import pipeline_common as h
from rheo_leads.references import ref
from rheo_leads.storage import pipeline as p

_log = logging.getLogger(__name__)

LINK_CHANGED: Final = "current.source_item_link.changed"
LINK_SCHEMA_VERSION: Final = 1
LINK_SOURCE: Final = "urn:rheo:module:current"
LINK_STATE: Final = "current.item.link_state"
LINK_CONSUMER_ID: Final = "leads.current_links"
RECONCILE_JOB_KIND: Final = "leads.current_links.reconcile"
RECONCILE_SCHEDULE: Final = routing.RECONCILE_SCHEDULE
RECONCILE_INTERVAL_SECONDS: Final = 900
RECONCILE_MAX_ATTEMPTS: Final = 3
PAGE_SIZE: Final = 25
REPAIR_OPERATIONS: Final = frozenset({routing.EXTERNAL_ACTION_GET, LINK_STATE})
"""The whole operation set of the repair job's restricted service context."""
NONTERMINAL: Final = "nonterminal"
SUCCEEDED: Final = "succeeded"


class CurrentLink(BaseModel):
    """Current's link read shape, field for field, as Leads accepts it.

    Declared here rather than imported: Leads names no Current type. Only the
    project fields and the revision are copied; the item state is never stored.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref: str
    item_ref: str
    project_ref: str | None
    last_project_ref: str | None
    item_state: str
    project_state: str
    revision: int = Field(ge=1)
    changed_at: AwareDatetime


def is_newer(stored: int | None, incoming: int) -> bool:
    """Whether a link revision may replace the stored one: strictly newer only."""
    return stored is None or incoming > stored


def _handoff_id(source_ref: str) -> UUID | None:
    try:
        parsed = RecordRef.parse(source_ref)
    except RecordRefMalformed:
        return None
    if parsed.module != "leads" or parsed.record_type != "handoff":
        return None
    return parsed.id


def apply_link(uow: UnitOfWork, link: CurrentLink) -> bool:
    """Copy a strictly newer link revision onto its handoff; whether it moved.

    A link for an item other than the handoff's recorded item is ignored. The
    revision compare is in the write itself, so a reordered or repeated delivery,
    and a repair racing an event, can only move the projection forward.
    """
    identifier = _handoff_id(link.source_ref)
    if identifier is None:
        return False
    revision = p.handoff.c.current_link_revision
    moved = uow.connection.execute(
        update(p.handoff)
        .where(
            p.handoff.c.id == identifier,
            (p.handoff.c.result_ref.is_(None))
            | (p.handoff.c.result_ref == link.item_ref),
            revision.is_(None) | (revision < link.revision),
        )
        .values(
            current_project_ref=link.project_ref,
            last_current_project_ref=link.last_project_ref,
            current_project_state=link.project_state,
            current_link_revision=link.revision,
        )
    ).rowcount
    return bool(moved == 1)


def on_link_changed(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
    """The consumer: copy the event's link when it is newer. Anything that is not a
    well-formed version-1 Current link event is left to the repair job."""
    if (
        envelope.type != LINK_CHANGED
        or envelope.schema_version != LINK_SCHEMA_VERSION
        or envelope.source != LINK_SOURCE
    ):
        return
    try:
        link = CurrentLink.model_validate(dict(envelope.data))
    except ValidationError:
        return
    h.lock(uow)
    apply_link(uow, link)


# --- the repair job ------------------------------------------------------------------


class ReconcilePayload(BaseModel):
    """What the ticker enqueues: the workspace, and nothing else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: UUID


@dataclass
class ScanReport:
    """Content-free counts for one scan's page."""

    examined: int = 0
    updated: int = 0
    unavailable: int = 0
    wrapped: bool = False


@dataclass
class ReconcileReport:
    nonterminal: ScanReport = field(default_factory=ScanReport)
    succeeded: ScanReport = field(default_factory=ScanReport)


def repair_context(workspace_id: UUID, uow: UnitOfWork) -> WorkspaceContext | Refusal:
    """The job's restricted service context, from core's boundary factory for a
    routed worker transaction: system actor, service role, no account, no audience,
    the modules enabled in this transaction, and exactly :data:`REPAIR_OPERATIONS`."""
    return context_for_event_consumer(
        workspace_id, uow, request_id=uuid7(), operation_names=REPAIR_OPERATIONS
    )


def reconcile_job(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    if not isinstance(payload, ReconcilePayload):
        raise TypeError("unexpected payload")
    ctx = repair_context(payload.workspace_id, uow)
    if isinstance(ctx, Refusal):
        return
    report = reconcile(ctx, uow, page_size=PAGE_SIZE)
    _log.info(
        "leads_current_links_reconciled nonterminal=%d/%d/%d succeeded=%d/%d/%d",
        report.nonterminal.examined,
        report.nonterminal.updated,
        report.nonterminal.unavailable,
        report.succeeded.examined,
        report.succeeded.updated,
        report.succeeded.unavailable,
    )


def reconcile(
    ctx: WorkspaceContext, uow: UnitOfWork, *, page_size: int
) -> ReconcileReport:
    """One bounded page of each scan, nonterminal first, in the caller's transaction."""
    if page_size < 1:
        raise ValueError("page_size must be positive")
    h.lock(uow)
    now = h.now()
    report = ReconcileReport()
    _scan(
        uow,
        NONTERMINAL,
        p.handoff.c.state == "pending",
        lambda row: _settle(ctx, uow, row, report.nonterminal, now),
        report.nonterminal,
        page_size,
        now,
    )
    _scan(
        uow,
        SUCCEEDED,
        p.handoff.c.state == "succeeded",
        lambda row: _relink(ctx, uow, row, report.succeeded),
        report.succeeded,
        page_size,
        now,
    )
    return report


def _scan(
    uow: UnitOfWork,
    scan: str,
    members: ColumnElement[bool],
    examine: Callable[[dict[str, Any]], object],
    report: ScanReport,
    page_size: int,
    now: datetime,
) -> None:
    cursor = p.current_link_cursor
    uow.connection.execute(
        insert(cursor)
        .values(
            scan=scan, after_requested_at=None, after_id=None, cycles=0, updated_at=now
        )
        .on_conflict_do_nothing(index_elements=[cursor.c.scan])
    )
    position = (
        uow.connection.execute(
            select(cursor).where(cursor.c.scan == scan).with_for_update()
        )
        .mappings()
        .one()
    )
    order = (p.handoff.c.requested_at, p.handoff.c.id)
    query = select(p.handoff).where(members)
    if position["after_id"] is not None:
        query = query.where(
            tuple_(*order)
            > tuple_(position["after_requested_at"], position["after_id"])
        )
    rows = [
        dict(row)
        for row in uow.connection.execute(
            query.order_by(*order).limit(page_size)
        ).mappings()
    ]
    for row in rows:
        report.examined += 1
        examine(row)
    if len(rows) < page_size:
        report.wrapped = True
        values: dict[str, Any] = dict(
            after_requested_at=None, after_id=None, cycles=position["cycles"] + 1
        )
    else:
        last = rows[-1]
        values = dict(after_requested_at=last["requested_at"], after_id=last["id"])
    uow.connection.execute(
        update(cursor).where(cursor.c.scan == scan).values(updated_at=now, **values)
    )


def _settle(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    row: dict[str, Any],
    report: ScanReport,
    now: datetime,
) -> None:
    """Core first: copy the action's outcome, then the link only after success."""
    read = routing.read_action(ctx, uow, row["external_action_id"])
    if read.record is None and not read.missing:
        report.unavailable += 1
        return
    moved = routing.write(uow, row, routing.outcome_values(row, read, now))
    if row["state"] == "succeeded" and row["result_ref"] is not None:
        moved = _relink(ctx, uow, row, report, counted=moved) or moved
    if moved:
        report.updated += 1


def read_link(
    ctx: WorkspaceContext, uow: UnitOfWork, source_ref: str
) -> CurrentLink | None:
    """``current.item.link_state`` under ``ctx``, inside a savepoint; ``None`` when
    Current is absent, disabled, denies the read, is not ready or has no link."""
    try:
        with uow.connection.begin_nested():
            answer = call_in_transaction(
                ctx, uow, LINK_STATE, {"source_ref": source_ref}
            )
    except OperationRefused:
        return None
    try:
        return CurrentLink.model_validate(answer.model_dump(mode="json"))
    except ValidationError:
        return None


def _relink(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    row: dict[str, Any],
    report: ScanReport,
    *,
    counted: bool | None = None,
) -> bool:
    """Read the link of a succeeded handoff and copy a newer revision. With
    ``counted`` set the caller owns the row's ``updated`` count."""
    reference = ref("handoff", row["id"])
    link = read_link(ctx, uow, reference)
    if (
        link is None
        or link.source_ref != reference
        or link.item_ref != row["result_ref"]
    ):
        report.unavailable += 1
        return False
    moved = is_newer(row["current_link_revision"], link.revision) and apply_link(
        uow, link
    )
    if moved and counted is None:
        report.updated += 1
    return moved
