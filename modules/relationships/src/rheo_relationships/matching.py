"""Evidence-bound resolution and conservative, per-hint review candidates."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from rheo_contracts import RecordRef, WorkspaceContext
from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
from rheo_core.refs import uuid7
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from rheo_core.storage.backend import UnitOfWork
from sqlalchemy import func, select
from sqlalchemy.engine import ScalarResult

from rheo_relationships import contracts as c
from rheo_relationships.normalization import normalize
from rheo_relationships.references import ref
from rheo_relationships.service import (
    add_affiliation,
    add_contact,
    bump,
    canonical,
    create_row,
    refuse,
)
from rheo_relationships.storage import tables as t


def identity(uow: UnitOfWork, namespace: str, kind: str, value: str) -> UUID | None:
    verification = "authenticated" if kind == "external_subject" else "source_verified"
    owner = uow.connection.execute(
        select(t.contact_point.c.party_id).where(
            t.contact_point.c.namespace == namespace,
            t.contact_point.c.kind == kind,
            t.contact_point.c.verification == verification,
            t.contact_point.c.normalized_value == normalize(kind, value),
            t.contact_point.c.retired_at.is_(None),
        )
    ).scalar_one_or_none()
    return canonical(uow, owner)["id"] if owner else None


def candidate(
    uow: UnitOfWork,
    party: UUID,
    other: UUID,
    hint: str,
    data: c.ResolveInput,
    *,
    contact_id: UUID | None = None,
    score: Any = None,
) -> str | None:
    if party == other:
        return None
    affiliation = hint in ("domain", "organization_name")
    left, right = (party, other) if affiliation else tuple(sorted((party, other)))
    existing = (
        uow.connection.execute(
            select(t.review_candidate).where(
                t.review_candidate.c.party_id == left,
                t.review_candidate.c.candidate_party_id == right,
                t.review_candidate.c.hint_kind == hint,
                t.review_candidate.c.state.in_(("open", "rejected")),
            )
        )
        .mappings()
        .all()
    )
    if any(row["state"] == "rejected" for row in existing):
        return None
    if existing:
        return ref("review_candidate", existing[0]["id"])
    identifier = uuid7()
    uow.connection.execute(
        t.review_candidate.insert().values(
            id=identifier,
            kind="affiliation" if affiliation else "same_party",
            party_id=left,
            candidate_party_id=right,
            hint_kind=hint,
            matched_contact_point_id=contact_id,
            score=score,
            evidence_ref=data.evidence_ref,
            occurred_at=data.occurred_at,
            state="open",
            created_at=datetime.now(UTC),
        )
    )
    return ref("review_candidate", identifier)


def candidates(
    ctx: WorkspaceContext, uow: UnitOfWork, party: UUID, data: c.ResolveInput
) -> list[str]:
    settings = resolve(
        workspace_id=ctx.workspace_id,
        source=TransactionBoundOverrideSource(uow, workspace_id=ctx.workspace_id),
    )
    cap = settings.get_int("relationships.match.max_candidates")
    result: set[str] = set()

    def propose(
        other: UUID, hint: str, contact_id: UUID | None = None, score: Any = None
    ) -> None:
        value = candidate(
            uow, party, other, hint, data, contact_id=contact_id, score=score
        )
        if value:
            result.add(value)

    contact_query = (
        select(t.contact_point)
        .join(t.party, t.party.c.id == t.contact_point.c.party_id)
        .where(
            t.contact_point.c.retired_at.is_(None),
            t.party.c.merged_into_id.is_(None),
            t.party.c.id != party,
            t.party.c.kind == "person",
        )
    )
    for kind, value in [
        ("email", data.verified_email or data.hints.email),
        ("phone", data.hints.phone),
    ]:
        if not value:
            continue
        rows = uow.connection.execute(
            contact_query.where(
                t.contact_point.c.kind == kind,
                t.contact_point.c.normalized_value == normalize(kind, value),
            ).order_by(t.contact_point.c.id)
        ).mappings()
        for row in rows:
            hint = (
                "phone"
                if kind == "phone"
                else (
                    "email_verified_other_source"
                    if row["verification"] == "source_verified"
                    and row["namespace"] != data.source_namespace
                    else "email_unverified"
                )
            )
            propose(row["party_id"], hint, row["id"])
    if data.hints.name:
        score = func.similarity(
            func.lower(t.party.c.display_name), normalize("name", data.hints.name)
        )
        rows = uow.connection.execute(
            select(t.party.c.id, score.label("score"))
            .where(
                t.party.c.kind == "person",
                t.party.c.merged_into_id.is_(None),
                t.party.c.id != party,
                score
                >= settings.get_int("relationships.match.name_similarity_percent")
                / 100,
            )
            .order_by(score.desc(), t.party.c.id)
            .limit(cap)
        ).mappings()
        for row in rows:
            propose(row["id"], "name", score=row["score"])
    organizations: list[tuple[UUID, str, UUID | None]] = []
    if data.hints.organization_domain:
        rows = uow.connection.execute(
            select(t.contact_point)
            .join(t.party, t.party.c.id == t.contact_point.c.party_id)
            .where(
                t.party.c.kind == "organization",
                t.party.c.merged_into_id.is_(None),
                t.contact_point.c.kind == "url",
                t.contact_point.c.retired_at.is_(None),
                t.contact_point.c.normalized_value
                == normalize("url", data.hints.organization_domain),
            )
        ).mappings()
        organizations.extend((row["party_id"], "domain", row["id"]) for row in rows)
    if data.hints.organization_name:
        organization_ids: ScalarResult[UUID] = uow.connection.execute(
            select(t.party.c.id).where(
                t.party.c.kind == "organization",
                t.party.c.merged_into_id.is_(None),
                func.lower(
                    func.regexp_replace(
                        func.trim(t.party.c.display_name), r"\s+", " ", "g"
                    )
                )
                == normalize("name", data.hints.organization_name),
            )
        ).scalars()
        organizations.extend(
            (identifier, "organization_name", None) for identifier in organization_ids
        )
    for organization, hint, contact_id in organizations:
        # An already-established affiliation is not another review task.
        linked = uow.connection.execute(
            select(t.affiliation.c.id)
            .where(
                t.affiliation.c.person_id == party,
                t.affiliation.c.organization_id == organization,
                t.affiliation.c.valid_to.is_(None),
            )
            .limit(1)
        ).scalar_one_or_none()
        if linked is None:
            propose(organization, hint, contact_id)
    if not organizations and (
        data.hints.organization_name or data.hints.organization_domain
    ):
        organization = create_row(
            uow,
            "organization",
            data.hints.organization_name or data.hints.organization_domain or "unnamed",
        )
        if data.hints.organization_domain:
            add_contact(
                uow,
                organization,
                "url",
                data.hints.organization_domain,
                provenance="observation",
                evidence=data.evidence_ref,
            )
        add_affiliation(
            uow,
            party,
            organization,
            valid_from=data.occurred_at.date(),
            evidence=data.evidence_ref,
        )
        bump(uow, party)
    return sorted(result)


def resolve_or_create(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.ResolveInput
) -> c.ResolveOutput:
    # Evidence supplies provenance, never authority.
    try:
        RecordRef.parse(data.evidence_ref)
    except ValueError:
        refuse("input_invalid")
    lock_workspace_lifecycle(uow.connection)
    subject = (
        identity(
            uow, data.source_namespace, "external_subject", data.external_subject_id
        )
        if data.external_subject_id
        else None
    )
    email = (
        identity(uow, data.source_namespace, "email", data.verified_email)
        if data.verified_email
        else None
    )
    if subject and email and subject != email:
        refuse("identity_conflict")
    party = subject or email
    outcome = "linked_subject" if subject else "linked_email" if email else "created"
    if party is None:
        party = create_row(
            uow,
            "person",
            data.hints.name
            or data.hints.email
            or data.verified_email
            or data.hints.phone
            or "unnamed",
        )
    elif canonical(uow, party)["kind"] != "person":
        refuse("party_kind_mismatch")
    changed = False
    for kind, value, verification in [
        ("external_subject", data.external_subject_id, "authenticated"),
        ("email", data.verified_email, "source_verified"),
        ("email", data.hints.email, "unverified"),
        ("phone", data.hints.phone, "unverified"),
    ]:
        if not value:
            continue
        namespace = data.source_namespace if verification != "unverified" else None
        existing = uow.connection.execute(
            select(t.contact_point.c.id)
            .where(
                t.contact_point.c.party_id == party,
                t.contact_point.c.kind == kind,
                t.contact_point.c.normalized_value == normalize(kind, value),
                t.contact_point.c.verification == verification,
                t.contact_point.c.namespace.is_not_distinct_from(namespace),
                t.contact_point.c.retired_at.is_(None),
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is None:
            add_contact(
                uow,
                party,
                kind,
                value,
                verification=verification,
                namespace=namespace,
                provenance="observation",
                evidence=data.evidence_ref,
            )
            changed = True
    if changed:
        bump(uow, party)
    return c.ResolveOutput(
        party_ref=ref("party", party),
        outcome=outcome,
        review_candidate_refs=candidates(ctx, uow, party, data),
    )
