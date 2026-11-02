"""Recorded qualifications, local drafts and handoff requests."""

import json
from hashlib import sha256
from typing import Any
from uuid import UUID

from rheo_contracts import StaleRecord, WorkspaceContext
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from sqlalchemy import insert, select

from rheo_leads import handoff_routing as routing
from rheo_leads import pipeline_common as h
from rheo_leads import pipeline_contracts as c
from rheo_leads.references import ref
from rheo_leads.storage import pipeline as p


def assess(ctx: WorkspaceContext, uow: UnitOfWork, m: c.AssessInput) -> c.RecordOutput:
    record = h.opportunity(uow, m, writing=True)
    evidence: list[UUID] = list(
        uow.connection.execute(
            select(p.opportunity_observation.c.observation_id).where(
                p.opportunity_observation.c.opportunity_id == record["id"]
            )
        ).scalars()
    )
    rubric = (
        uow.connection.execute(
            select(p.preset_rubric).where(
                p.preset_rubric.c.preset_id == record["preset_id"],
                p.preset_rubric.c.version == record["preset_version"],
            )
        )
        .mappings()
        .one()
    )
    fields = list(
        uow.connection.execute(
            select(p.opportunity_field_state).where(
                p.opportunity_field_state.c.opportunity_id == record["id"],
                p.opportunity_field_state.c.value_kind == "value",
                p.opportunity_field_state.c.orphaned.is_(False),
            )
        ).mappings()
    )
    result = dict(
        id=uuid7(),
        opportunity_id=record["id"],
        preset_version=record["preset_version"],
        rubric_slug=rubric["rubric_slug"],
        rubric_version=rubric["rubric_version"],
        objective=m.objective,
        evidence_refs=[ref("observation", i) for i in evidence],
        input_revision=record["revision"],
        evidence_digest=h.evidence_digest(uow, record),
        fit="needs_information",
        intent="needs_information",
        urgency="needs_information",
        evidence_completeness="high"
        if len(fields) >= 6
        else "medium"
        if len(fields) >= 3
        else "low"
        if fields
        else "needs_information",
        explanation=(
            "Evidence completeness counts populated fields. "
            "Fit, buying intent and urgency require explicit assessment evidence."
        ),
        uncertainty="high",
        model_id=None,
        prompt_version=None,
        **h.actor(ctx),
    )
    if m.assessment is not None:
        result.update(m.assessment.model_dump(exclude={"author"}))
    uow.connection.execute(insert(p.qualification).values(**result))
    h.event(
        ctx, uow, "opportunity.qualified", record, ref("qualification", result["id"])
    )
    return h.output(result, "qualification")


def qualifications(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.RefInput
) -> c.ItemsOutput:
    record = h.opportunity(uow, m)
    return c.ItemsOutput(
        items=[
            {**dict(r), "ref": ref("qualification", r["id"])}
            for r in uow.connection.execute(
                select(p.qualification)
                .where(p.qualification.c.opportunity_id == record["id"])
                .order_by(
                    p.qualification.c.input_revision.desc(),
                    p.qualification.c.created_at.desc(),
                    p.qualification.c.id.desc(),
                )
            ).mappings()
        ]
    )


def draft(ctx: WorkspaceContext, uow: UnitOfWork, m: c.DraftInput) -> c.RecordOutput:
    record = h.opportunity(uow, m, writing=True)
    result = dict(
        id=uuid7(),
        opportunity_id=record["id"],
        kind="followup",
        body=m.body,
        template_version=record["preset_version"],
        runtime_request_id=None,
        **h.actor(ctx),
    )
    uow.connection.execute(insert(p.draft).values(**result))
    h.bump(uow, record)
    return h.output(result, "draft")


