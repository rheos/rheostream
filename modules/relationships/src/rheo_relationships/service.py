"""Workspace-local party services, with one lifecycle lock for all identity writes.

The same lock as approved deletion orders resolution, merge and erasure. Reads do
not take it. No operation commits: dispatch owns the transaction and audit boundary.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from rheo_contracts import StaleRecord, WorkspaceContext
from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.storage.backend import UnitOfWork
from sqlalchemy import func, or_, select, update

from rheo_relationships import contracts as c
from rheo_relationships.normalization import normalize
from rheo_relationships.references import ref
from rheo_relationships.storage import tables as t


def refuse(state: str) -> None:
    raise OperationRefused(state, state.replace("_", " "))


def get_row(uow: UnitOfWork, identifier: UUID, *, live: bool = False) -> dict[str, Any]:
    row = (
        uow.connection.execute(select(t.party).where(t.party.c.id == identifier))
        .mappings()
        .one_or_none()
    )
    if row is None:
        refuse("not_found")
    assert row is not None
    if live and row["merged_into_id"] is not None:
        refuse("party_is_alias")
    return dict(row)


def canonical(uow: UnitOfWork, identifier: UUID) -> dict[str, Any]:
    row = get_row(uow, identifier)
    return (
        get_row(uow, row["merged_into_id"], live=True) if row["merged_into_id"] else row
    )


def bump(uow: UnitOfWork, identifier: UUID, revision: int | None = None) -> int:
    query = update(t.party).where(t.party.c.id == identifier)
    if revision is not None:
        query = query.where(t.party.c.revision == revision)
    result = uow.connection.execute(
        query.values(
            revision=t.party.c.revision + 1, updated_at=datetime.now(UTC)
        ).returning(t.party.c.revision)
    ).scalar_one_or_none()
    if result is None:
        raise StaleRecord("party revision changed")
    return int(result)


def checked(uow: UnitOfWork, identifier: UUID, revision: int) -> dict[str, Any]:
    row = get_row(uow, identifier, live=True)
    if row["revision"] != revision:
        raise StaleRecord("party revision changed")
    return row


def aliases(uow: UnitOfWork, identifier: UUID) -> list[str]:
    values: list[UUID] = list(
        uow.connection.execute(
            select(t.party.c.id)
            .where(t.party.c.merged_into_id == identifier)
            .order_by(t.party.c.id)
        ).scalars()
    )
    return [ref("party", value) for value in values]


def party_get(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.PartyInput
) -> c.PartyOutput:
    row = canonical(uow, data.ref.id)
    identifier = row["id"]
    return c.PartyOutput(
        ref=data.ref.format(),
        kind=row["kind"],
        display_name=row["display_name"],
        canonical_ref=ref("party", identifier),
        alias_refs=aliases(uow, identifier),
        revision=row["revision"],
        contact_points=[
            dict(r)
            for r in uow.connection.execute(
                select(t.contact_point)
                .where(
                    t.contact_point.c.party_id == identifier,
                    t.contact_point.c.retired_at.is_(None),
                )
                .order_by(t.contact_point.c.id)
            ).mappings()
        ],
        affiliations=[
            dict(r)
            for r in uow.connection.execute(
                select(t.affiliation)
                .where(
                    or_(
                        t.affiliation.c.person_id == identifier,
                        t.affiliation.c.organization_id == identifier,
                    )
                )
                .order_by(t.affiliation.c.id)
            ).mappings()
        ],
    )


def party_aliases(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.PartyInput
) -> c.AliasesOutput:
    row = canonical(uow, data.ref.id)
    return c.AliasesOutput(
        canonical_ref=ref("party", row["id"]), alias_refs=aliases(uow, row["id"])
    )


def party_find(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.FindInput
) -> c.ItemsOutput:
    keys = {
        normalize(kind, data.query) for kind in ("email", "phone", "external_subject")
    }
    contacts = select(t.contact_point.c.party_id).where(
        t.contact_point.c.retired_at.is_(None),
        t.contact_point.c.normalized_value.in_(keys),
    )
    score = func.similarity(func.lower(t.party.c.display_name), data.query.casefold())
    query = select(t.party).where(
        t.party.c.merged_into_id.is_(None),
        or_(t.party.c.id.in_(contacts), score >= 0.3),
    )
    if data.kind:
        query = query.where(t.party.c.kind == data.kind)
    rows = uow.connection.execute(
        query.order_by(score.desc(), t.party.c.id).limit(data.limit)
    ).mappings()
    return c.ItemsOutput(
        items=[
            {
                "ref": ref("party", r["id"]),
                "kind": r["kind"],
                "display_name": r["display_name"],
                "revision": r["revision"],
            }
            for r in rows
        ]
    )


def create_row(uow: UnitOfWork, kind: str, name: str) -> UUID:
    identifier = uuid7()
    now = datetime.now(UTC)
    uow.connection.execute(
        t.party.insert().values(
            id=identifier,
            kind=kind,
            display_name=name,
            revision=1,
            created_at=now,
            updated_at=now,
        )
    )
    return identifier


def party_create(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.CreateInput
) -> c.PartyOutput:
    lock_workspace_lifecycle(uow.connection)
    identifier = create_row(uow, data.kind, data.display_name)
    return party_get(
        ctx, uow, c.PartyInput.model_validate({"ref": ref("party", identifier)})
    )


def party_update(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.UpdateInput
) -> c.PartyOutput:
    lock_workspace_lifecycle(uow.connection)
    checked(uow, data.ref.id, data.revision)
    bump(uow, data.ref.id, data.revision)
    uow.connection.execute(
        update(t.party)
        .where(t.party.c.id == data.ref.id)
        .values(display_name=data.display_name)
    )
    return party_get(ctx, uow, c.PartyInput(ref=data.ref))


def add_contact(
    uow: UnitOfWork,
    party_id: UUID,
    kind: str,
    value: str,
    *,
    verification: str = "unverified",
    namespace: str | None = None,
    provenance: str = "person",
    evidence: str | None = None,
) -> UUID:
    normalized = normalize(kind, value)
    if not normalized:
        refuse("input_invalid")
    identifier = uuid7()
    uow.connection.execute(
        t.contact_point.insert().values(
            id=identifier,
            party_id=party_id,
            kind=kind,
            value=value,
            normalized_value=normalized,
            namespace=namespace,
            verification=verification,
            provenance_kind=provenance,
            provenance_ref=evidence,
            created_at=datetime.now(UTC),
        )
    )
    return identifier


def contact_add(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.AddContactInput
) -> c.ChangeOutput:
    lock_workspace_lifecycle(uow.connection)
    checked(uow, data.party_ref.id, data.revision)
    identifier = add_contact(uow, data.party_ref.id, data.kind, data.value)
    return c.ChangeOutput(
        id=identifier, revision=bump(uow, data.party_ref.id, data.revision)
    )


def contact_change(
    uow: UnitOfWork, data: c.ContactInput, *, retire: bool
) -> c.ChangeOutput:
    lock_workspace_lifecycle(uow.connection)
    checked(uow, data.party_ref.id, data.revision)
    row = (
        uow.connection.execute(
            select(t.contact_point).where(
                t.contact_point.c.id == data.contact_point_id,
                t.contact_point.c.party_id == data.party_ref.id,
                t.contact_point.c.retired_at.is_(None),
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        refuse("not_found")
    assert row is not None
    if not retire and row["verification"] in ("source_verified", "authenticated"):
        # Confirmation does not discard the stronger source-bound identity.
        return c.ChangeOutput(id=data.contact_point_id, revision=data.revision)
    changes = (
        {"retired_at": datetime.now(UTC)} if retire else {"verification": "confirmed"}
    )
    uow.connection.execute(
        update(t.contact_point)
        .where(t.contact_point.c.id == data.contact_point_id)
        .values(**changes)
    )
    return c.ChangeOutput(
        id=data.contact_point_id, revision=bump(uow, data.party_ref.id, data.revision)
    )


def contact_confirm(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.ContactInput
) -> c.ChangeOutput:
    return contact_change(uow, data, retire=False)


def contact_retire(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.ContactInput
) -> c.ChangeOutput:
    return contact_change(uow, data, retire=True)


def add_affiliation(
    uow: UnitOfWork,
    person: UUID,
    organization: UUID,
    *,
    valid_from: Any = None,
    valid_to: Any = None,
    role_label: str | None = None,
    evidence: str | None = None,
) -> UUID:
    if (
        get_row(uow, person, live=True)["kind"] != "person"
        or get_row(uow, organization, live=True)["kind"] != "organization"
    ):
        refuse("party_kind_mismatch")
    if valid_from and valid_to and valid_to < valid_from:
        refuse("input_invalid")
    duplicate = uow.connection.execute(
        select(t.affiliation.c.id).where(
            t.affiliation.c.person_id == person,
            t.affiliation.c.organization_id == organization,
            t.affiliation.c.valid_from.is_not_distinct_from(valid_from),
        )
    ).scalar_one_or_none()
    if duplicate:
        refuse("affiliation_conflict")
    identifier = uuid7()
    uow.connection.execute(
        t.affiliation.insert().values(
            id=identifier,
            person_id=person,
            organization_id=organization,
            valid_from=valid_from,
            valid_to=valid_to,
            role_label=role_label,
            provenance_ref=evidence,
            created_at=datetime.now(UTC),
        )
    )
    bump(uow, organization)
    return identifier


def affiliation_add(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.AddAffiliationInput
) -> c.ChangeOutput:
    lock_workspace_lifecycle(uow.connection)
    checked(uow, data.person_ref.id, data.revision)
    identifier = add_affiliation(
        uow,
        data.person_ref.id,
        data.organization_ref.id,
        valid_from=data.valid_from,
        valid_to=data.valid_to,
        role_label=data.role_label,
    )
    return c.ChangeOutput(
        id=identifier, revision=bump(uow, data.person_ref.id, data.revision)
    )


def affiliation_end(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.EndAffiliationInput
) -> c.ChangeOutput:
    lock_workspace_lifecycle(uow.connection)
    checked(uow, data.person_ref.id, data.revision)
    row = (
        uow.connection.execute(
            select(t.affiliation).where(
                t.affiliation.c.id == data.affiliation_id,
                t.affiliation.c.person_id == data.person_ref.id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        refuse("not_found")
    assert row is not None
    if row["valid_to"] is not None or (
        row["valid_from"] and data.valid_to < row["valid_from"]
    ):
        refuse("input_invalid")
    uow.connection.execute(
        update(t.affiliation)
        .where(t.affiliation.c.id == data.affiliation_id)
        .values(valid_to=data.valid_to)
    )
    bump(uow, row["organization_id"])
    return c.ChangeOutput(
        id=data.affiliation_id, revision=bump(uow, data.person_ref.id, data.revision)
    )
