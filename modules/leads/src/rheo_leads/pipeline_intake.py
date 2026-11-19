"""Intake steps 4–6: source-authenticated party identity and explicit routing."""

from datetime import UTC, date, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.operations.transaction import call_in_transaction
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from sqlalchemy import RowMapping, select, update

from rheo_leads import pipeline_common as h
from rheo_leads.configuration import INQUIRY_FOLLOWUP_KEY
from rheo_leads.references import ref
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t

if TYPE_CHECKING:
    from rheo_leads.intake.process import DeliveryState


def resolve_party(
    ctx: WorkspaceContext, uow: UnitOfWork, state: "DeliveryState"
) -> None:
    assert state.observation_id is not None
    obs = h.observation(uow, state.observation_id)
    values = {f.target: f.value_text for f in state.facts if f.value_kind == "value"}
    hints = {
        key: values[target]
        for key, target in (
            ("name", "person.name"),
            ("email", "person.email"),
            ("phone", "person.phone"),
            ("organization_name", "organization.name"),
            ("organization_domain", "organization.domain"),
        )
        if values.get(target)
        and len(values[target] or "") <= (128 if key == "phone" else 512)
    }
    if not hints and not obs["external_subject_id"] and not obs["verified_email"]:
        return
    answer = call_in_transaction(
        ctx,
        uow,
        f"{h.RELATIONSHIPS}.party.resolve_or_create",
        dict(
            source_namespace=state.connection["source_namespace"],
            external_subject_id=obs["external_subject_id"],
            verified_email=obs["verified_email"],
            hints=hints,
            evidence_ref=ref("observation", obs["id"]),
            occurred_at=obs["source_occurred_at"],
        ),
    )
    reference = answer.model_dump()["party_ref"]
    uow.connection.execute(
        update(t.observation)
        .where(t.observation.c.id == obs["id"])
        .values(party_ref=reference)
    )


