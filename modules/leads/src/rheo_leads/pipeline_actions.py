"""Recorded qualifications, local drafts and destination-less handoff requests."""

import json
from hashlib import sha256
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from sqlalchemy import insert, select

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


def handoff(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.HandoffInput
) -> c.RecordOutput:
    h.lock(uow)
    record = h.row(uow, p.opportunity, m.ref.id)
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
        return h.output(dict(existing), "handoff")
    h.opportunity(uow, m)
    result = dict(
        id=uuid7(),
        opportunity_id=record["id"],
        purpose=m.purpose.value,
        destination_ref=m.destination_ref,
        idempotency_key=m.idempotency_key,
        snapshot_digest=digest,
        state="unavailable",
        result_ref=None,
        result_detail="no_destination",
        approval_id=None,
        external_action_id=None,
        **h.actor(ctx, "requested"),
        resolved_at=h.now(),
    )
    uow.connection.execute(insert(p.handoff).values(**result))
    uow.connection.execute(
        insert(p.handoff_snapshot).values(
            handoff_id=result["id"], body=m.payload, byte_length=len(body)
        )
    )
    h.bump(uow, record)
    h.event(ctx, uow, "handoff.requested", result, ref("opportunity", record["id"]))
    return h.output(result, "handoff")


def handoff_get(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.HandoffGet
) -> c.RecordOutput:
    record = h.row(uow, p.handoff, m.handoff_id)
    record["snapshot"] = uow.connection.execute(
        select(p.handoff_snapshot.c.body).where(
            p.handoff_snapshot.c.handoff_id == m.handoff_id
        )
    ).scalar_one()
    return h.output(record, "handoff")
