"""The pricing operations, and the pricing part of ``leads.jobproposal.get``.

- ``leads.jobpricing.request`` queues milestone generation for a Job search
  opportunity. Generating milestones is the choice to price by milestones, so the
  request sets that mode. It refuses ``pricing_not_set`` when the proposal policy has
  no pricing section, and ``pricing_in_progress`` while one is queued or running.
- ``leads.proposalmilestone.set`` edits one milestone's title, description, amount
  or due date.

Both answer the whole proposal view, like the other proposal operations. Nothing is
sent anywhere: a milestone schedule is what the owner copies into an apply form.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from pydantic import Field
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
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rheo_leads import pipeline_common as h
from rheo_leads.pipeline_contracts import RefInput, Strict
from rheo_leads.proposals import pricing
from rheo_leads.proposals.pricing_store import amounts, milestone_rows
from rheo_leads.proposals.profile import read_proposal_profile
from rheo_leads.scoring.tier1 import EXTENSION
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import proposals as s

MEMBERS: Final = frozenset({Role.OWNER, Role.MEMBER})
BUSY: Final = ("queued", "running")


class MilestoneSetInput(RefInput):
    milestone_id: UUID
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=4_000)
    amount: Decimal | None = Field(default=None, ge=0, le=10_000_000, decimal_places=2)
    due_on: date | None = None


class MilestoneView(Strict):
    id: UUID
    position: int
    title: str
    description: str | None
    amount: str
    due_on: date | None


class PricingView(Strict):
    # Whether the owner's proposal policy has pricing at all.
    available: bool
    state: str = "idle"
    mode: str | None = None
    failure_kind: str | None = None
    warning: str | None = None
    too_vague: str | None = None
    generated_at: datetime | None = None
    milestones: list[MilestoneView] = Field(default_factory=list)
    # The hourly form rate and its basis, from the milestones and the analysis.
    suggestion: dict[str, Any] | None = None


def _listing_type(uow: UnitOfWork, identifier: UUID) -> str | None:
    state = p.opportunity_field_state
    found: str | None = uow.connection.execute(
        select(state.c.value_text).where(
            state.c.opportunity_id == identifier,
            state.c.target == EXTENSION + "job_type",
            state.c.value_kind == "value",
            state.c.orphaned.is_(False),
        )
    ).scalar_one_or_none()
    return found


def pricing_view(uow: UnitOfWork, identifier: UUID, findings: Any) -> PricingView:
    """The job's pricing as the detail page reads it."""
    try:
        profile = read_proposal_profile(uow)
    except ValueError:
        profile = None
    policy = profile.policy.pricing if profile is not None else None
    row = (
        uow.connection.execute(
            select(s.job_pricing).where(s.job_pricing.c.opportunity_id == identifier)
        )
        .mappings()
        .one_or_none()
    )
    rows = milestone_rows(uow, identifier)
    analysis = findings.get("analysis") if isinstance(findings, dict) else None
    suggestion = (
        pricing.suggest(
            policy,
            listing_type=_listing_type(uow, identifier),
            amounts=amounts(rows),
            analysis=analysis if isinstance(analysis, dict) else {},
        ).as_dict()
        if policy is not None
        else None
    )
    milestones = [
        MilestoneView(
            id=r["id"],
            position=r["position"],
            title=r["title"],
            description=r["description"],
            amount=str(r["amount"]),
            due_on=r["due_on"],
        )
        for r in rows
    ]
    if row is None:
        return PricingView(
            available=policy is not None, milestones=milestones, suggestion=suggestion
        )
    return PricingView(
        available=policy is not None,
        state=row["state"],
        mode=row["mode"],
        failure_kind=row["failure_kind"],
        warning=row["warning"],
        too_vague=row["too_vague"],
        generated_at=row["generated_at"],
        milestones=milestones,
        suggestion=suggestion,
    )


def request(ctx: WorkspaceContext, uow: UnitOfWork, m: RefInput) -> Any:
    from rheo_leads.proposals.job import queue_sweep
    from rheo_leads.proposals.operations import _job, view

    h.require(ctx)
    if not h.jobsearch_enabled(ctx, uow):
        raise OperationRefused("capability_disabled", "job search is off")
    record = _job(uow, m, writing=True)
    profile = read_proposal_profile(uow)
    if profile is None or profile.policy.pricing is None:
        raise OperationRefused(
            "pricing_not_set", "the proposal profile has no pricing yet"
        )
    identifier = record["id"]
    table = s.job_pricing
    current = uow.connection.execute(
        select(table.c.state)
        .where(table.c.opportunity_id == identifier)
        .with_for_update()
    ).scalar_one_or_none()
    if current in BUSY:
        raise OperationRefused(
            "pricing_in_progress", "milestones are already being prepared"
        )
    now = datetime.now(UTC)
    values = {
        "state": "queued",
        # Generating milestones is the choice to price by milestones.
        "mode": "milestones",
        "attempts": 0,
        "retry_after": None,
        "failure_kind": None,
        "requested_at": now,
        "updated_at": now,
    }
    uow.connection.execute(
        pg_insert(table)
        .values(opportunity_id=identifier, **values)
        .on_conflict_do_update(index_elements=[table.c.opportunity_id], set_=values)
    )
    queue_sweep(uow, workspace_id=ctx.workspace_id, now=now)
    return view(uow, identifier)


def set_milestone(ctx: WorkspaceContext, uow: UnitOfWork, m: MilestoneSetInput) -> Any:
    from rheo_leads.proposals.operations import _job, view

    h.require(ctx)
    record = _job(uow, m, writing=True)
    identifier = record["id"]
    state = uow.connection.execute(
        select(s.job_pricing.c.state)
        .where(s.job_pricing.c.opportunity_id == identifier)
        .with_for_update()
    ).scalar_one_or_none()
    if state in BUSY:
        raise OperationRefused("pricing_in_progress", "milestones are being prepared")
    changes: dict[str, Any] = {
        name: getattr(m, name)
        for name in ("title", "description", "amount", "due_on")
        if name in m.model_fields_set
    }
    if "title" in changes and changes["title"] is None:
        raise OperationRefused("input_invalid", "a milestone needs a title")
    if "amount" in changes and changes["amount"] is None:
        raise OperationRefused("input_invalid", "a milestone needs an amount")
    milestone = s.proposal_milestone
    found = uow.connection.execute(
        update(milestone)
        .where(
            milestone.c.id == m.milestone_id,
            milestone.c.opportunity_id == identifier,
        )
        .values(**changes, updated_at=datetime.now(UTC))
        .returning(milestone.c.id)
    ).scalar_one_or_none()
    if found is None:
        raise OperationRefused("not_found", "milestone unavailable")
    return view(uow, identifier)


def operations(
    output: type[Strict],
) -> tuple[tuple[OperationDeclaration, Handler], ...]:
    return (
        (
            OperationDeclaration(
                name="leads.jobpricing.request",
                safety_class=SafetyClass.DRAFT,
                roles=MEMBERS,
                input_model=RefInput,
                output=output,
                idempotency=Idempotency.NONE,
                audit=AuditSpec(subject_field="ref"),
            ),
            request,
        ),
        (
            OperationDeclaration(
                name="leads.proposalmilestone.set",
                safety_class=SafetyClass.DRAFT,
                roles=MEMBERS,
                input_model=MilestoneSetInput,
                output=output,
                idempotency=Idempotency.NONE,
                audit=AuditSpec(subject_field="ref"),
            ),
            set_milestone,
        ),
    )
