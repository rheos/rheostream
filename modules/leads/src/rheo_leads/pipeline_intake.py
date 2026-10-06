"""Intake steps 4–6: source-authenticated party identity and explicit routing."""

from typing import TYPE_CHECKING

from rheo_contracts import WorkspaceContext
from rheo_core.operations.transaction import call_in_transaction
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import select, update

from rheo_leads import pipeline_common as h
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
        if rule["action"] == "attach_to_open_opportunity" and obs["party_ref"]:
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
                answer = call_in_transaction(
                    ctx,
                    uow,
                    "leads.opportunity.attach_observation",
                    dict(
                        ref=ref("opportunity", target["id"]),
                        revision=target["revision"],
                        observation_ref=ref("observation", obs["id"]),
                    ),
                )
                _record_route(uow, answer.model_dump()["ref"], obs["id"], decision)
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
        _record_route(uow, answer.model_dump()["ref"], obs["id"], decision)
        return


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
