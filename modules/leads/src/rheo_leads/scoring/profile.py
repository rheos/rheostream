"""The job-search profile: ``leads.jobprofile.set`` and ``leads.jobprofile.get``.

The profile is the candidate's own evidence (career, skills, portfolio) as plain text,
and the policy says how to judge a job against it: goals, exclusions, preferences,
economics, known gaps, per-platform notes and the tier-1 thresholds. Both are the
workspace's data, set by the owner and exported with Leads. Nothing in this package
ships a profile, and no log line or audit record carries its text: the audit row
names no subject, and the scorer logs counts and fixed words only.

Setting the profile also turns the scoring schedule on and queues a scoring sweep.
The owner who sets it is the account whose model login the scorer then runs as.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from pydantic import Field, model_validator
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
from rheo_core.work.schedules import ensure_module_schedule
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from rheo_leads import pipeline_common as h
from rheo_leads.configuration import MODULE_ID
from rheo_leads.pipeline_contracts import Empty, Strict
from rheo_leads.storage import scoring as s

SCHEDULE_NAME: Final = "jobsearch_score"
MAX_PROFILE_CHARS: Final = 100_000
MAX_ITEM_CHARS: Final = 2_000
MAX_ITEMS: Final = 20


class Tier1Policy(Strict):
    """The deterministic client filters. An unknown value never fails a clause."""

    min_client_spend: Decimal = Field(default=Decimal("500"), ge=0)
    min_client_rating: Decimal = Field(default=Decimal("4.5"), ge=0, le=5)
    min_hire_rate: Decimal = Field(default=Decimal("0.40"), ge=0, le=1)
    max_proposals: int = Field(default=50, ge=1, le=1000)


class ScoringPolicy(Strict):
    """How the model judges a job against the profile; every list is the owner's."""

    goals: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    exclusions: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    preferences: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    economics: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    known_gaps: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    platform_notes: dict[str, str] = Field(default_factory=dict, max_length=MAX_ITEMS)
    # The owner's own revision label for the policy text, shown to the model.
    version: int | None = Field(default=None, ge=1)
    tier1: Tier1Policy = Field(default_factory=Tier1Policy)

    @model_validator(mode="after")
    def bounded(self) -> "ScoringPolicy":
        for items in (
            self.goals,
            self.exclusions,
            self.preferences,
            self.economics,
            self.known_gaps,
            *([v] for v in self.platform_notes.values()),
        ):
            if any(not item.strip() or len(item) > MAX_ITEM_CHARS for item in items):
                raise ValueError("policy entries must be non-empty and bounded")
        if any(not key.strip() or len(key) > 64 for key in self.platform_notes):
            raise ValueError("platform note keys must be short names")
        return self

    def model_context(self, platform: str) -> dict[str, Any]:
        """What the model reads: everything but the tier-1 thresholds, with only
        the job's own platform note."""
        context: dict[str, Any] = {
            "economics": self.economics,
            "exclusions": self.exclusions,
            "goals": self.goals,
            "known_gaps": self.known_gaps,
            "preferences": self.preferences,
        }
        if self.version is not None:
            context["version"] = self.version
        context["platform_notes"] = self.platform_notes.get(platform, "")
        return context


class ProfileSet(Strict):
    profile: str = Field(min_length=1, max_length=MAX_PROFILE_CHARS)
    policy: ScoringPolicy = Field(default_factory=ScoringPolicy)


class ProfileState(Strict):
    """The stored profile. ``set`` answers without the text; ``get`` includes it."""

    version: int
    updated_at: datetime
    profile_chars: int
    profile: str | None = None
    policy: ScoringPolicy


class StoredProfile(Strict):
    profile: str
    policy: ScoringPolicy
    version: int
    scoring_account_id: Any


def read_profile(uow: UnitOfWork) -> StoredProfile | None:
    row = uow.connection.execute(select(s.jobsearch_profile)).mappings().one_or_none()
    if row is None:
        return None
    return StoredProfile(
        profile=row["profile"],
        policy=ScoringPolicy.model_validate(row["policy"]),
        version=row["version"],
        scoring_account_id=row["scoring_account_id"],
    )


def _state(row: Any, *, with_text: bool) -> ProfileState:
    return ProfileState(
        version=row["version"],
        updated_at=row["updated_at"],
        profile_chars=len(row["profile"]),
        profile=row["profile"] if with_text else None,
        policy=ScoringPolicy.model_validate(row["policy"]),
    )


def set_profile(ctx: WorkspaceContext, uow: UnitOfWork, m: ProfileSet) -> ProfileState:
    h.require(ctx)
    if not h.jobsearch_enabled(ctx, uow):
        raise OperationRefused("capability_disabled", "job search is off")
    account = ctx.principal.account_id
    if account is None:
        raise OperationRefused("account_required", "an owner account sets the profile")
    h.lock(uow)
    now = datetime.now(UTC)
    values = {
        "profile": m.profile,
        "policy": m.policy.model_dump(mode="json"),
        "scoring_account_id": account,
        "updated_by_kind": ctx.actor.kind.value,
        "updated_by_id": ctx.actor.id,
        "updated_at": now,
    }
    table = s.jobsearch_profile
    statement = insert(table).values(singleton=True, version=1, **values)
    row = (
        uow.connection.execute(
            statement.on_conflict_do_update(
                index_elements=[table.c.singleton],
                set_={**values, "version": table.c.version + 1},
            ).returning(table)
        )
        .mappings()
        .one()
    )
    # The lifecycle lock is held, which orders this against module enable.
    ensure_module_schedule(
        uow.connection,
        module_id=MODULE_ID,
        name=SCHEDULE_NAME,
        enabled=True,
        due_at=now,
    )
    from rheo_leads.scoring.job import queue_sweep, repend_unscored

    repend_unscored(uow, now=now)
    queue_sweep(uow, ctx.workspace_id, now=now)
    return _state(row, with_text=False)


def get_profile(ctx: WorkspaceContext, uow: UnitOfWork, m: Empty) -> ProfileState:
    h.require(ctx)
    row = uow.connection.execute(select(s.jobsearch_profile)).mappings().one_or_none()
    if row is None:
        raise OperationRefused("not_found", "no job-search profile is set")
    return _state(row, with_text=True)


OPERATIONS: tuple[tuple[OperationDeclaration, Handler], ...] = (
    (
        OperationDeclaration(
            name="leads.jobprofile.set",
            safety_class=SafetyClass.MUTATE,
            roles=frozenset({Role.OWNER}),
            input_model=ProfileSet,
            output=ProfileState,
            # Setting the same text again only bumps the version.
            idempotency=Idempotency.NATURAL,
            # No subject: the audit row names the operation, never the text.
            audit=AuditSpec(subject_field=None),
        ),
        set_profile,
    ),
    (
        OperationDeclaration(
            name="leads.jobprofile.get",
            safety_class=SafetyClass.READ,
            roles=frozenset({Role.OWNER}),
            input_model=Empty,
            output=ProfileState,
            idempotency=Idempotency.NONE,
        ),
        get_profile,
    ),
)
