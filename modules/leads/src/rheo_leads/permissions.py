"""Purpose permission is explicit; captured interest never grants it."""

from pydantic import BaseModel
from rheo_contracts import WorkspaceContext
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rheo_leads import pipeline_common as h
from rheo_leads import pipeline_contracts as c
from rheo_leads.events import publish_event
from rheo_leads.storage import pipeline as p


def aliases(ctx: WorkspaceContext, uow: UnitOfWork, reference: str) -> list[str]:
    result = h.party_read(ctx, uow, reference, aliases=True)
    return [result.canonical_ref, *result.alias_refs]


def check(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.PermissionCheck
) -> c.PermissionOutput:
    h.require(ctx)
    refs = aliases(ctx, uow, m.party_ref.format())
    if uow.connection.execute(
        select(p.contact_suppression.c.party_ref)
        .where(p.contact_suppression.c.party_ref.in_(refs))
        .limit(1)
    ).first():
        return c.PermissionOutput(state="suppressed")
    q = select(p.contact_permission).where(
        p.contact_permission.c.party_ref.in_(refs),
        p.contact_permission.c.purpose == m.purpose.value,
    )
    # A purpose-only read is conservative: a withdrawal on any channel denies it.
    if m.channel is not None:
        q = q.where(
            or_(
                p.contact_permission.c.channel == m.channel,
                p.contact_permission.c.channel.is_(None),
            )
        )
    rows = list(uow.connection.execute(q).mappings())
    if not rows:
        return c.PermissionOutput(state="absent")
    if m.channel is None:
        # A newer phone grant cannot erase a still-effective email withdrawal.
        # An explicit all-channel grant can supersede earlier withdrawals.
        channels = {r["channel"] for r in rows if r["channel"] is not None} or {None}
        for channel in channels:
            applicable = [r for r in rows if r["channel"] in {None, channel}]
            latest = max(
                applicable,
                key=lambda r: (r["withdrawn_at"] or r["granted_at"], r["id"]),
            )
            if latest["withdrawn_at"] is not None:
                return c.PermissionOutput(state="withdrawn")
        return c.PermissionOutput(state="permitted")
    latest = max(rows, key=lambda r: (r["withdrawn_at"] or r["granted_at"], r["id"]))
    if latest["withdrawn_at"] is not None:
        return c.PermissionOutput(state="withdrawn")
    return c.PermissionOutput(state="permitted")


def record(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.PermissionRecord
) -> c.PermissionOutput:
    h.lock(uow)
    aliases(ctx, uow, m.party_ref.format())
    evidence = None
    if m.evidence_observation_ref:
        observation = h.observation(uow, m.evidence_observation_ref.id)
        refs = aliases(ctx, uow, m.party_ref.format())
        if observation["party_ref"] not in refs:
            from rheo_core.operations.refusals import OperationRefused

            raise OperationRefused(
                "evidence_mismatch", "permission evidence belongs to another party"
            )
        evidence = observation["id"]
    uow.connection.execute(
        insert(p.contact_permission).values(
            id=uuid7(),
            party_ref=m.party_ref.format(),
            purpose=m.purpose.value,
            channel=m.channel,
            recipient_scope=m.recipient_scope,
            disclosure_version=m.disclosure_version,
            evidence_observation_id=evidence,
            **h.actor(ctx, "granted"),
        )
    )
    return check(
        ctx,
        uow,
        c.PermissionCheck.model_validate(
            m.model_dump(include={"party_ref", "purpose", "channel"})
        ),
    )


def withdraw(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.WithdrawInput
) -> c.PermissionOutput:
    h.lock(uow)
    refs = aliases(ctx, uow, m.party_ref.format())
    # Always retain a withdrawal marker, including a channel-specific withdrawal
    # of an all-channel grant. Historical grants remain explicit evidence.
    q = update(p.contact_permission).where(
        p.contact_permission.c.party_ref.in_(refs),
        p.contact_permission.c.purpose == m.purpose.value,
        p.contact_permission.c.withdrawn_at.is_(None),
    )
    if m.channel:
        q = q.where(
            or_(
                p.contact_permission.c.channel == m.channel,
                p.contact_permission.c.channel.is_(None),
            )
        )
    uow.connection.execute(
        q.values(**h.actor(ctx, "withdrawn"), withdrawn_reason=m.reason)
    )
    uow.connection.execute(
        insert(p.contact_permission).values(
            id=uuid7(),
            party_ref=m.party_ref.format(),
            purpose=m.purpose.value,
            channel=m.channel,
            recipient_scope="workspace",
            **h.actor(ctx, "withdrawn"),
            withdrawn_reason=m.reason,
        )
    )
    permission_event(ctx, uow, "withdrawn", m.party_ref.format())
    return c.PermissionOutput(state="withdrawn")


def suppress(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.SuppressInput
) -> c.PermissionOutput:
    h.lock(uow)
    aliases(ctx, uow, m.party_ref.format())
    values = dict(
        party_ref=m.party_ref.format(), reason=m.reason, **h.actor(ctx, "suppressed")
    )
    uow.connection.execute(
        pg_insert(p.contact_suppression)
        .values(**values)
        .on_conflict_do_update(index_elements=["party_ref"], set_=values)
    )
    permission_event(ctx, uow, "suppressed", m.party_ref.format())
    return c.PermissionOutput(state="suppressed")


def permission_event(
    ctx: WorkspaceContext, uow: UnitOfWork, kind: str, reference: str
) -> None:
    publish_event(
        ctx,
        uow,
        event_type="leads.contact_permission." + kind,
        subject=reference,
        data=c.ReferenceEvent(ref=reference),
    )


class ContactPermissionGuard:
    """External-action guard supplied by Leads; a destination must attach it.

    No production destination is registered in this release. The recording sink
    exercises this guard after approval binding and before its test-only effect.
    A purpose alone never grants permission; execution needs a currently live grant.
    """

    name = "ContactPermissionGuard"

    def check(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        approval: object,
        model_input: object,
        now: object,
    ) -> str | None:
        from rheo_core.operations.refusals import OperationRefused

        try:
            if not isinstance(model_input, BaseModel):
                return "contact_permission_unavailable"
            model = c.PermissionCheck.model_validate(
                model_input.model_dump(include={"party_ref", "purpose", "channel"})
            )
            # Serialize withdrawal and execution within the same workspace.
            h.lock(uow)
            return (
                None
                if check(ctx, uow, model).state == "permitted"
                else "contact_permission_required"
            )
        except (OperationRefused, ValueError, AttributeError):
            return "contact_permission_unavailable"