def tracked_opportunity(
    uow: UnitOfWork, *, pipeline_id: UUID, field: str, value: str
) -> RowMapping | None:
    """The newest opportunity in ``pipeline_id`` whose ``field`` holds ``value``
    (value kind ``value``, not orphaned), or ``None``: the ``attach_by_field`` lookup.

    Open or closed: a posting already passed on is refreshed, not reopened as a new
    lead.
    """
    return (
        uow.connection.execute(
            select(p.opportunity)
            .join(
                p.opportunity_field_state,
                p.opportunity_field_state.c.opportunity_id == p.opportunity.c.id,
            )
            .where(
                p.opportunity.c.pipeline_id == pipeline_id,
                p.opportunity_field_state.c.target == field,
                p.opportunity_field_state.c.value_kind == "value",
                p.opportunity_field_state.c.value_text == value,
                p.opportunity_field_state.c.orphaned.is_(False),
            )
            .order_by(p.opportunity.c.created_at.desc(), p.opportunity.c.id.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )


def route(ctx: WorkspaceContext, uow: UnitOfWork, state: "DeliveryState") -> None:
    assert state.observation_id is not None
    obs = h.observation(uow, state.observation_id)
    values = {f.target: f.value_text for f in state.facts if f.value_kind == "value"}
    rules = (
        uow.connection.execute(
            select(p.routing_rule)
            .where(
                p.routing_rule.c.connection_id == state.connection["id"],
                p.routing_rule.c.enabled.is_(True),
            )
            .order_by(p.routing_rule.c.ordinal)
        )
        .mappings()
        .all()
    )
    for rule in rules:
        matched = True
        conditions = uow.connection.execute(
            select(p.routing_condition).where(
                p.routing_condition.c.rule_id == rule["id"]
            )
        ).mappings()
        for condition in conditions:
            value = values.get(condition["field"])
            expected = condition["value"]
            match = {
                "present": value is not None,
                "absent": value is None,
                "equals": value is not None and value == expected,
                "contains": value is not None
                and expected is not None
                and expected in value,
            }[condition["op"]]
            matched = matched and match
        if not matched:
            continue
        if rule["action"] == "record_only":
            return
        decision = f"routing_rule:{rule['id']}"
        if rule["action"] == "attach_by_field":
            key = values.get(rule["match_field"])
            if key is None or not key.strip():
                # Nothing to match on, and a blank id must never join unrelated
                # sightings: fall through like a rule whose conditions failed.
                continue
            target = tracked_opportunity(
                uow,
                pipeline_id=rule["pipeline_id"],
                field=rule["match_field"],
                value=key,
            )
            if target:
                _attach(ctx, uow, target, obs["id"], decision)
                return
        elif rule["action"] == "attach_to_open_opportunity" and obs["party_ref"]:
            aliases = h.party_read(ctx, uow, obs["party_ref"], aliases=True)
            refs = [aliases.canonical_ref, *aliases.alias_refs]
            target = (
                uow.connection.execute(
                    select(p.opportunity)
                    .join(
                        p.opportunity_party,
                        p.opportunity_party.c.opportunity_id == p.opportunity.c.id,
                    )
                    .where(
                        p.opportunity.c.pipeline_id == rule["pipeline_id"],
                        p.opportunity.c.disposition_outcome.is_(None),
                        p.opportunity_party.c.party_ref.in_(refs),
                        p.opportunity_party.c.removed_at.is_(None),
                    )
                    .order_by(
                        p.opportunity.c.created_at.desc(), p.opportunity.c.id.desc()
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if target:
                _attach(ctx, uow, target, obs["id"], decision)
                return
        answer = call_in_transaction(
            ctx,
            uow,
            "leads.opportunity.create_from_observation",
            dict(
                observation_ref=ref("observation", obs["id"]),
                pipeline_id=rule["pipeline_id"],
            ),
        )
        created = answer.model_dump()
        _record_route(uow, created["ref"], obs["id"], decision)
        _first_followup(ctx, uow, created, rule["pipeline_id"], obs["received_at"])
        return


FOLLOWUP_ACTION: Final = "Reply to the inquiry"


def business_days_after(start: date, days: int) -> date:
    """The date ``days`` working days after ``start``, skipping Saturday and Sunday."""
    current = start
    while days > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days -= 1
    return current


def _first_followup(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    created: dict[str, Any],
    pipeline_id: UUID,
    received_at: Any,
) -> None:
    """Give a new inquiry opportunity its first dated follow-up, when the workspace
    asks for one. Counted from the UTC date rheoStream received the inquiry. A
    repeat that attaches to an existing opportunity never reaches here, so it
    leaves that opportunity's follow-up alone."""
    days = resolve(
        workspace_id=ctx.workspace_id,
        source=TransactionBoundOverrideSource(uow, workspace_id=ctx.workspace_id),
    ).get_int(INQUIRY_FOLLOWUP_KEY)
    if days <= 0:
        return
    preset: UUID = uow.connection.execute(
        select(p.pipeline.c.preset_id).where(p.pipeline.c.id == pipeline_id)
    ).scalar_one()
    if preset == h.JOBSEARCH_PRESET_ID:
        return
    call_in_transaction(
        ctx,
        uow,
        "leads.followup.schedule",
        dict(
            ref=created["ref"],
            revision=created["revision"],
            action=FOLLOWUP_ACTION,
            due_on=business_days_after(
                received_at.astimezone(UTC).date(), days
            ).isoformat(),
        ),
    )


def _attach(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    target: Any,
    observation_id: Any,
    decision: str,
) -> None:
    answer = call_in_transaction(
        ctx,
        uow,
        "leads.opportunity.attach_observation",
        dict(
            ref=ref("opportunity", target["id"]),
            revision=target["revision"],
            observation_ref=ref("observation", observation_id),
        ),
    )
    _record_route(uow, answer.model_dump()["ref"], observation_id, decision)


def _record_route(
    uow: UnitOfWork, reference: str, observation_id: object, decision: str
) -> None:
    from rheo_contracts import RecordRef

    uow.connection.execute(
        update(p.opportunity_observation)
        .where(
            p.opportunity_observation.c.opportunity_id == RecordRef.parse(reference).id,
            p.opportunity_observation.c.observation_id == observation_id,
        )
        .values(decision=decision)
    )
