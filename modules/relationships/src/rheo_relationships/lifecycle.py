"""Approved erasure removes survivor and aliases, preserving content-free history."""

from datetime import UTC, datetime

from rheo_contracts import RecordRef, Role, StaleRecord, WorkspaceContext
from rheo_core.boundary.context import Refusal
from rheo_core.deletion import (
    DeleteAuthorization,
    Disposition,
    RemovedOwnedRecords,
)
from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
from rheo_core.storage.backend import UnitOfWork
from sqlalchemy import delete, or_, select, update

from rheo_relationships.storage import tables as t


def authorize_delete(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef, *, disposition: Disposition
) -> DeleteAuthorization | Refusal:
    if (
        ref.module != "relationships"
        or ref.record_type != "party"
        or "relationships" not in ctx.enabled_modules
        or ctx.role is not Role.OWNER
        or disposition is not Disposition.USER_ERASURE
    ):
        return Refusal("not_found", "party unavailable")
    row = (
        uow.connection.execute(select(t.party).where(t.party.c.id == ref.id))
        .mappings()
        .one_or_none()
    )
    if row is None:
        return Refusal("not_found", "party unavailable")
    if row["merged_into_id"]:
        return Refusal("party_is_alias", "delete the canonical party")
    return DeleteAuthorization(ref, row["revision"])


def delete_owned(
    ctx: WorkspaceContext, uow: UnitOfWork, authorization: DeleteAuthorization
) -> RemovedOwnedRecords:
    lock_workspace_lifecycle(uow.connection)
    rows = (
        uow.connection.execute(
            select(t.party)
            .where(
                or_(
                    t.party.c.id == authorization.ref.id,
                    t.party.c.merged_into_id == authorization.ref.id,
                )
            )
            .with_for_update()
        )
        .mappings()
        .all()
    )
    target = next((row for row in rows if row["id"] == authorization.ref.id), None)
    if (
        target is None
        or target["merged_into_id"]
        or target["revision"] != authorization.revision
    ):
        raise StaleRecord("party changed since approval")
    ids = [row["id"] for row in rows]
    uow.connection.execute(
        delete(t.contact_point).where(t.contact_point.c.party_id.in_(ids))
    )
    uow.connection.execute(
        delete(t.affiliation).where(
            or_(
                t.affiliation.c.person_id.in_(ids),
                t.affiliation.c.organization_id.in_(ids),
            )
        )
    )
    uow.connection.execute(
        update(t.merge_record)
        .where(
            or_(
                t.merge_record.c.survivor_id.in_(ids),
                t.merge_record.c.merged_id.in_(ids),
            )
        )
        .values(evidence=None, unmerge_note=None)
    )
    uow.connection.execute(
        update(t.review_candidate)
        .where(
            or_(
                t.review_candidate.c.party_id.in_(ids),
                t.review_candidate.c.candidate_party_id.in_(ids),
            )
        )
        .values(state="withdrawn", resolved_at=datetime.now(UTC))
    )
    # Remove aliases first, then the revision-bound survivor. History has no party FK.
    uow.connection.execute(
        delete(t.party).where(
            t.party.c.id.in_(ids), t.party.c.id != authorization.ref.id
        )
    )
    removed = uow.connection.execute(
        delete(t.party)
        .where(
            t.party.c.id == authorization.ref.id,
            t.party.c.revision == authorization.revision,
        )
        .returning(t.party.c.id)
    ).scalar_one_or_none()
    if removed is None:
        raise StaleRecord("party changed since approval")
    return RemovedOwnedRecords(
        records=tuple(
            DeleteAuthorization(
                RecordRef(module="relationships", record_type="party", id=row["id"]),
                row["revision"],
            )
            for row in rows
        )
    )