def latest_qualification(uow: UnitOfWork, opportunity_id: UUID) -> dict[str, Any]:
    """The opportunity's current assessment, read newest first, or a refusal."""
    found = (
        uow.connection.execute(
            select(p.qualification)
            .where(p.qualification.c.opportunity_id == opportunity_id)
            .order_by(
                p.qualification.c.input_revision.desc(),
                p.qualification.c.created_at.desc(),
                p.qualification.c.id.desc(),
            )
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    if found is None:
        raise OperationRefused(
            "qualification_required", "assess the opportunity before handing it off"
        )
    return dict(found)


def _handoff_output(uow: UnitOfWork, record: dict[str, Any]) -> c.RecordOutput:
    """A handoff with the qualification its request was made against."""
    snapshot = (
        uow.connection.execute(
            select(
                p.handoff_snapshot.c.qualification_ref,
                p.handoff_snapshot.c.qualification_stale,
            ).where(p.handoff_snapshot.c.handoff_id == record["id"])
        )
        .mappings()
        .one()
    )
    return h.output({**record, **dict(snapshot)}, "handoff")


def _locked_handoff(uow: UnitOfWork, handoff_id: UUID) -> dict[str, Any]:
    found = (
        uow.connection.execute(
            select(p.handoff).where(p.handoff.c.id == handoff_id).with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if found is None:
        raise OperationRefused("not_found", "record unavailable")
    return dict(found)


def handoff(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.HandoffInput
) -> c.RecordOutput:
    """Record one handoff and, for Current, hold its first attempt for approval.

    The opportunity is locked and its stage and outcome never change. The same
    idempotency key with the same request answers with the same handoff, after
    copying core's outcome for it; a changed request under a used key refuses.
    """
    h.lock(uow)
    record = dict(
        uow.connection.execute(
            select(p.opportunity)
            .where(p.opportunity.c.id == m.ref.id)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
        or {}
    )
    if not record:
        raise OperationRefused("not_found", "record unavailable")
    try:
        body = json.dumps(
            dict(
                purpose=m.purpose.value,
                destination_ref=m.destination_ref,
                payload=m.payload,
            ),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (ValueError, TypeError, RecursionError):
        raise OperationRefused(
            "input_invalid", "handoff payload must be JSON"
        ) from None
    maximum = resolve(
        workspace_id=ctx.workspace_id,
        source=TransactionBoundOverrideSource(uow, workspace_id=ctx.workspace_id),
    ).get_int("approvals.max_payload_bytes")
    if len(body) > int(maximum):
        raise OperationRefused("payload_too_large", "handoff snapshot exceeds limit")
    digest = sha256(body).digest()
    existing = (
        uow.connection.execute(
            select(p.handoff).where(
                p.handoff.c.opportunity_id == record["id"],
                p.handoff.c.idempotency_key == m.idempotency_key,
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing:
        if existing["snapshot_digest"] != digest:
            raise OperationRefused("idempotency_key_reused", "handoff request differs")
        found = routing.refresh(ctx, uow, _locked_handoff(uow, existing["id"]), h.now())
        return _handoff_output(uow, found)
    h.opportunity(uow, m)
    qualification = latest_qualification(uow, record["id"])
    stale = qualification["evidence_digest"] != h.evidence_digest(uow, record)
    identifier = uuid7()
    if m.destination_ref is not None:
        # Refuse a payload naming a routing field before anything is written.
        routing.current_request(ref("handoff", identifier), 1, m.payload)
    now = h.now()
    result = dict(
        id=identifier,
        opportunity_id=record["id"],
        purpose=m.purpose.value,
        destination_ref=m.destination_ref,
        idempotency_key=m.idempotency_key,
        snapshot_digest=digest,
        state="unavailable",
        result_ref=None,
        result_detail=routing.NO_DESTINATION
        if m.destination_ref is None
        else routing.DESTINATION_UNAVAILABLE,
        approval_id=None,
        external_action_id=None,
        **h.actor(ctx, "requested"),
        resolved_at=now,
        attempt_count=0,
    )
    uow.connection.execute(insert(p.handoff).values(**result))
    uow.connection.execute(
        insert(p.handoff_snapshot).values(
            handoff_id=identifier,
            body=m.payload,
            byte_length=len(body),
            qualification_ref=ref("qualification", qualification["id"]),
            qualification_stale=stale,
        )
    )
    if m.destination_ref == routing.CURRENT:
        staged = routing.stage(ctx, uow, result, m.payload, attempt=1, now=now)
        if staged is not None:
            routing.write(uow, result, staged)
    h.bump(uow, record)
    h.event(ctx, uow, "handoff.requested", result, ref("opportunity", record["id"]))
    return _handoff_output(uow, _locked_handoff(uow, identifier))


def handoff_restage(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.HandoffRestage
) -> c.RecordOutput:
    """Stage the same handoff again, under a new approval, where the closed matrix
    allows it (:func:`rheo_leads.handoff_routing.may_restage`).

    The attempt count rises by one and derives the next core key; the Current source
    stays the handoff. A request whose count is one behind answers with the handoff
    as it now is, so a lost response never stages twice.
    """
    h.lock(uow)
    found = _locked_handoff(uow, m.handoff_id)
    now = h.now()
    if found["attempt_count"] == m.attempt_count + 1:
        return _handoff_output(uow, routing.refresh(ctx, uow, found, now))
    if found["attempt_count"] != m.attempt_count:
        raise StaleRecord("handoff changed")
    read = routing.settle(ctx, uow, found, now)
    if not routing.may_restage(found, read):
        raise OperationRefused(
            routing.NOT_RESTAGEABLE, f"a {found['state']} handoff cannot be restaged"
        )
    payload: dict[str, Any] = uow.connection.execute(
        select(p.handoff_snapshot.c.body).where(
            p.handoff_snapshot.c.handoff_id == found["id"]
        )
    ).scalar_one()
    staged = routing.stage(
        ctx, uow, found, payload, attempt=found["attempt_count"] + 1, now=now
    )
    if staged is None:
        raise OperationRefused(
            routing.DESTINATION_UNAVAILABLE, "Current is not available to restage"
        )
    routing.write(uow, found, staged)
    return _handoff_output(uow, found)


def handoff_get(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.HandoffGet
) -> c.RecordOutput:
    record = h.row(uow, p.handoff, m.handoff_id)
    record["snapshot"] = uow.connection.execute(
        select(p.handoff_snapshot.c.body).where(
            p.handoff_snapshot.c.handoff_id == m.handoff_id
        )
    ).scalar_one()
    return _handoff_output(uow, record)


def handoff_list(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.HandoffList
) -> c.HandoffPage:
    """An opportunity's handoffs, newest first, one page at a time."""
    record = h.opportunity(uow, m)
    snapshot = p.handoff_snapshot
    rows = list(
        uow.connection.execute(
            select(
                p.handoff,
                snapshot.c.qualification_ref,
                snapshot.c.qualification_stale,
            )
            .join(snapshot, snapshot.c.handoff_id == p.handoff.c.id)
            .where(p.handoff.c.opportunity_id == record["id"])
            .order_by(p.handoff.c.requested_at.desc(), p.handoff.c.id.desc())
            .offset(m.offset)
            .limit(m.limit + 1)
        ).mappings()
    )
    return c.HandoffPage(
        items=[
            c.HandoffSummary(
                ref=ref("handoff", row["id"]),
                opportunity_ref=ref("opportunity", row["opportunity_id"]),
                **{
                    field: row[field]
                    for field in c.HandoffSummary.model_fields
                    if field not in {"ref", "opportunity_ref"}
                },
            )
            for row in rows[: m.limit]
        ],
        next_offset=m.offset + m.limit if len(rows) > m.limit else None,
    )
