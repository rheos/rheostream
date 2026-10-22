"""The proposal operations: request a draft, read it, edit it, delete it, and the
answer library.

Every operation here prepares text and none sends anything: an application is the
owner's to submit, on the platform, by hand.

- ``leads.jobproposal.request`` queues a draft of a Job search opportunity (optionally
  replacing its screening questions) and a drafting sweep. While one is queued or
  drafting it refuses ``draft_in_progress``. When a section was edited by hand or taken
  from the library since the last draft, it refuses ``draft_edited`` unless ``force``:
  a regenerate replaces every section.
- ``leads.jobproposal.get`` answers the state, progress, questions, sections, findings
  and the whole-draft text; a job with no proposal answers ``idle``.
- ``leads.proposalsection.set`` edits one section's body;
  ``leads.jobproposal.update`` replaces the whole draft from its text form (a text with
  no ``---`` divider replaces only the cover letter); ``leads.jobproposal.delete``
  removes the draft and keeps the questions, and also stops a queued or running draft.
- ``leads.answerlibrary.save`` keeps an answer under its normalised question,
  ``leads.answerlibrary.match`` finds one, and ``leads.proposalsection.use_library``
  puts the matching answer into a section.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from pydantic import Field
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    ToolDeclaration,
    WorkspaceContext,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rheo_leads import pipeline_common as h
from rheo_leads.pipeline_contracts import RefInput, Strict
from rheo_leads.proposals.job import queue_sweep
from rheo_leads.proposals.pricing_operations import PricingView, pricing_view
from rheo_leads.proposals.pricing_operations import operations as pricing_operations
from rheo_leads.proposals.profile import read_proposal_profile
from rheo_leads.proposals.render import (
    UNLABELLED,
    Answer,
    Draft,
    parse_draft,
    render_draft,
)
from rheo_leads.references import ref
from rheo_leads.storage import proposals as s

MEMBERS: Final = frozenset({Role.OWNER, Role.MEMBER})
READERS: Final = MEMBERS | {Role.SERVICE}
MAX_QUESTIONS_CHARS: Final = 8_000
MAX_SECTION_CHARS: Final = 16_000
MAX_DRAFT_CHARS: Final = 60_000
BUSY: Final = ("queued", "drafting")


def question_key(text: str) -> str:
    """Lowercased, whitespace collapsed, surrounding punctuation stripped."""
    text = re.sub(r"\s+", " ", text.lower()).strip()
    return text.strip(".,;:!?\"'()[]")


class RequestInput(RefInput):
    # ``None`` keeps the stored questions; a string (empty to clear) replaces them.
    questions: str | None = Field(default=None, max_length=MAX_QUESTIONS_CHARS)
    # ``None`` keeps the stored choice. True: the form has a field per question, so
    # each gets its own answer block; false, the cover letter answers them all.
    separate_answers: bool | None = None
    force: bool = False


class SectionInput(RefInput):
    section_id: UUID


class SectionSetInput(SectionInput):
    body: str = Field(min_length=1, max_length=MAX_SECTION_CHARS)


class DraftUpdateInput(RefInput):
    draft: str = Field(min_length=1, max_length=MAX_DRAFT_CHARS)


class SectionView(Strict):
    id: UUID
    position: int
    kind: str
    question: str | None
    body: str
    library_answer_id: UUID | None
    # A library answer for this section's question, when one exists.
    library_match_id: UUID | None
    edited: bool
    updated_at: datetime


class ProposalView(Strict):
    opportunity_ref: str
    state: str
    stage: str | None = None
    step: int | None = None
    total: int | None = None
    failure_kind: str | None = None
    attempts: int = 0
    questions: str | None = None
    separate_answers: bool = False
    requested_at: datetime | None = None
    generated_at: datetime | None = None
    findings: dict[str, Any] | None = None
    sections: list[SectionView] = Field(default_factory=list)
    # The whole-draft text form, for a copy of everything at once.
    draft: str | None = None
    edited: bool = False
    profile_version: int | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    pricing: PricingView | None = None


class LibraryQuestion(Strict):
    question: str = Field(min_length=1, max_length=2_000)


class LibrarySave(LibraryQuestion):
    answer: str = Field(min_length=1, max_length=MAX_SECTION_CHARS)


class LibraryEntry(Strict):
    id: UUID
    question: str
    answer: str
    updated_at: datetime


class LibraryMatch(Strict):
    entry: LibraryEntry | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _job(uow: UnitOfWork, m: RefInput, *, writing: bool) -> dict[str, Any]:
    record = h.opportunity(uow, m, writing=writing)
    if record["preset_id"] != h.JOBSEARCH_PRESET_ID:
        raise OperationRefused(
            "not_job_search", "proposals are for Job search opportunities"
        )
    return record


def _row(
    uow: UnitOfWork, identifier: UUID, *, lock: bool = False
) -> dict[str, Any] | None:
    statement = select(s.job_proposal).where(
        s.job_proposal.c.opportunity_id == identifier
    )
    if lock:
        statement = statement.with_for_update()
    found = uow.connection.execute(statement).mappings().one_or_none()
    return dict(found) if found is not None else None


def _sections(uow: UnitOfWork, identifier: UUID) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in uow.connection.execute(
            select(s.proposal_section)
            .where(s.proposal_section.c.opportunity_id == identifier)
            .order_by(s.proposal_section.c.position)
        ).mappings()
    ]


def _edited(row: Mapping[str, Any] | None, section: Mapping[str, Any]) -> bool:
    generated = row["generated_at"] if row else None
    return section["library_answer_id"] is not None or (
        generated is not None and section["updated_at"] > generated
    )


def _library_matches(uow: UnitOfWork, questions: list[str]) -> dict[str, UUID]:
    keys = {question_key(q) for q in questions if q}
    if not keys:
        return {}
    lib = s.answer_library
    return {
        r.question_key: r.id
        for r in uow.connection.execute(
            select(lib.c.id, lib.c.question_key).where(lib.c.question_key.in_(keys))
        )
    }


def _draft_of(sections: list[dict[str, Any]]) -> Draft:
    letter = next((x["body"] for x in sections if x["kind"] == "cover_letter"), "")
    return Draft(
        letter=letter,
        answers=tuple(
            Answer(question=x["question"], answer=x["body"])
            for x in sections
            if x["kind"] == "answer"
        ),
    )


def view(uow: UnitOfWork, identifier: UUID) -> ProposalView:
    row = _row(uow, identifier)
    reference = ref("opportunity", identifier)
    if row is None:
        return ProposalView(
            opportunity_ref=reference,
            state="idle",
            pricing=pricing_view(uow, identifier, None),
        )
    sections = _sections(uow, identifier)
    matches = _library_matches(uow, [x["question"] for x in sections if x["question"]])
    views = [
        SectionView(
            id=x["id"],
            position=x["position"],
            kind=x["kind"],
            question=x["question"],
            body=x["body"],
            library_answer_id=x["library_answer_id"],
            library_match_id=matches.get(question_key(x["question"]))
            if x["question"]
            else None,
            edited=_edited(row, x),
            updated_at=x["updated_at"],
        )
        for x in sections
    ]
    return ProposalView(
        opportunity_ref=reference,
        state=row["state"],
        stage=row["stage"],
        step=row["step"],
        total=row["total"],
        failure_kind=row["failure_kind"],
        attempts=row["attempts"],
        questions=row["questions"],
        separate_answers=row["separate_answers"],
        requested_at=row["requested_at"],
        generated_at=row["generated_at"],
        findings=row["findings"],
        sections=views,
        draft=render_draft(_draft_of(sections)) if sections else None,
        edited=any(v.edited for v in views),
        profile_version=row["profile_version"],
        model_id=row["model_id"],
        prompt_version=row["prompt_version"],
        pricing=pricing_view(uow, identifier, row["findings"]),
    )


def request(ctx: WorkspaceContext, uow: UnitOfWork, m: RequestInput) -> ProposalView:
    h.require(ctx)
    if not h.jobsearch_enabled(ctx, uow):
        raise OperationRefused("capability_disabled", "job search is off")
    record = _job(uow, m, writing=True)
    if read_proposal_profile(uow) is None:
        raise OperationRefused("profile_required", "set a proposal profile first")
    identifier = record["id"]
    row = _row(uow, identifier, lock=True)
    now = _now()
    # A drafting claim whose window lapsed has no live holder, so it may be replaced.
    lapsed = (
        row is not None
        and row["state"] == "drafting"
        and row["retry_after"] is not None
        and row["retry_after"] <= now
    )
    if row is not None and row["state"] in BUSY and not lapsed:
        raise OperationRefused("draft_in_progress", "a draft is already being prepared")
    if (
        row is not None
        and not m.force
        and any(_edited(row, x) for x in _sections(uow, identifier))
    ):
        raise OperationRefused(
            "draft_edited",
            "a section was edited since the last draft; request with force to "
            "replace it",
        )
    questions = row["questions"] if row is not None else None
    if m.questions is not None:
        questions = m.questions.strip() or None
    posting = bool(row["separate_answers"]) if row is not None else False
    if m.separate_answers is not None:
        posting = m.separate_answers
    values = {
        "state": "queued",
        "questions": questions,
        "separate_answers": posting,
        "stage": None,
        "step": None,
        "total": None,
        "attempts": 0,
        "retry_after": None,
        "failure_kind": None,
        "requested_by_kind": ctx.actor.kind.value,
        "requested_by_id": ctx.actor.id,
        "requested_at": now,
        "updated_at": now,
    }
    table = s.job_proposal
    uow.connection.execute(
        pg_insert(table)
        .values(opportunity_id=identifier, **values)
        .on_conflict_do_update(index_elements=[table.c.opportunity_id], set_=values)
    )
    queue_sweep(uow, workspace_id=ctx.workspace_id, now=now)
    return view(uow, identifier)


def get(ctx: WorkspaceContext, uow: UnitOfWork, m: RefInput) -> ProposalView:
    h.require(ctx)
    record = _job(uow, m, writing=False)
    return view(uow, record["id"])


def _editable(uow: UnitOfWork, m: RefInput) -> Mapping[str, Any]:
    record = _job(uow, m, writing=True)
    row = _row(uow, record["id"], lock=True)
    if row is None or not _sections(uow, record["id"]):
        raise OperationRefused("not_found", "no draft to edit")
    if row["state"] in BUSY:
        raise OperationRefused("draft_in_progress", "a draft is being prepared")
    return row


def _section(uow: UnitOfWork, identifier: UUID, section_id: UUID) -> dict[str, Any]:
    found = next((x for x in _sections(uow, identifier) if x["id"] == section_id), None)
    if found is None:
        raise OperationRefused("not_found", "section unavailable")
    return found


def set_section(
    ctx: WorkspaceContext, uow: UnitOfWork, m: SectionSetInput
) -> ProposalView:
    h.require(ctx)
    row = _editable(uow, m)
    identifier = row["opportunity_id"]
    section = _section(uow, identifier, m.section_id)
    if section["body"] != m.body:
        uow.connection.execute(
            update(s.proposal_section)
            .where(s.proposal_section.c.id == section["id"])
            .values(body=m.body, updated_at=_now())
        )
    return view(uow, identifier)


def update_draft(
    ctx: WorkspaceContext, uow: UnitOfWork, m: DraftUpdateInput
) -> ProposalView:
    """Write an edited whole draft through to the sections. Answers pair with the
    existing answer sections in order; a changed section is stamped (so it counts as a
    hand edit), new ones are added and surplus ones removed. A text with no divider
    replaces only the cover letter, so sending just a revised letter cannot drop
    every answer."""
    h.require(ctx)
    row = _editable(uow, m)
    identifier = row["opportunity_id"]
    parsed = parse_draft(m.draft)
    letter_only = "\n---\n" not in m.draft
    now = _now()
    section = s.proposal_section
    existing = _sections(uow, identifier)
    cover = next(x for x in existing if x["kind"] == "cover_letter")
    if cover["body"] != parsed.letter:
        if not parsed.letter.strip():
            raise OperationRefused("input_invalid", "the cover letter cannot be empty")
        uow.connection.execute(
            update(section)
            .where(section.c.id == cover["id"])
            .values(body=parsed.letter, updated_at=now)
        )
    if not letter_only:
        answers = [x for x in existing if x["kind"] == "answer"]
        keep = parsed.answers
        surplus = answers[len(keep) :]
        for old in surplus:
            uow.connection.execute(delete(section).where(section.c.id == old["id"]))
        if surplus:
            # Removing answers is a hand edit too: stamp the letter so a regenerate
            # asks for force.
            uow.connection.execute(
                update(section)
                .where(section.c.id == cover["id"])
                .values(updated_at=now)
            )
        for i, item in enumerate(keep, start=1):
            question = item.question or UNLABELLED
            if i <= len(answers):
                old = answers[i - 1]
                changes: dict[str, Any] = {}
                if old["question"] != question:
                    changes["question"] = question
                if old["body"] != item.answer:
                    changes["body"] = item.answer
                if changes:
                    uow.connection.execute(
                        update(section)
                        .where(section.c.id == old["id"])
                        .values(**changes, updated_at=now)
                    )
            else:
                uow.connection.execute(
                    insert(section).values(
                        id=uuid7(),
                        opportunity_id=identifier,
                        position=i,
                        kind="answer",
                        question=question,
                        body=item.answer,
                        library_answer_id=None,
                        updated_at=now,
                    )
                )
    return view(uow, identifier)


def delete_draft(ctx: WorkspaceContext, uow: UnitOfWork, m: RefInput) -> ProposalView:
    h.require(ctx)
    record = _job(uow, m, writing=True)
    identifier = record["id"]
    if _row(uow, identifier, lock=True) is None:
        return view(uow, identifier)
    uow.connection.execute(
        delete(s.proposal_section).where(
            s.proposal_section.c.opportunity_id == identifier
        )
    )
    # Idle stops a queued draft and makes a running one's result stale.
    uow.connection.execute(
        update(s.job_proposal)
        .where(s.job_proposal.c.opportunity_id == identifier)
        .values(
            state="idle",
            stage=None,
            step=None,
            total=None,
            findings=None,
            generated_at=None,
            retry_after=None,
            failure_kind=None,
            attempts=0,
            updated_at=_now(),
        )
    )
    return view(uow, identifier)


def _entry(row: Mapping[str, Any]) -> LibraryEntry:
    return LibraryEntry(
        id=row["id"],
        question=row["question"],
        answer=row["answer"],
        updated_at=row["updated_at"],
    )


def library_save(
    ctx: WorkspaceContext, uow: UnitOfWork, m: LibrarySave
) -> LibraryEntry:
    """Keep ``answer`` under the question; the same normalised question updates the
    existing entry, never adds a second."""
    h.require(ctx)
    now = _now()
    table = s.answer_library
    values = {
        "question": m.question.strip(),
        "answer": m.answer,
        "updated_by_kind": ctx.actor.kind.value,
        "updated_by_id": ctx.actor.id,
        "updated_at": now,
    }
    row = (
        uow.connection.execute(
            pg_insert(table)
            .values(
                id=uuid7(),
                question_key=question_key(m.question),
                created_at=now,
                **values,
            )
            .on_conflict_do_update(index_elements=[table.c.question_key], set_=values)
            .returning(table)
        )
        .mappings()
        .one()
    )
    return _entry(dict(row))


def _match(uow: UnitOfWork, question: str) -> dict[str, Any] | None:
    table = s.answer_library
    found = (
        uow.connection.execute(
            select(table).where(table.c.question_key == question_key(question))
        )
        .mappings()
        .one_or_none()
    )
    return dict(found) if found is not None else None


def library_match(
    ctx: WorkspaceContext, uow: UnitOfWork, m: LibraryQuestion
) -> LibraryMatch:
    h.require(ctx)
    found = _match(uow, m.question)
    return LibraryMatch(entry=_entry(found) if found is not None else None)


def use_library(
    ctx: WorkspaceContext, uow: UnitOfWork, m: SectionInput
) -> ProposalView:
    h.require(ctx)
    row = _editable(uow, m)
    identifier = row["opportunity_id"]
    section = _section(uow, identifier, m.section_id)
    found = _match(uow, section["question"] or "") if section["question"] else None
    if found is None:
        raise OperationRefused("not_found", "no library answer for this question")
    uow.connection.execute(
        update(s.proposal_section)
        .where(s.proposal_section.c.id == section["id"])
        .values(body=found["answer"], library_answer_id=found["id"], updated_at=_now())
    )
    return view(uow, identifier)


def _declare(
    name: str,
    model: type[Strict],
    output: type[Strict],
    handler: Handler,
    safety: SafetyClass,
    roles: frozenset[Role],
    subject: str | None,
) -> tuple[OperationDeclaration, Handler]:
    return (
        OperationDeclaration(
            name=name,
            safety_class=safety,
            roles=roles,
            input_model=model,
            output=output,
            idempotency=Idempotency.NONE,
            audit=None
            if safety is SafetyClass.READ
            else AuditSpec(subject_field=subject),
        ),
        handler,
    )


OPERATIONS: tuple[tuple[OperationDeclaration, Handler], ...] = (
    _declare(
        "leads.jobproposal.request",
        RequestInput,
        ProposalView,
        request,
        SafetyClass.DRAFT,
        MEMBERS,
        "ref",
    ),
    _declare(
        "leads.jobproposal.get",
        RefInput,
        ProposalView,
        get,
        SafetyClass.READ,
        READERS,
        None,
    ),
    _declare(
        "leads.proposalsection.set",
        SectionSetInput,
        ProposalView,
        set_section,
        SafetyClass.DRAFT,
        MEMBERS,
        "ref",
    ),
    _declare(
        "leads.proposalsection.use_library",
        SectionInput,
        ProposalView,
        use_library,
        SafetyClass.DRAFT,
        MEMBERS,
        "ref",
    ),
    _declare(
        "leads.jobproposal.update",
        DraftUpdateInput,
        ProposalView,
        update_draft,
        SafetyClass.DRAFT,
        MEMBERS,
        "ref",
    ),
    _declare(
        "leads.jobproposal.delete",
        RefInput,
        ProposalView,
        delete_draft,
        SafetyClass.DRAFT,
        MEMBERS,
        "ref",
    ),
    # No subject: an audit row names the operation, never the question text.
    _declare(
        "leads.answerlibrary.save",
        LibrarySave,
        LibraryEntry,
        library_save,
        SafetyClass.MUTATE,
        MEMBERS,
        None,
    ),
    _declare(
        "leads.answerlibrary.match",
        LibraryQuestion,
        LibraryMatch,
        library_match,
        SafetyClass.READ,
        READERS,
        None,
    ),
) + pricing_operations(ProposalView)


# The MCP names for the proposal operations, so an agent reads and edits a draft
# through operations_call instead of the web form. Same operations, same roles and
# safety classes; a tool only names one.
NOTHING_SENT: Final = (
    " Prepares text only: nothing is sent or submitted; the owner applies on the "
    "platform by hand. ref takes an opportunity reference or its short ID; a missing "
    "opportunity returns not_found, and one that is not a Job search opportunity "
    "returns not_job_search."
)
TOOL_SPECS: Final = {
    "leads.jobproposal.get": (
        "leads_get_proposal",
        "Read the proposal drafted for a Job search opportunity: state (idle when "
        "none was requested, queued, drafting, ready or failed), progress, the "
        "screening questions, each section with its id, the findings, pricing and "
        "milestones, and draft, the whole text with the cover letter first.",
    ),
    "leads.jobproposal.request": (
        "leads_request_proposal",
        "Queue a proposal draft for a Job search opportunity; read it back with "
        "leads_get_proposal once state is ready. questions replaces the stored "
        "screening questions (an empty string clears them; omit to keep them). "
        "separate_answers true gives each question its own answer section. Refuses "
        "profile_required without a proposal profile, draft_in_progress while one "
        "is queued or drafting, and draft_edited when a section was edited since the "
        "last draft unless force is true, which replaces every section.",
    ),
    "leads.jobproposal.update": (
        "leads_update_proposal",
        "Replace the whole proposal draft from its text form, as leads_get_proposal "
        "returns it in draft. A text with no --- divider replaces only the cover "
        "letter. Refuses not_found when there is no draft to edit.",
    ),
    "leads.jobproposal.delete": (
        "leads_delete_proposal",
        "Delete the proposal draft and keep its screening questions; also stops a "
        "queued or running draft.",
    ),
    "leads.proposalsection.set": (
        "leads_set_proposal_section",
        "Replace one section's body. section_id comes from leads_get_proposal; an "
        "unknown one returns not_found. The section then counts as hand edited.",
    ),
    "leads.proposalsection.use_library": (
        "leads_use_library_answer",
        "Fill one section with the answer library's answer to its question. Refuses "
        "not_found when the library has no answer for it.",
    ),
    "leads.answerlibrary.save": (
        "leads_save_library_answer",
        "Keep an answer in the workspace answer library under its question, "
        "matched case and punctuation blind; saving the same question again "
        "replaces the answer. Not tied to any opportunity.",
    ),
    "leads.answerlibrary.match": (
        "leads_match_library_answer",
        "Find the answer library's answer to a question; entry is null when there "
        "is none.",
    ),
    "leads.jobpricing.request": (
        "leads_request_pricing",
        "Queue milestone pricing for a Job search opportunity, which sets the "
        "proposal to price by milestones. Refuses pricing_not_set when the proposal "
        "policy has no pricing section and pricing_in_progress while one is queued "
        "or running.",
    ),
    "leads.proposalmilestone.set": (
        "leads_set_proposal_milestone",
        "Edit one milestone's title, description, amount or due_on (YYYY-MM-DD); "
        "fields left out keep their value. milestone_id comes from "
        "leads_get_proposal; an unknown one returns not_found.",
    ),
}
LIBRARY: Final = frozenset({"leads.answerlibrary.save", "leads.answerlibrary.match"})
TOOLS: tuple[ToolDeclaration, ...] = tuple(
    ToolDeclaration(
        name=TOOL_SPECS[declaration.name][0],
        operation=declaration.name,
        safety_class=declaration.safety_class,
        input_model=declaration.input_model,
        description=TOOL_SPECS[declaration.name][1]
        + ("" if declaration.name in LIBRARY else NOTHING_SENT),
    )
    for declaration, _ in OPERATIONS
)
