"""Working opportunities through explicit links and pinned pipeline semantics."""

from typing import Any

from rheo_contracts import WorkspaceContext
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rheo_leads import pipeline_common as h
from rheo_leads import pipeline_contracts as c
from rheo_leads import pipeline_evidence as e
from rheo_leads.references import ref
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t


def get(ctx: WorkspaceContext, uow: UnitOfWork, m: c.RefInput) -> c.RecordOutput:
    h.require(ctx)
    record = h.opportunity(uow, m)
    current = h.row(uow, p.pipeline_preset, record["preset_id"])["current_version"]
    stage = (
        uow.connection.execute(
            select(p.preset_stage).where(
                p.preset_stage.c.preset_id == record["preset_id"],
                p.preset_stage.c.version == record["preset_version"],
                p.preset_stage.c.stage_id == record["stage_id"],
            )
        )
        .mappings()
        .one()
    )
    label = uow.connection.execute(
        select(p.preset_stage.c.label).where(
            p.preset_stage.c.preset_id == record["preset_id"],
            p.preset_stage.c.version == current,
            p.preset_stage.c.stage_id == record["stage_id"],
        )
    ).scalar_one_or_none()
    record["stage"] = {**dict(stage), "label": label or stage["label"]}
    for name in (
        "opportunity_field_state",
        "opportunity_party",
        "opportunity_observation",
        "opportunity_note",
    ):
        table = getattr(p, name)
        record[name] = [
            dict(r)
            for r in uow.connection.execute(
                select(table)
                .where(table.c.opportunity_id == record["id"])
                .order_by(*table.primary_key.columns)
            ).mappings()
        ]
    assessment = (
        uow.connection.execute(
            select(p.qualification)
            .where(p.qualification.c.opportunity_id == record["id"])
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
    record["qualification"] = dict(assessment) if assessment else None
    record["transitions"] = [
        dict(r)
        for r in uow.connection.execute(
            select(p.preset_stage)
            .join(
                p.preset_transition,
                (p.preset_transition.c.preset_id == p.preset_stage.c.preset_id)
                & (p.preset_transition.c.version == p.preset_stage.c.version)
                & (p.preset_transition.c.to_stage_id == p.preset_stage.c.stage_id),
            )
            .where(
                p.preset_transition.c.preset_id == record["preset_id"],
                p.preset_transition.c.version == record["preset_version"],
                p.preset_transition.c.from_stage_id == record["stage_id"],
            )
            .order_by(p.preset_stage.c.ordinal)
        ).mappings()
    ]
    record["drafts"] = [
        dict(r)
        for r in uow.connection.execute(
            select(p.draft)
            .where(p.draft.c.opportunity_id == record["id"])
            .order_by(p.draft.c.created_at.desc())
            .limit(20)
        ).mappings()
    ]
    return h.output(record)


def listing(ctx: WorkspaceContext, uow: UnitOfWork, m: c.ListInput) -> c.ItemsOutput:
    h.require(ctx)
    q = select(p.opportunity)
    if m.pipeline_id:
        q = q.where(p.opportunity.c.pipeline_id == m.pipeline_id)
    if m.query:
        q = q.where(p.opportunity.c.title.icontains(m.query, autoescape=True))
    rows = uow.connection.execute(
        q.order_by(p.opportunity.c.created_at.desc(), p.opportunity.c.id.desc())
        .limit(m.limit)
        .offset(m.offset)
    ).mappings()
    return c.ItemsOutput(
        items=[{**dict(r), "ref": ref("opportunity", r["id"])} for r in rows]
    )


def _link(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    record: dict[str, Any],
    obs: dict[str, Any],
    decision: str,
) -> bool:
    added = uow.connection.execute(
        pg_insert(p.opportunity_observation)
        .values(
            opportunity_id=record["id"],
            observation_id=obs["id"],
            decision=decision,
            **h.actor(ctx, "decided"),
        )
        .on_conflict_do_nothing()
        .returning(p.opportunity_observation.c.observation_id)
    ).scalar_one_or_none()
    if obs["party_ref"]:
        h.party_read(ctx, uow, obs["party_ref"])
        uow.connection.execute(
            pg_insert(p.opportunity_party)
            .values(
                opportunity_id=record["id"],
                party_ref=obs["party_ref"],
                role="contact",
                **h.actor(ctx, "added"),
            )
            .on_conflict_do_nothing()
        )
    return added is not None


def create(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    m: c.CreateInput,
    *,
    decision: str = "manual",
) -> c.RecordOutput:
    h.require(ctx, write=True)
    h.lock(uow)
    obs = h.observation(uow, m.observation_ref.id)
    pipeline = h.row(uow, p.pipeline, m.pipeline_id)
    preset = h.row(uow, p.pipeline_preset, pipeline["preset_id"])
    stage = uow.connection.execute(
        select(p.preset_stage.c.stage_id)
        .where(
            p.preset_stage.c.preset_id == preset["id"],
            p.preset_stage.c.version == preset["current_version"],
            p.preset_stage.c.kind == "open",
        )
        .order_by(p.preset_stage.c.ordinal)
        .limit(1)
    ).scalar_one_or_none()
    if stage is None:
        raise OperationRefused("pipeline_invalid", "pipeline has no open entry stage")
    record = dict(
        id=uuid7(),
        pipeline_id=pipeline["id"],
        preset_id=preset["id"],
        preset_version=preset["current_version"],
        stage_id=stage,
        title="Untitled inquiry",
        owner_id=pipeline["owner_id"],
        funnel_id=obs["funnel_id"],
        campaign_id=obs["campaign_id"],
        source_observation_id=obs["id"],
        revision=1,
        updated_at=h.now(),
        **h.actor(ctx),
    )
    uow.connection.execute(insert(p.opportunity).values(**record))
    _link(ctx, uow, record, obs, decision)
    changes = e.derive(uow, record)
    uow.connection.execute(
        update(p.opportunity)
        .where(p.opportunity.c.id == record["id"])
        .values(**changes)
    )
    record.update(changes)
    h.event(ctx, uow, "opportunity.created", record, ref("observation", obs["id"]))
    return h.output(record)


def attach(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    m: c.AttachInput,
    *,
    decision: str = "manual",
) -> c.RecordOutput:
    record = h.opportunity(uow, m, writing=True)
    if _link(ctx, uow, record, h.observation(uow, m.observation_ref.id), decision):
        h.bump(uow, record, **e.derive(uow, record))
    return h.output(record)


def edit(ctx: WorkspaceContext, uow: UnitOfWork, m: c.UpdateInput) -> c.RecordOutput:
    record = h.opportunity(uow, m, writing=True)
    specs = e.fields(uow, record)
    for target, value in m.fields.items():
        if target not in specs:
            raise OperationRefused(
                "field_unknown", "extension field is not in the pinned preset"
            )
        e.validate_value(specs[target], value)
        e.user_field(uow, record["id"], target, value)
    changes: dict[str, Any] = {}
    if m.title is not None:
        changes["title"] = m.title
        e.user_field(uow, record["id"], "subject", m.title)
    if m.owner_id is not None:
        changes["owner_id"] = m.owner_id
    if "value_amount" in m.model_fields_set:
        changes.update(
            value_amount=m.value_amount,
            value_currency=m.value_currency,
            value_basis=m.value_basis,
        )
    h.bump(uow, record, **changes)
    return h.output(record)


def transition(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.TransitionInput
) -> c.RecordOutput:
    record = h.opportunity(uow, m, writing=True)
    keys = (
        p.preset_transition.c.preset_id == record["preset_id"],
        p.preset_transition.c.version == record["preset_version"],
    )
    allowed = uow.connection.execute(
        select(p.preset_transition).where(
            *keys,
            p.preset_transition.c.from_stage_id == record["stage_id"],
            p.preset_transition.c.to_stage_id == m.to_stage_id,
        )
    ).first()
    if allowed is None:
        raise OperationRefused(
            "transition_not_allowed", "transition is not in the pinned preset"
        )
    stage = (
        uow.connection.execute(
            select(p.preset_stage).where(
                p.preset_stage.c.preset_id == record["preset_id"],
                p.preset_stage.c.version == record["preset_version"],
                p.preset_stage.c.stage_id == m.to_stage_id,
            )
        )
        .mappings()
        .one()
    )
    required: Any = uow.connection.execute(
        select(p.preset_requirement.c.field).where(
            p.preset_requirement.c.preset_id == record["preset_id"],
            p.preset_requirement.c.version == record["preset_version"],
            p.preset_requirement.c.to_stage_id == m.to_stage_id,
        )
    ).scalars()
    values = {
        r["target"]: r["value_text"]
        for r in uow.connection.execute(
            select(p.opportunity_field_state).where(
                p.opportunity_field_state.c.opportunity_id == record["id"],
                p.opportunity_field_state.c.value_kind == "value",
                p.opportunity_field_state.c.orphaned.is_(False),
            )
        ).mappings()
    }
    values["title"] = record["title"]
    if any(not values.get(field) for field in required):
        raise OperationRefused("evidence_required", "required fields are missing")
    h.bump(
        uow,
        record,
        stage_id=m.to_stage_id,
        disposition_outcome=stage["outcome"],
        disposition_at=h.now() if stage["kind"] == "terminal" else None,
    )
    if m.note:
        uow.connection.execute(
            insert(p.opportunity_note).values(
                id=uuid7(),
                opportunity_id=record["id"],
                body=m.note,
                stage_id=m.to_stage_id,
                **h.actor(ctx),
            )
        )
    h.event(ctx, uow, "opportunity.transitioned", record)
    return h.output(record)


def add_note(ctx: WorkspaceContext, uow: UnitOfWork, m: c.NoteInput) -> c.RecordOutput:
    record = h.opportunity(uow, m, writing=True)
    uow.connection.execute(
        insert(p.opportunity_note).values(
            id=uuid7(), opportunity_id=record["id"], body=m.body, **h.actor(ctx)
        )
    )
    h.bump(uow, record)
    return h.output(record)


def party_change(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.PartyInput, *, remove: bool = False
) -> c.RecordOutput:
    record = h.opportunity(uow, m, writing=True)
    h.party_read(ctx, uow, m.party_ref.format())
    if remove:
        uow.connection.execute(
            update(p.opportunity_party)
            .where(
                p.opportunity_party.c.opportunity_id == record["id"],
                p.opportunity_party.c.party_ref == m.party_ref.format(),
                p.opportunity_party.c.role == m.role,
            )
            .values(removed_at=h.now())
        )
    else:
        uow.connection.execute(
            pg_insert(p.opportunity_party)
            .values(
                opportunity_id=record["id"],
                party_ref=m.party_ref.format(),
                role=m.role,
                **h.actor(ctx, "added"),
            )
            .on_conflict_do_update(
                index_elements=["opportunity_id", "party_ref", "role"],
                set_={"removed_at": None},
            )
        )
    h.bump(uow, record)
    return h.output(record)


def remove_party(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.PartyInput
) -> c.RecordOutput:
    return party_change(ctx, uow, m, remove=True)


def observation_get(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.ObservationInput
) -> c.RecordOutput:
    h.require(ctx)
    record = h.observation(uow, m.observation_ref.id)
    record["fields"] = [
        dict(r)
        for r in uow.connection.execute(
            select(t.observation_field).where(
                t.observation_field.c.observation_id == record["id"]
            )
        ).mappings()
    ]
    return h.output(record, "observation")
