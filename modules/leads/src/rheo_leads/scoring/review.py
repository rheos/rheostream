"""The review queue: jobs the client filters held back, and the owner's override.

``leads.ui.queue`` with ``held_only`` lists them (open Job search jobs whose current
assessment is a tier-1 hold and that nobody promoted), each with the clauses that
held it (:func:`hold_reasons`), decided again against the current profile so the
badges follow the thresholds the owner has now.

``leads.jobreview.promote`` records the override (``job_promotion``) and puts the
job back in line for scoring: the next sweep sends it to the model like a job that
passed, with the clauses that held it kept as conditions. Promoting twice is a
no-op; a job that is not held is refused (``not_held``), so a stray click never
spends a model call on a job that already has its score.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import exists, select, update
from sqlalchemy.dialects.postgresql import insert

from rheo_leads import pipeline_common as h
from rheo_leads.pipeline_contracts import RefInput, Strict
from rheo_leads.references import ref
from rheo_leads.scoring import job as scoring
from rheo_leads.scoring.profile import read_profile
from rheo_leads.scoring.tier1 import JobFacts, run_tier1
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import scoring as s

MEMBERS = frozenset({Role.OWNER, Role.MEMBER})
# A clause the current profile no longer names (the job waits to be rescored),
# or a hold read without a profile: its sentence comes from the stored assessment.
OTHER = "other"


class PromoteOutput(Strict):
    ref: str
    promoted_at: datetime


def latest(column: Any, opportunity_id: Any) -> Any:
    """``column`` of the opportunity's current assessment, as a scalar subquery."""
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


def is_held(opportunity_id: Any) -> Any:
    """The current assessment is a tier-1 hold: no model score, and the hold's
    first line."""
    qual = p.qualification
    return (latest(qual.c.score, opportunity_id).is_(None)) & (
        latest(qual.c.explanation, opportunity_id).startswith(scoring.HELD_LINE)
    )


def not_promoted(opportunity_id: Any) -> Any:
    table = s.job_promotion
    return ~exists().where(table.c.opportunity_id == opportunity_id)


def hold_reasons(
    uow: UnitOfWork,
    records: Sequence[Mapping[str, Any]],
    stored: Mapping[UUID, list[str]],
) -> dict[UUID, list[dict[str, str]]]:
    """Each held job's failed clauses as ``{code, text}``, from the current profile;
    the stored assessment's lines when there is no profile or nothing fails now."""
    try:
        profile = read_profile(uow)
    except ValidationError:
        profile = None
    fields = scoring.read_fields(uow, [r["id"] for r in records])
    found: dict[UUID, list[dict[str, str]]] = {}
    for record in records:
        identifier = record["id"]
        reasons: list[dict[str, str]] = []
        if profile is not None:
            decided = run_tier1(
                JobFacts.from_fields(record["title"], fields[identifier]),
                profile.policy.tier1,
            )
            reasons = [
                {"code": code, "text": text}
                for code, text in zip(decided.codes, decided.failed, strict=True)
            ]
        if not reasons:
            lines = [
                line for line in stored.get(identifier, []) if line != scoring.HELD_LINE
            ]
            reasons = [{"code": OTHER, "text": line} for line in lines]
        found[identifier] = reasons
    return found


def promote(ctx: WorkspaceContext, uow: UnitOfWork, m: RefInput) -> PromoteOutput:
    h.require(ctx)
    if not h.jobsearch_enabled(ctx, uow):
        raise OperationRefused("capability_disabled", "job search is off")
    record = h.opportunity(uow, m, writing=True)
    identifier: UUID = record["id"]
    if record["preset_id"] != h.JOBSEARCH_PRESET_ID:
        raise OperationRefused("not_job_search", "only a Job search job is promoted")
    table = s.job_promotion
    existing = uow.connection.execute(
        select(table.c.promoted_at).where(table.c.opportunity_id == identifier)
    ).scalar_one_or_none()
    if existing is not None:
        return PromoteOutput(ref=ref("opportunity", identifier), promoted_at=existing)
    stage = p.preset_stage
    open_stage: str = uow.connection.execute(
        select(stage.c.kind).where(
            stage.c.preset_id == record["preset_id"],
            stage.c.version == record["preset_version"],
            stage.c.stage_id == record["stage_id"],
        )
    ).scalar_one()
    held: bool | None = uow.connection.execute(select(is_held(identifier))).scalar_one()
    if open_stage != "open" or not held:
        raise OperationRefused(
            "not_held", "the client filters are not holding this job"
        )
    now = datetime.now(UTC)
    uow.connection.execute(
        insert(table)
        .values(
            opportunity_id=identifier,
            promoted_by_kind=ctx.actor.kind.value,
            promoted_by_id=ctx.actor.id,
            promoted_at=now,
        )
        .on_conflict_do_nothing(index_elements=[table.c.opportunity_id])
    )
    # Back in line with a fresh attempt count; a job the sweep has not seen yet
    # gets its state from the next refresh.
    state = s.job_score_state
    uow.connection.execute(
        update(state)
        .where(state.c.opportunity_id == identifier)
        .values(
            state="pending",
            attempts=0,
            retry_after=None,
            failure_kind=None,
            updated_at=now,
        )
    )
    scoring.queue_sweep(uow, ctx.workspace_id, now=now)
    return PromoteOutput(ref=ref("opportunity", identifier), promoted_at=now)


OPERATIONS: tuple[tuple[OperationDeclaration, Handler], ...] = (
    (
        OperationDeclaration(
            name="leads.jobreview.promote",
            safety_class=SafetyClass.MUTATE,
            roles=MEMBERS,
            input_model=RefInput,
            output=PromoteOutput,
            # Promoting a promoted job answers its first promotion.
            idempotency=Idempotency.NATURAL,
            audit=AuditSpec(subject_field="ref"),
        ),
        promote,
    ),
)
