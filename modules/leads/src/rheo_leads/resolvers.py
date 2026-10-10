"""Generic labels only: resolution never emits captured contact information."""

from rheo_contracts import RecordRef, Role, WorkspaceContext
from rheo_core.refs.resolver import LIVE, RecordHead, Unavailable, UnitOfWork
from sqlalchemy import select

from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t

RECORD_TABLES = {
    table.name: table
    for table in (
        t.funnel,
        t.campaign,
        t.intake_connection,
        t.field_mapping,
        t.delivery_receipt,
        t.observation,
        p.pipeline,
        p.pipeline_preset,
        p.opportunity,
        p.qualification,
        p.handoff,
        p.draft,
    )
}


def resolve_record(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> RecordHead | Unavailable:
    """Resolve only in the supplied workspace and enabled module."""
    table = RECORD_TABLES.get(ref.record_type)
    if (
        ref.module != "leads"
        or table is None
        or "leads" not in ctx.enabled_modules
        or ctx.role not in {Role.OWNER, Role.MEMBER, Role.SERVICE}
    ):
        return Unavailable(ref.format(), "not_found")
    query = select(table).where(table.c.id == ref.id)
    if ref.record_type == "field_mapping":
        query = query.order_by(table.c.version.desc())
    row = uow.connection.execute(query.limit(1)).mappings().one_or_none()
    if row is None:
        return Unavailable(ref.format(), "not_found")
    return RecordHead(
        ref=ref,
        display=f"{ref.record_type.replace('_', ' ').title()} {ref.id}",
        readable=True,
        state=LIVE,
        revision=row.get("revision", row.get("version", 1)),
    )
