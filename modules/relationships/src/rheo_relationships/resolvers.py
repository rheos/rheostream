"""A one-hop alias resolves to its survivor; restricted fields have a model loader."""

from collections.abc import Mapping

from rheo_contracts import RecordRef, Role, WorkspaceContext
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import LIVE, RecordHead, Unavailable, UnitOfWork

from rheo_relationships.contracts import PartyInput
from rheo_relationships.service import canonical, party_get


def resolve_party(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> RecordHead | Unavailable:
    if (
        ref.module != "relationships"
        or ref.record_type != "party"
        or "relationships" not in ctx.enabled_modules
        or ctx.role not in {Role.OWNER, Role.MEMBER, Role.SERVICE}
    ):
        return Unavailable(ref.format(), "not_found")
    try:
        row = canonical(uow, ref.id)
    except OperationRefused:
        return Unavailable(ref.format(), "not_found")
    return RecordHead(
        ref=ref,
        display=row["display_name"],
        readable=True,
        state=LIVE,
        revision=row["revision"],
    )


def load_for_model(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> Mapping[str, object] | None:
    if isinstance(resolve_party(ctx, uow, ref), Unavailable):
        return None
    result = party_get(ctx, uow, PartyInput(ref=ref))
    return {
        key: result.model_dump(mode="json")[key]
        for key in ("kind", "canonical_ref", "display_name", "contact_points")
    }
