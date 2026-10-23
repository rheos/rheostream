"""The job queue's card projection: one bounded read instead of a get per card.

Each item carries what a queue card shows and nothing else: the opportunity's own
columns, its stage, the Job search extension fields and the start of the original
message, the latest assessment's ratings and explanation, the pending follow-up,
whether notes or drafts exist, the source transport, the linked party's display
name, the stages it may move to next, and the closed stage a dismiss would move
it to. Sorting by score (then fit) and hiding crowded postings happen here, before
the limit,
so the first page is the right one. Contact points, notes,
draft bodies and full messages stay behind ``leads.opportunity.get``.

With ``held_only`` it is the review queue: Job search jobs the client filters hold
back and nobody promoted, each with a ``hold`` list of the clauses that held it
(:mod:`rheo_leads.scoring.review`).
"""

from typing import Any, Literal
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
from sqlalchemy import Integer, and_, case, cast, exists, select

from rheo_leads import pipeline_common as h
from rheo_leads import pipeline_contracts as c
from rheo_leads.references import ref
from rheo_leads.scoring import review
from rheo_leads.short_ids import with_short_ids
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import proposals
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
    "client_hire_rate",
    "invited",
)
PREVIEW_CHARS = 240
SUMMARY_CHARS = 800
# The explanation's lines, for the job detail's list of reasons.
MAX_REASONS = 12
REASON_CHARS = 600
# Above this many proposals a posting is crowded (the web client's MAX_PROPOSALS).
MAX_PROPOSALS = 50
# Fit ratings by sort weight, higher first; an unassessed opportunity sorts last.
FIT_RANK = {"high": 4, "medium": 3, "needs_information": 2, "low": 1}
# A dismiss closes the opportunity as not pursued; a lost outcome is the fallback
# for stages that cannot be disqualified any more.
DISMISS_OUTCOMES = ("disqualified", "lost")


class QueueInput(c.Strict):
    """Without ``ref``: open stages unless a stage or closed ones are asked for,
    by latest score, then fit, then newest (``sort="score"``) or newest only,
    optionally without postings above the proposal cap, or only the Job search jobs
    the client filters hold (``held_only``, the review queue). With ``ref``: that
    one opportunity, whatever its stage or count."""

    ref: c.OpportunityRef | None = None
    pipeline_id: UUID | None = None
    jobsearch_only: bool = False
    stage_id: str | None = Field(default=None, min_length=1, max_length=128)
    include_closed: bool = False
    sort: Literal["score", "newest"] = "newest"
    hide_full: bool = False
    held_only: bool = False
    limit: int = Field(default=100, ge=1, le=200)


# The provider's marker tag feed descriptions can arrive wrapped in (#392). A
# preview drops exactly that outer wrapper before it is cut to length, so the
# tag never spends the preview's characters; the stored text is unchanged.
_WRAPPER_OPEN = "<untrusted_participant_content>"
_WRAPPER_CLOSE = "</untrusted_participant_content>"


def _unwrapped(text: str) -> str:
    text = text.strip()
    if text.startswith(_WRAPPER_OPEN):
        text = text[len(_WRAPPER_OPEN) :].lstrip()
    if text.endswith(_WRAPPER_CLOSE):
        text = text[: -len(_WRAPPER_CLOSE)].rstrip()
    return text


def _preview(text: str | None, limit: int) -> str | None:
    if not text or not text.strip():
        return None
    text = _unwrapped(text)
    if not text:
        return None
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _summary(text: str | None) -> str | None:
    """The whole explanation as one paragraph, capped; the card clamps it."""
    if not text:
        return None
    return _preview(
        " ".join(line.strip() for line in text.split("\n") if line.strip()),
        SUMMARY_CHARS,
    )


