"""References resolve to a generic label, never to a title or body."""

from rheo_contracts import RecordRef, Role, WorkspaceContext
from rheo_core.refs.resolver import LIVE, RecordHead, Unavailable, UnitOfWork
from sqlalchemy import select

from rheo_current.lifecycle import TABLES
from rheo_current.references import MODULE_ID


def resolve_record(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> RecordHead | Unavailable:
    table = TABLES.get(ref.record_type)
    if (
        ref.module != MODULE_ID
        or table is None
        or MODULE_ID not in ctx.enabled_modules
        or ctx.role not in {Role.OWNER, Role.MEMBER, Role.SERVICE}
    ):
        return Unavailable(ref.format(), "not_found")
    revision = uow.connection.execute(
        select(table.c.revision).where(table.c.id == ref.id)
    ).scalar_one_or_none()
    if revision is None:
        return Unavailable(ref.format(), "not_found")
    return RecordHead(
        ref=ref,
        display=f"{ref.record_type.title()} {ref.id}",
        readable=True,
        state=LIVE,
        revision=revision,
    )
