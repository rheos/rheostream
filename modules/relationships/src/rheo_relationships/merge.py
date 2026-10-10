"""Alias merges restore recorded ownership; no outside reference is rewritten."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
from rheo_core.events import NewEvent, publish
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from sqlalchemy import or_, select, update

from rheo_relationships import contracts as c
from rheo_relationships.references import ref
from rheo_relationships.service import add_affiliation, bump, checked, get_row, refuse
from rheo_relationships.storage import tables as t


def collision_check(
    uow: UnitOfWork, rows: list[dict[str, Any]], column: str, target: UUID
) -> None:
    for row in rows:
        person = target if column == "person_id" else row["person_id"]
        organization = target if column == "organization_id" else row["organization_id"]
        existing = uow.connection.execute(
            select(t.affiliation.c.id).where(
                t.affiliation.c.person_id == person,
                t.affiliation.c.organization_id == organization,
                t.affiliation.c.valid_from.is_not_distinct_from(row["valid_from"]),
                t.affiliation.c.id != row["id"],
            )
        ).scalar_one_or_none()
        if existing:
            refuse("affiliation_conflict")


def merge(ctx: WorkspaceContext, uow: UnitOfWork, data: c.MergeInput) -> c.MergeOutput:
    lock_workspace_lifecycle(uow.connection)
    if data.source_ref.id == data.target_ref.id:
        refuse("party_self_merge")
    source = checked(uow, data.source_ref.id, data.source_revision)
    target = checked(uow, data.target_ref.id, data.target_revision)
    if source["kind"] != target["kind"]:
        refuse("party_kind_mismatch")
    if not isinstance(uow, HandlerUnitOfWork) or uow.consumers is None:
        refuse("consumers_missing")
    assert isinstance(uow, HandlerUnitOfWork) and uow.consumers is not None
    source_id, target_id = source["id"], target["id"]
    affiliation_column = (
        "person_id" if source["kind"] == "person" else "organization_id"
    )
    affiliations = [
        dict(row)
        for row in uow.connection.execute(
            select(t.affiliation).where(
                t.affiliation.c[affiliation_column] == source_id
            )
        ).mappings()
    ]
    collision_check(uow, affiliations, affiliation_column, target_id)
    identifier = uuid7()
    now = datetime.now(UTC)
    uow.connection.execute(
        t.merge_record.insert().values(
            id=identifier,
            survivor_id=target_id,
            merged_id=source_id,
            evidence=data.evidence,
            evidence_refs=data.evidence_refs,
            actor_kind=ctx.actor.kind.value,
            actor_id=ctx.actor.id,
            merged_at=now,
        )
    )

    def move(table: Any, column: str, kind: str, ids: list[UUID]) -> None:
        if not ids:
            return
        uow.connection.execute(
            t.merge_record_item.insert(),
            [
                {
                    "merge_record_id": identifier,
                    "kind": kind,
                    "item_id": item,
                    "previous_owner_id": source_id,
                }
                for item in ids
            ],
        )
        uow.connection.execute(
            update(table).where(table.c.id.in_(ids)).values(**{column: target_id})
        )

    contact_ids: list[UUID] = list(
        uow.connection.execute(
            select(t.contact_point.c.id).where(
                t.contact_point.c.party_id == source_id,
                t.contact_point.c.retired_at.is_(None),
            )
        ).scalars()
    )
    move(t.contact_point, "party_id", "contact_point", contact_ids)
    move(
        t.affiliation,
        affiliation_column,
        "affiliation_person"
        if affiliation_column == "person_id"
        else "affiliation_organization",
        [row["id"] for row in affiliations],
    )
    alias_ids: list[UUID] = list(
        uow.connection.execute(
            select(t.party.c.id).where(t.party.c.merged_into_id == source_id)
        ).scalars()
    )
    move(t.party, "merged_into_id", "alias", alias_ids)
    uow.connection.execute(
        update(t.party)
        .where(t.party.c.id == source_id)
        .values(merged_into_id=target_id)
    )
    for party_id in (source_id, target_id, *alias_ids):
        bump(uow, party_id)
    # Both positions matter because same-party candidates use canonical pair order.
    uow.connection.execute(
        update(t.review_candidate)
        .where(
            t.review_candidate.c.state == "open",
            or_(
                t.review_candidate.c.party_id.in_((source_id, *alias_ids)),
                t.review_candidate.c.candidate_party_id.in_((source_id, *alias_ids)),
            ),
        )
        .values(state="withdrawn", resolved_at=now)
    )
    publish(
        ctx,
        uow,
        NewEvent(
            type="relationships.party.merged",
            schema_version=1,
            subject_ref=data.target_ref.format(),
            subject_revision=target["revision"] + 1,
            data={
                "survivor_ref": data.target_ref.format(),
                "merged_ref": data.source_ref.format(),
                "merge_record_ref": ref("merge_record", identifier),
            },
        ),
        now=now,
        consumers=uow.consumers,
    )
    return c.MergeOutput(merge_record_ref=ref("merge_record", identifier))


def unmerge(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.UnmergeInput
) -> c.MergeOutput:
    if not isinstance(uow, HandlerUnitOfWork) or uow.consumers is None:
        refuse("consumers_missing")
    assert isinstance(uow, HandlerUnitOfWork) and uow.consumers is not None
    lock_workspace_lifecycle(uow.connection)
    record = (
        uow.connection.execute(
            select(t.merge_record).where(
                t.merge_record.c.id == data.merge_record_ref.id
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        refuse("not_found")
    assert record is not None
    if record["unmerged_at"]:
        refuse("merge_already_unmerged")
    source = get_row(uow, record["merged_id"])
    target = get_row(uow, record["survivor_id"])
    if target["merged_into_id"] is not None or source["merged_into_id"] != target["id"]:
        refuse("survivor_merged_onward")
    items = (
        uow.connection.execute(
            select(t.merge_record_item).where(
                t.merge_record_item.c.merge_record_id == record["id"]
            )
        )
        .mappings()
        .all()
    )
    changed = {source["id"], target["id"]}
    for item in items:
        table, column = {
            "contact_point": (t.contact_point, "party_id"),
            "affiliation_person": (t.affiliation, "person_id"),
            "affiliation_organization": (t.affiliation, "organization_id"),
            "alias": (t.party, "merged_into_id"),
        }[item["kind"]]
        row = (
            uow.connection.execute(select(table).where(table.c.id == item["item_id"]))
            .mappings()
            .one_or_none()
        )
        # Erasure must never be undone; an erased child is not recreated.
        if row is None:
            continue
        if row[column] != target["id"]:
            refuse("merge_item_changed")
        if table is t.affiliation:
            collision_check(uow, [dict(row)], column, item["previous_owner_id"])
        uow.connection.execute(
            update(table)
            .where(table.c.id == item["item_id"])
            .values(**{column: item["previous_owner_id"]})
        )
        if table is t.party:
            changed.add(item["item_id"])
    uow.connection.execute(
        update(t.party).where(t.party.c.id == source["id"]).values(merged_into_id=None)
    )
    for party_id in changed:
        bump(uow, party_id)
    uow.connection.execute(
        update(t.merge_record)
        .where(t.merge_record.c.id == record["id"])
        .values(
            unmerged_at=datetime.now(UTC),
            unmerged_by_kind=ctx.actor.kind.value,
            unmerged_by_id=ctx.actor.id,
            unmerge_note=data.note,
        )
    )
    publish(
        ctx,
        uow,
        NewEvent(
            type="relationships.party.unmerged",
            schema_version=1,
            subject_ref=ref("party", target["id"]),
            subject_revision=target["revision"] + 1,
            data={
                "survivor_ref": ref("party", target["id"]),
                "merged_ref": ref("party", source["id"]),
                "merge_record_ref": data.merge_record_ref.format(),
            },
        ),
        now=datetime.now(UTC),
        consumers=uow.consumers,
    )
    return c.MergeOutput(merge_record_ref=data.merge_record_ref.format())


def list_review(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.ListReviewInput
) -> c.ItemsOutput:
    rows = uow.connection.execute(
        select(t.review_candidate)
        .where(t.review_candidate.c.state == data.state)
        .order_by(t.review_candidate.c.created_at, t.review_candidate.c.id)
        .limit(data.limit)
    ).mappings()

    def head(identifier: UUID) -> dict[str, Any] | None:
        try:
            party = get_row(uow, identifier)
        except OperationRefused:
            return None
        return {
            "ref": ref("party", identifier),
            "display_name": party["display_name"],
            "kind": party["kind"],
            "revision": party["revision"],
        }

    return c.ItemsOutput(
        items=[
            {
                **dict(row),
                "candidate_ref": ref("review_candidate", row["id"]),
                "party_ref": ref("party", row["party_id"]),
                "candidate_party_ref": ref("party", row["candidate_party_id"]),
                "party": head(row["party_id"]),
                "candidate_party": head(row["candidate_party_id"]),
            }
            for row in rows
        ]
    )


def resolve_review(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.ReviewInput
) -> c.ItemsOutput:
    lock_workspace_lifecycle(uow.connection)
    row = (
        uow.connection.execute(
            select(t.review_candidate).where(
                t.review_candidate.c.id == data.candidate_ref.id
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        refuse("not_found")
    assert row is not None
    if row["state"] != "open":
        refuse("candidate_not_open")
    values: dict[str, Any] = {
        "state": "rejected",
        "resolved_at": datetime.now(UTC),
        "resolved_by_kind": ctx.actor.kind.value,
        "resolved_by_id": ctx.actor.id,
    }
    if data.decision == "accept":
        left = get_row(uow, row["party_id"], live=True)
        right = get_row(uow, row["candidate_party_id"], live=True)
        if row["kind"] == "same_party":
            target, source = left, right
            if data.survivor:
                if data.survivor.id not in (left["id"], right["id"]):
                    refuse("input_invalid")
                if data.survivor.id == right["id"]:
                    target, source = right, left
            merged = merge(
                ctx,
                uow,
                c.MergeInput.model_validate(
                    {
                        "source_ref": ref("party", source["id"]),
                        "target_ref": ref("party", target["id"]),
                        "source_revision": source["revision"],
                        "target_revision": target["revision"],
                        "evidence": data.note,
                        "evidence_refs": [row["evidence_ref"]]
                        if row["evidence_ref"]
                        else [],
                    }
                ),
            )
            values["merge_record_id"] = UUID(merged.merge_record_ref.split(":")[1])
        else:
            if data.survivor:
                refuse("input_invalid")
            values["affiliation_id"] = add_affiliation(
                uow,
                left["id"],
                right["id"],
                valid_from=row["occurred_at"].date(),
                evidence=row["evidence_ref"],
            )
            bump(uow, left["id"])
        values["state"] = "accepted"
    uow.connection.execute(
        update(t.review_candidate)
        .where(t.review_candidate.c.id == row["id"])
        .values(**values)
    )
    return c.ItemsOutput(
        items=[{"candidate_ref": data.candidate_ref.format(), "state": values["state"]}]
    )
