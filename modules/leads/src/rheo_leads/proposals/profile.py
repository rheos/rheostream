"""The proposal profile: ``leads.proposalprofile.set`` and ``.get``.

The profile is the candidate's own evidence for applications (projects, capability
areas, links) as plain text, and the policy is how the owner wants it used: writing
notes the drafter must follow (which project a tool belongs to, which links to give),
example letters to learn editorial moves from, vouched anecdotes the drafter may tell
when a posting asks for a story, per-platform notes and extra named terms the accuracy
check accepts. All of it is the workspace's data, set by the owner and exported with
Leads. Nothing in this package ships a profile, and no log line or audit record carries
its text.

The owner who sets it is the account whose model login drafts proposals, the same
rule the scoring profile follows.
"""

from datetime import UTC, datetime
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
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from rheo_leads import pipeline_common as h
from rheo_leads.pipeline_contracts import Empty, Strict
from rheo_leads.storage import proposals as s

MAX_PROFILE_CHARS: Final = 100_000
MAX_EXAMPLES_CHARS: Final = 60_000
MAX_NOTE_CHARS: Final = 4_000
MAX_STORY_CHARS: Final = 4_000
MAX_NOTES: Final = 40
MAX_ANECDOTES: Final = 20
MAX_TERMS: Final = 300


class Anecdote(Strict):
    """A true story the candidate can speak to live, told only when a posting asks."""

    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    story: str = Field(min_length=1, max_length=MAX_STORY_CHARS)
    tags: list[str] = Field(default_factory=list, max_length=20)
    use_when: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def bounded(self) -> "Anecdote":
        if any(not tag.strip() or len(tag) > 64 for tag in self.tags):
            raise ValueError("anecdote tags must be short words")
        return self


class ProposalPolicy(Strict):
    """How the drafter uses the profile; every entry is the owner's."""

    writing_notes: list[str] = Field(default_factory=list, max_length=MAX_NOTES)
    example_letters: str = Field(default="", max_length=MAX_EXAMPLES_CHARS)
    anecdotes: list[Anecdote] = Field(default_factory=list, max_length=MAX_ANECDOTES)
    platform_notes: dict[str, str] = Field(default_factory=dict, max_length=20)
    entity_terms: list[str] = Field(default_factory=list, max_length=MAX_TERMS)
    # The owner's own revision label for the policy text.
    version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def bounded(self) -> "ProposalPolicy":
        for items, cap in (
            (self.writing_notes, MAX_NOTE_CHARS),
            (list(self.platform_notes.values()), MAX_NOTE_CHARS),
            (self.entity_terms, 100),
        ):
            if any(not item.strip() or len(item) > cap for item in items):
                raise ValueError("policy entries must be non-empty and bounded")
        if any(not key.strip() or len(key) > 64 for key in self.platform_notes):
            raise ValueError("platform note keys must be short names")
        if len({a.id for a in self.anecdotes}) != len(self.anecdotes):
            raise ValueError("anecdote ids must be unique")
        return self


class ProposalProfileSet(Strict):
    profile: str = Field(min_length=1, max_length=MAX_PROFILE_CHARS)
    policy: ProposalPolicy = Field(default_factory=ProposalPolicy)


class ProposalProfileState(Strict):
    """The stored profile. ``set`` answers without the text; ``get`` includes it."""

    version: int
    updated_at: datetime
    profile_chars: int
    profile: str | None = None
    policy: ProposalPolicy | None = None


class StoredProposalProfile(Strict):
    profile: str
    policy: ProposalPolicy
    version: int
    drafting_account_id: Any


def read_proposal_profile(uow: UnitOfWork) -> StoredProposalProfile | None:
    row = uow.connection.execute(select(s.proposal_profile)).mappings().one_or_none()
    if row is None:
        return None
    return StoredProposalProfile(
        profile=row["profile"],
        policy=ProposalPolicy.model_validate(row["policy"]),
        version=row["version"],
        drafting_account_id=row["drafting_account_id"],
    )


def _state(row: Any, *, with_text: bool) -> ProposalProfileState:
    return ProposalProfileState(
        version=row["version"],
        updated_at=row["updated_at"],
        profile_chars=len(row["profile"]),
        profile=row["profile"] if with_text else None,
        policy=ProposalPolicy.model_validate(row["policy"]) if with_text else None,
    )


def set_proposal_profile(
    ctx: WorkspaceContext, uow: UnitOfWork, m: ProposalProfileSet
) -> ProposalProfileState:
    h.require(ctx)
    if not h.jobsearch_enabled(ctx, uow):
        raise OperationRefused("capability_disabled", "job search is off")
    account = ctx.principal.account_id
    if account is None:
        raise OperationRefused("account_required", "an owner account sets the profile")
    h.lock(uow)
    values = {
        "profile": m.profile,
        "policy": m.policy.model_dump(mode="json"),
        "drafting_account_id": account,
        "updated_by_kind": ctx.actor.kind.value,
        "updated_by_id": ctx.actor.id,
        "updated_at": datetime.now(UTC),
    }
    table = s.proposal_profile
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
    return _state(row, with_text=False)


def get_proposal_profile(
    ctx: WorkspaceContext, uow: UnitOfWork, m: Empty
) -> ProposalProfileState:
    h.require(ctx)
    row = uow.connection.execute(select(s.proposal_profile)).mappings().one_or_none()
    if row is None:
        raise OperationRefused("not_found", "no proposal profile is set")
    return _state(row, with_text=True)


OPERATIONS: tuple[tuple[OperationDeclaration, Handler], ...] = (
    (
        OperationDeclaration(
            name="leads.proposalprofile.set",
            safety_class=SafetyClass.MUTATE,
            roles=frozenset({Role.OWNER}),
            input_model=ProposalProfileSet,
            output=ProposalProfileState,
            idempotency=Idempotency.NATURAL,
            # No subject: the audit row names the operation, never the text.
            audit=AuditSpec(subject_field=None),
        ),
        set_proposal_profile,
    ),
    (
        OperationDeclaration(
            name="leads.proposalprofile.get",
            safety_class=SafetyClass.READ,
            roles=frozenset({Role.OWNER}),
            input_model=Empty,
            output=ProposalProfileState,
            idempotency=Idempotency.NONE,
        ),
        get_proposal_profile,
    ),
)