def _reasons(text: str | None) -> list[str]:
    """The explanation's non-empty lines, each capped, at most ``MAX_REASONS``."""
    lines = [line.strip() for line in (text or "").split("\n") if line.strip()]
    return [
        line if len(line) <= REASON_CHARS else line[:REASON_CHARS].rstrip() + "…"
        for line in lines[:MAX_REASONS]
    ]


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
        if m.hide_full:
            q = q.where(~_over_cap(o.c.id))
        if m.held_only:
            # Only open jobs are held: a closed one has nothing left to promote.
            q = q.where(
                s.c.kind == "open",
                o.c.preset_id == h.JOBSEARCH_PRESET_ID,
                review.is_held(o.c.id),
                review.not_promoted(o.c.id),
            )
    order = [o.c.created_at.desc(), o.c.id.desc()]
    if m.sort == "score":
        order[:0] = [
            _latest_score(o.c.id).desc().nulls_last(),
            _fit_rank(o.c.id).desc(),
        ]
    records = [
        dict(r)
        for r in uow.connection.execute(q.order_by(*order).limit(m.limit)).mappings()
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
    # A drafted proposal counts as a draft on the card too.
    drafts |= set(
        uow.connection.execute(
            select(proposals.proposal_section.c.opportunity_id)
            .where(proposals.proposal_section.c.opportunity_id.in_(ids))
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
    moves = _transitions(uow, records)
    holds = (
        review.hold_reasons(
            uow,
            records,
            {i: _reasons(a["explanation"]) for i, a in assessments.items()},
        )
        if m.held_only
        else {}
    )

    def key(record: dict[str, Any]) -> tuple[UUID, int, str]:
        return (record["preset_id"], record["preset_version"], record["stage_id"])

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
                # The whole derived posting, only for the one job the detail reads.
                **({"description": found.get("message")} if m.ref is not None else {}),
                "assessment": None
                if assessment is None
                else {
                    "fit": assessment["fit"],
                    "score": assessment["score"],
                    "intent": assessment["intent"],
                    "urgency": assessment["urgency"],
                    "evidence_completeness": assessment["evidence_completeness"],
                    "uncertainty": assessment["uncertainty"],
                    "summary": _summary(assessment["explanation"]),
                    "reasons": _reasons(assessment["explanation"]),
                    "created_at": assessment["created_at"],
                },
                "followup": followups.get(identifier),
                "has_draft": identifier in drafts,
                "has_notes": identifier in notes,
                "party": None if reference is None else _party(ctx, uow, reference),
                "transitions": moves.get(key(record), {}).get("next", []),
                "dismiss": moves.get(key(record), {}).get("dismiss"),
                **({"hold": holds.get(identifier, [])} if m.held_only else {}),
            }
        )
    with_short_ids(uow, items)
    return c.ItemsOutput(items=items)


def _latest(column: Any, opportunity_id: Any) -> Any:
    """``column`` of the opportunity's latest assessment, as a scalar subquery."""
    qual = p.qualification
    return (
        select(column)
        .where(qual.c.opportunity_id == opportunity_id)
        .order_by(
            qual.c.input_revision.desc(), qual.c.created_at.desc(), qual.c.id.desc()
        )
        .limit(1)
        .scalar_subquery()
    )


def _latest_score(opportunity_id: Any) -> Any:
    """The latest assessment's 1-100 score; null when it has none."""
    return _latest(p.qualification.c.score, opportunity_id)


def _fit_rank(opportunity_id: Any) -> Any:
    """The latest assessment's fit as a sort weight, 0 when unassessed."""
    latest = _latest(p.qualification.c.fit, opportunity_id)
    return case(*((latest == fit, rank) for fit, rank in FIT_RANK.items()), else_=0)


def _over_cap(opportunity_id: Any) -> Any:
    """A whole-number proposal count above the cap, band or exact."""
    state = p.opportunity_field_state
    count = case(
        (
            state.c.value_text.regexp_match("^[0-9]{1,9}$"),
            cast(state.c.value_text, Integer),
        ),
        else_=0,
    )
    return exists().where(
        state.c.opportunity_id == opportunity_id,
        state.c.target == EXTENSION + "proposal_count",
        state.c.value_kind == "value",
        count > MAX_PROPOSALS,
    )


def _transitions(
    uow: UnitOfWork, records: list[dict[str, Any]]
) -> dict[tuple[UUID, int, str], dict[str, Any]]:
    """Per pinned (preset, version, stage): the stages it may move to next, in
    stage order, and the closed stage a dismiss moves to (a disqualifying one before
    a lost one, or none when neither is allowed)."""
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
            s.c.kind,
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
        .where(tr.c.preset_id.in_({k[0] for k in keys}))
        .order_by(s.c.ordinal, s.c.stage_id)
    ).mappings()
    found: dict[tuple[UUID, int, str], dict[str, Any]] = {}
    for r in rows:
        if (r["preset_id"], r["version"]) not in keys:
            continue
        entry = found.setdefault(
            (r["preset_id"], r["version"], r["from_stage_id"]),
            {"next": [], "dismiss": None, "rank": len(DISMISS_OUTCOMES)},
        )
        entry["next"].append(
            {"stage_id": r["stage_id"], "label": r["label"], "kind": r["kind"]}
        )
        if r["kind"] == "terminal" and r["outcome"] in DISMISS_OUTCOMES:
            rank = DISMISS_OUTCOMES.index(r["outcome"])
            if rank < entry["rank"]:
                entry["rank"] = rank
                entry["dismiss"] = {"stage_id": r["stage_id"], "label": r["label"]}
    return found


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
