"""The job queue's card projection: one bounded read instead of a get per card.

Each item carries what a queue card shows and nothing else: the opportunity's own
columns, its stage, the Job search extension fields and the start of the original
message, the latest assessment's ratings and first line, the pending follow-up,
whether notes or drafts exist, the source transport, the linked party's display
name, and the closed stage a dismiss would move it to. Contact points, notes,
draft bodies and full messages stay behind ``leads.opportunity.get``.
"""

from typing import Any
from uuid import UUID

from pydantic import Field
from rheo_contracts import (
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import and_, select

from rheo_leads import pipeline_common as h
from rheo_leads import pipeline_contracts as c
from rheo_leads.references import ref
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t

EXTENSION = "ext.jobsearch."
# The extension fields a card or detail header reads, by their short name.
CARD_FIELDS = (
    "platform",
    "posting_url",
    "job_type",
    "budget_min",
    "budget_max",
    "proposal_count",
    "proposal_count_exact",
    "posted_on",
    "client_payment_verified",
    "client_spend",
    "client_rating",
    "client_reviews",
)
PREVIEW_CHARS = 240
SUMMARY_CHARS = 280
# A dismiss closes the opportunity as not pursued; a lost outcome is the fallback
# for stages that cannot be disqualified any more.
DISMISS_OUTCOMES = ("disqualified", "lost")


class QueueInput(c.Strict):
    """Without ``ref``: newest first, open stages unless a stage or closed ones are
    asked for. With ``ref``: that one opportunity, whatever its stage."""

    ref: c.OpportunityRef | None = None
    pipeline_id: UUID | None = None
    jobsearch_only: bool = False
    stage_id: str | None = Field(default=None, min_length=1, max_length=128)
    include_closed: bool = False
    limit: int = Field(default=100, ge=1, le=200)


def _preview(text: str | None, limit: int) -> str | None:
    if not text or not text.strip():
        return None
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _first_line(text: str | None) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        if line.strip():
            return _preview(line, SUMMARY_CHARS)
    return None


def _party(
    ctx: WorkspaceContext, uow: UnitOfWork, reference: str
) -> dict[str, str] | None:
    """The party's kind and display name, or None when Relationships refuses."""
    try:
        party = h.party_read(ctx, uow, reference)
    except OperationRefused:
        return None
    return {"kind": str(party.kind), "name": str(party.display_name)}


def queue(ctx: WorkspaceContext, uow: UnitOfWork, m: QueueInput) -> c.ItemsOutput:
    h.require(ctx)
    o, s = p.opportunity, p.preset_stage
    q = (
        select(
            o,
            s.c.label.label("stage_label"),
            s.c.kind.label("stage_kind"),
            t.observation.c.transport.label("source"),
        )
        .join(
            s,
            and_(
                s.c.preset_id == o.c.preset_id,
                s.c.version == o.c.preset_version,
                s.c.stage_id == o.c.stage_id,
            ),
        )
        .outerjoin(t.observation, t.observation.c.id == o.c.source_observation_id)
    )
    if m.ref is not None:
        q = q.where(o.c.id == m.ref.id)
    else:
        if m.pipeline_id is not None:
            q = q.where(o.c.pipeline_id == m.pipeline_id)
        if m.jobsearch_only:
            q = q.where(o.c.preset_id == h.JOBSEARCH_PRESET_ID)
        if m.stage_id is not None:
            q = q.where(o.c.stage_id == m.stage_id)
        elif not m.include_closed:
            q = q.where(s.c.kind == "open")
    records = [
        dict(r)
        for r in uow.connection.execute(
            q.order_by(o.c.created_at.desc(), o.c.id.desc()).limit(m.limit)
        ).mappings()
    ]
    if m.ref is not None and not records:
        raise OperationRefused("not_found", "opportunity not found")
    ids = [r["id"] for r in records]
    fields: dict[UUID, dict[str, str]] = {i: {} for i in ids}
    targets = [EXTENSION + name for name in CARD_FIELDS] + ["message"]
    state = p.opportunity_field_state
    for r in uow.connection.execute(
        select(state.c.opportunity_id, state.c.target, state.c.value_text).where(
            state.c.opportunity_id.in_(ids),
            state.c.target.in_(targets),
            state.c.value_kind == "value",
        )
    ).mappings():
        if r["value_text"] is not None:
            fields[r["opportunity_id"]][r["target"]] = r["value_text"]
    qual = p.qualification
    # Newest first per opportunity; the first row seen for each one wins.
    assessments: dict[UUID, dict[str, Any]] = {}
    for r in uow.connection.execute(
        select(qual)
        .where(qual.c.opportunity_id.in_(ids))
        .order_by(
            qual.c.opportunity_id,
            qual.c.input_revision.desc(),
            qual.c.created_at.desc(),
            qual.c.id.desc(),
        )
    ).mappings():
        assessments.setdefault(r["opportunity_id"], dict(r))
    reminders = p.followup_reminder
    followups = {
        r["opportunity_id"]: {"due_on": r["due_on"], "action": r["action"]}
        for r in uow.connection.execute(
            select(
                reminders.c.opportunity_id, reminders.c.due_on, reminders.c.action
            ).where(reminders.c.opportunity_id.in_(ids), reminders.c.state == "pending")
        ).mappings()
    }
    drafts: set[UUID] = set(
        uow.connection.execute(
            select(p.draft.c.opportunity_id)
            .where(p.draft.c.opportunity_id.in_(ids))
            .distinct()
        ).scalars()
    )
    notes: set[UUID] = set(
        uow.connection.execute(
            select(p.opportunity_note.c.opportunity_id)
            .where(p.opportunity_note.c.opportunity_id.in_(ids))
            .distinct()
        ).scalars()
    )
    party = p.opportunity_party
    parties: dict[UUID, str] = {}
    for r in uow.connection.execute(
        select(party.c.opportunity_id, party.c.party_ref)
        .where(party.c.opportunity_id.in_(ids), party.c.removed_at.is_(None))
        .order_by(
            party.c.opportunity_id,
            (party.c.role != "contact"),
            party.c.added_at,
            party.c.party_ref,
        )
    ).mappings():
        parties.setdefault(r["opportunity_id"], r["party_ref"])
    dismiss = _dismiss_targets(uow, records)
    items = []
    for record in records:
        identifier = record["id"]
        found = fields[identifier]
        assessment = assessments.get(identifier)
        reference = parties.get(identifier)
        items.append(
            {
                "ref": ref("opportunity", identifier),
                "id": identifier,
                "title": record["title"],
                "pipeline_id": record["pipeline_id"],
                "preset_id": record["preset_id"],
                "stage_id": record["stage_id"],
                "stage_label": record["stage_label"],
                "stage_kind": record["stage_kind"],
                "revision": record["revision"],
                "created_at": record["created_at"],
                "updated_at": record["updated_at"],
                "source": record["source"],
                "fields": {
                    target.removeprefix(EXTENSION): value
                    for target, value in found.items()
                    if target.startswith(EXTENSION)
                },
                "preview": _preview(found.get("message"), PREVIEW_CHARS),
                "assessment": None
                if assessment is None
                else {
                    "fit": assessment["fit"],
                    "intent": assessment["intent"],
                    "urgency": assessment["urgency"],
                    "evidence_completeness": assessment["evidence_completeness"],
                    "uncertainty": assessment["uncertainty"],
                    "summary": _first_line(assessment["explanation"]),
                    "created_at": assessment["created_at"],
                },
                "followup": followups.get(identifier),
                "has_draft": identifier in drafts,
                "has_notes": identifier in notes,
                "party": None if reference is None else _party(ctx, uow, reference),
                "dismiss": dismiss.get(
                    (record["preset_id"], record["preset_version"], record["stage_id"])
                ),
            }
        )
    return c.ItemsOutput(items=items)


def _dismiss_targets(
    uow: UnitOfWork, records: list[dict[str, Any]]
) -> dict[tuple[UUID, int, str], dict[str, str]]:
    """Per pinned (preset, version, stage): the closed stage a dismiss moves to,
    a disqualifying one before a lost one, or nothing when neither is allowed."""
    keys = {(r["preset_id"], r["preset_version"]) for r in records}
    if not keys:
        return {}
    tr, s = p.preset_transition, p.preset_stage
    rows = uow.connection.execute(
        select(
            tr.c.preset_id,
            tr.c.version,
            tr.c.from_stage_id,
            s.c.stage_id,
            s.c.label,
            s.c.outcome,
        )
        .join(
            s,
            and_(
                s.c.preset_id == tr.c.preset_id,
                s.c.version == tr.c.version,
                s.c.stage_id == tr.c.to_stage_id,
            ),
        )
        .where(
            s.c.kind == "terminal",
            s.c.outcome.in_(DISMISS_OUTCOMES),
            tr.c.preset_id.in_({k[0] for k in keys}),
        )
    ).mappings()
    best: dict[tuple[UUID, int, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["preset_id"], r["version"], r["from_stage_id"])
        if (r["preset_id"], r["version"]) not in keys:
            continue
        current = best.get(key)
        rank = DISMISS_OUTCOMES.index(r["outcome"])
        if (
            current is None
            or rank < current["rank"]
            or (rank == current["rank"] and r["stage_id"] < current["stage_id"])
        ):
            best[key] = {"rank": rank, "stage_id": r["stage_id"], "label": r["label"]}
    return {
        key: {"stage_id": v["stage_id"], "label": v["label"]} for key, v in best.items()
    }


OPERATIONS = (
    (
        OperationDeclaration(
            name="leads.ui.queue",
            safety_class=SafetyClass.READ,
            idempotency=Idempotency.NONE,
            roles=frozenset({Role.OWNER, Role.MEMBER}),
            input_model=QueueInput,
            output=c.ItemsOutput,
        ),
        queue,
    ),
)
