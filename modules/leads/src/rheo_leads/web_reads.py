"""Display projections for the browser; never expose connection credentials/payloads."""

from uuid import UUID

from rheo_contracts import (
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import select

from rheo_leads import pipeline_common as h
from rheo_leads import pipeline_contracts as c
from rheo_leads.references import identifier, ref
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t


class ReceiptInput(c.Strict):
    receipt_ref: str


def catalog(ctx: WorkspaceContext, uow: UnitOfWork, m: c.Empty) -> c.ItemsOutput:
    h.require(ctx)
    items = []
    for row in uow.connection.execute(
        select(t.funnel).where(t.funnel.c.archived_at.is_(None)).order_by(t.funnel.c.id)
    ).mappings():
        items.append({"kind": "funnel", "ref": ref("funnel", row["id"]), **dict(row)})
    for row in uow.connection.execute(
        select(p.pipeline).order_by(p.pipeline.c.id)
    ).mappings():
        preset = h.row(uow, p.pipeline_preset, row["preset_id"])
        stages = [
            dict(s)
            for s in uow.connection.execute(
                select(p.preset_stage)
                .where(
                    p.preset_stage.c.preset_id == row["preset_id"],
                    p.preset_stage.c.version == preset["current_version"],
                )
                .order_by(p.preset_stage.c.ordinal)
            ).mappings()
        ]
        items.append({"kind": "pipeline", **dict(row), "stages": stages})
    if ctx.role is Role.OWNER:
        # Explicit projection excludes signing handles and identity policy.
        columns = [
            t.intake_connection.c[name]
            for name in (
                "id",
                "name",
                "transport",
                "state",
                "funnel_id",
                "mapping_version",
                "signing_key_generation",
            )
        ]
        for row in uow.connection.execute(
            select(*columns).order_by(t.intake_connection.c.id)
        ).mappings():
            rules = [
                dict(r)
                for r in uow.connection.execute(
                    select(p.routing_rule)
                    .where(
                        p.routing_rule.c.connection_id == row["id"],
                    )
                    .order_by(p.routing_rule.c.ordinal)
                ).mappings()
            ]
            for rule in rules:
                rule["conditions"] = [
                    dict(r)
                    for r in uow.connection.execute(
                        select(p.routing_condition)
                        .where(p.routing_condition.c.rule_id == rule["id"])
                        .order_by(p.routing_condition.c.ordinal)
                    ).mappings()
                ]
            items.append(
                {
                    "kind": "connection",
                    "ref": ref("intake_connection", row["id"]),
                    **dict(row),
                    "rules": rules,
                }
            )
    return c.ItemsOutput(items=items)


def receipt(ctx: WorkspaceContext, uow: UnitOfWork, m: ReceiptInput) -> c.RecordOutput:
    h.require(ctx)
    row = h.row(uow, t.delivery_receipt, identifier(m.receipt_ref, "delivery_receipt"))
    linked_ids: list[UUID] = list(
        uow.connection.execute(
            select(p.opportunity_observation.c.opportunity_id)
            .where(p.opportunity_observation.c.observation_id == row["id"])
            .order_by(p.opportunity_observation.c.opportunity_id)
        ).scalars()
    )
    links = [ref("opportunity", value) for value in linked_ids]
    exists = uow.connection.execute(
        select(t.observation.c.id).where(t.observation.c.id == row["id"])
    ).scalar_one_or_none()
    return c.RecordOutput(
        ref=m.receipt_ref,
        data={
            "state": "erased"
            if row["state_detail"] == "observation_deleted"
            else row["state"],
            "received_at": row["received_at"],
            "processed_at": row["processed_at"],
            "opportunity_refs": links,
            "observation_ref": ref("observation", row["id"])
            if exists is not None
            else None,
        },
    )


OPERATIONS = (
    (
        OperationDeclaration(
            name="leads.ui.catalog",
            safety_class=SafetyClass.READ,
            idempotency=Idempotency.NONE,
            roles=frozenset({Role.OWNER, Role.MEMBER}),
            input_model=c.Empty,
            output=c.ItemsOutput,
        ),
        catalog,
    ),
    (
        OperationDeclaration(
            name="leads.intake.receipt",
            safety_class=SafetyClass.READ,
            idempotency=Idempotency.NONE,
            roles=frozenset({Role.OWNER, Role.MEMBER}),
            input_model=ReceiptInput,
            output=c.RecordOutput,
        ),
        receipt,
    ),
)
