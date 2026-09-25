"""The typed write and entity contracts: § A13's bounds, as models a caller is
validated against before a handler runs.

**What is checked here and what is not.** This file owns *shape*: the closed kind
vocabulary, the trimmed title, the byte-bounded body, the per-write ceilings on refs
and mentions, the discriminated mention union, and the entity-list bounds. Everything
that needs the database or the caller's identity — whether a ref resolves, whether the
audience is inside this caller's ceiling, whether the purposes survive a derivation's
intersection — is the handler's, because a model cannot see a workspace.

The split matters for the refusal a caller gets. A malformed reference is
``input_invalid`` and is answered here; a well-formed *memory* reference handed to
``remember`` is ``use_derive`` and is answered by the handler, because the model has no
way to tell that apart from any other canonical string and collapsing the two would
hide a real design instruction behind a syntax error.

``extra = "forbid"`` on every model is § A13's rule, and it is what makes an unknown
key ``input_invalid`` at the dispatcher rather than an argument silently ignored.
"""

from datetime import datetime
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator
from rheo_contracts import ContextPurpose, RecordRef, RecordRefMalformed
from rheo_contracts.source_units import MemoryKind

from rheo_recallatron.configuration import (
    BODY_MAX_BYTES,
    DERIVE_SOURCES_MAX,
    DERIVE_SOURCES_MIN,
    ENTITY_LIST_LIMIT_DEFAULT,
    ENTITY_LIST_LIMIT_MAX,
    ENTITY_LIST_LIMIT_MIN,
    ENTITY_NAME_MAX_LENGTH,
    ENTITY_NAME_MIN_LENGTH,
    MAX_MENTIONS_PER_WRITE,
    MAX_REFS_PER_WRITE,
    MENTION_ROLE_MAX_LENGTH,
    MINIMUM_REVISION,
    TITLE_MAX_LENGTH,
    TITLE_MIN_LENGTH,
)
from rheo_recallatron.references import is_memory_ref
from rheo_recallatron.storage import tables as t

EntityKind = Literal["person", "organization", "project", "topic", "place", "thing"]
"""§ A3's closed entity vocabulary. Held against the DDL by
:func:`_entity_kinds_match_the_database`'s assertion below rather than by a comment:
the check constraint and this ``Literal`` are two spellings of one list."""

AudienceChoice = Literal["workspace", "member"]
"""What a writer may *ask* for. There is no audience **id** field anywhere in this
file, and that absence is the rule § A13 words as "normal writes cannot supply another
member's audience id": a member audience is always the caller's own account, resolved
from the verified principal, so there is no channel through which another member's id
could arrive."""


if set(get_args(EntityKind)) != set(t.ENTITY_KINDS):
    # At import, and raised rather than asserted so ``python -O`` cannot skip it. The
    # ``Literal`` is what a caller is validated against and the check constraint is
    # what the database enforces; a divergence would mean one of them silently
    # admitting a kind the other refuses, which only shows up as a failed INSERT.
    raise RuntimeError(
        "the declared entity kinds and the database's own vocabulary disagree: "
        f"{sorted(get_args(EntityKind))} vs {sorted(t.ENTITY_KINDS)}"
    )


class Strict(BaseModel):
    """Extra fields forbidden, frozen. Every model in this module is one."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _trimmed_title(value: str) -> str:
    """§ A13's "title 1-200 **trimmed** characters", applied rather than asserted.

    The trimmed value is what is stored, so a title differing from another only in
    surrounding whitespace is the same title rather than a second one that sorts
    beside it.
    """
    trimmed = value.strip()
    if not TITLE_MIN_LENGTH <= len(trimmed) <= TITLE_MAX_LENGTH:
        raise ValueError(
            f"title must be {TITLE_MIN_LENGTH}-{TITLE_MAX_LENGTH} trimmed characters"
        )
    return trimmed


def _bounded_body(value: str) -> str:
    """Nonblank, and at most § A13's byte bound.

    Bytes rather than characters: the bound exists to cap what is stored and
    transported, and a character bound would admit a body four times the size on
    multi-byte text. The body itself is **not** trimmed — leading indentation can be
    part of what somebody wrote down — only required to be more than whitespace.
    """
    if not value.strip():
        raise ValueError("body must not be blank")
    if len(value.encode("utf-8")) > BODY_MAX_BYTES:
        raise ValueError(f"body must be at most {BODY_MAX_BYTES} UTF-8 bytes")
    return value


def _aware(value: datetime | None) -> datetime | None:
    """§ A13: naive timestamps refuse.

    A naive instant is not a time, it is a time in an unstated zone, and retention and
    ordering both compare against an aware ``now``.
    """
    if value is not None and value.tzinfo is None:
        raise ValueError("a timestamp must carry a timezone")
    return value


Title = Annotated[str, Field(min_length=TITLE_MIN_LENGTH)]
Body = Annotated[str, Field(min_length=1)]
Role = Annotated[str, Field(min_length=1, max_length=MENTION_ROLE_MAX_LENGTH)]
EntityName = Annotated[
    str, Field(min_length=ENTITY_NAME_MIN_LENGTH, max_length=ENTITY_NAME_MAX_LENGTH)
]


class MentionCreate(Strict):
    """``create(kind, name, backing_ref?)``: always a fresh entity UUID.

    Never a name-based upsert, including for a name that already exists in this
    workspace. Two entities with the same normalized name are two entities, because
    the alternative — matching on name — would answer "does this name exist here"
    across an audience boundary the caller may not cross.
    """

    variant: Literal["create"]
    kind: EntityKind
    name: EntityName
    backing_ref: str | None = None
    role: Role | None = None


class MentionSelect(Strict):
    """``select(entity_ref)``: an entity this caller can already reach.

    Reachable means an eligible mention on a memory this caller may read, or an
    independently readable backing ref. No route is the same ``not_found`` as an
    unknown id — no hidden label, ref, collision or count comes back.
    """

    variant: Literal["select"]
    entity_ref: str
    role: Role | None = None


MentionInput = Annotated[MentionCreate | MentionSelect, Field(discriminator="variant")]
"""The discriminated union § A5 ratifies. ``variant`` carries no default, so a mention
that names neither route is ``input_invalid`` rather than defaulting into one."""


class _Write(Strict):
    """What ``remember`` and ``derive`` have in common.

    ``origin``, ``recorded_by`` and ``recorded_at`` are absent by construction and not
    by validation: they are server-owned, and a field a caller cannot name is a field
    no refusal has to exist for.
    """

    kind: MemoryKind
    title: Title
    body: Body
    audience: AudienceChoice | None = None
    purposes: frozenset[ContextPurpose] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    occurred_at: datetime | None = None
    about_refs: tuple[str, ...] = Field(default=(), max_length=MAX_REFS_PER_WRITE)
    mentions: tuple[MentionInput, ...] = Field(
        default=(), max_length=MAX_MENTIONS_PER_WRITE
    )

    @field_validator("title")
    @classmethod
    def _title_is_trimmed_and_bounded(cls, value: str) -> str:
        return _trimmed_title(value)

    @field_validator("body")
    @classmethod
    def _body_is_nonblank_and_bounded(cls, value: str) -> str:
        return _bounded_body(value)

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_is_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value)


class RememberInput(_Write):
    """A memory somebody stated, with its own provenance.

    ``provenance_refs`` are ``derived_from`` relations to **non-memory** records: a
    memory reference in either relation is ``use_derive``.
    """

    provenance_refs: tuple[str, ...] = Field(default=(), max_length=MAX_REFS_PER_WRITE)


class DeriveInput(_Write):
    """A memory computed from 1-64 distinct, currently authorized sources.

    ``sources`` is bounded at both ends: zero sources is not a derivation, and the
    upper bound is § A13's. Distinctness is checked in the handler against the
    *canonical* form, so two spellings of one reference are one source rather than a
    duplicate that inflates the count.
    """

    sources: tuple[str, ...] = Field(
        min_length=DERIVE_SOURCES_MIN, max_length=DERIVE_SOURCES_MAX
    )


class _Lifecycle(Strict):
    """What ``correct`` and ``supersede`` have in common: a target and a CAS.

    ``ref`` is a :class:`~rheo_contracts.refs.RecordRef` rather than the ``str`` every
    other reference field in this file carries, and the difference is load-bearing
    rather than stylistic. ``AuditSpec(subject_field="ref")`` names an input field
    **carrying a ``RecordRef``**, and ``dispatch``'s ``_subject_ref`` records a
    subject only for a field that is one; a ``str`` here would write a null
    ``subject_ref`` on the audit row of the two operations in this module that
    genuinely act on a record, indistinguishable from the ordinary null of an
    operation that acts on none. The validator below is what keeps the **wire** form
    the same canonical string a caller sends everywhere else.

    ``expected_revision`` is § A7's compare-and-set and is required at or above 1:
    the column's own check constraint starts at 1, so a caller that supplied 0 would
    be naming a revision no row can hold.
    """

    ref: RecordRef
    expected_revision: int = Field(ge=MINIMUM_REVISION)

    @field_validator("ref", mode="before")
    @classmethod
    def _accept_the_canonical_string(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            parsed = RecordRef.parse(value)
        except (RecordRefMalformed, TypeError):
            raise ValueError("a reference must be canonical") from None
        if not is_memory_ref(parsed):
            raise ValueError("the target must be a memory reference")
        return parsed


class CorrectInput(_Lifecycle):
    """``correct(ref, expected_revision, title, body, confidence?)``.

    A correction **restates** the record rather than patching it: ``title`` and
    ``body`` are required and ``confidence`` defaults to null, so the three columns
    § A7 lets a correction touch are written from what the caller sent and from
    nothing else. There is no partial form, because a patch whose omitted field means
    "leave it" and whose null means "clear it" cannot express both over a nullable
    column without a third sentinel nobody would remember.

    No audience, purposes, references or mentions: a correction fixes what a memory
    says, and changing who may read it or what it was derived from is a different
    act with a different closure.
    """

    title: Title
    body: Body
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("title")
    @classmethod
    def _title_is_trimmed_and_bounded(cls, value: str) -> str:
        return _trimmed_title(value)

    @field_validator("body")
    @classmethod
    def _body_is_nonblank_and_bounded(cls, value: str) -> str:
        return _bounded_body(value)


class SupersedeInput(_Lifecycle):
    """``supersede(ref, expected_revision, kind, title, body, confidence?,
    occurred_at?)``.

    The replacement's *content* is the caller's; its **audience, purposes and
    restrictions are not**. § A5 gives the replacement source-meet purposes and
    audience and the predecessor's copied restrictions, so there is no audience,
    purposes, ``about_refs`` or ``provenance_refs`` field here — and no field for
    the marked ancestry link either, which is § A5's "the marker is not accepted in
    caller input" expressed as an absence rather than as a refusal.
    """

    kind: MemoryKind
    title: Title
    body: Body
    confidence: float | None = Field(default=None, ge=0, le=1)
    occurred_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def _title_is_trimmed_and_bounded(cls, value: str) -> str:
        return _trimmed_title(value)

    @field_validator("body")
    @classmethod
    def _body_is_nonblank_and_bounded(cls, value: str) -> str:
        return _bounded_body(value)

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_is_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value)


class EntityHead(Strict):
    """One entity as a writer sees it on the memory it just created."""

    ref: str
    kind: str
    name: str
    backing_ref: str | None
    role: str | None


class ArmCounts(Strict):
    """How many of the **returned** items each retrieval arm ranked. A fused item both
    arms found counts once in each; an arm that did not run, or found none of the
    returned items, counts zero.

    Counted after the permission walk, over the items only, so a memory the caller
    may not read is never itself counted, and an empty answer always counts zero. One
    residual: the hybrid dense arm is cut to ``k`` times the over-fetch multiplier
    before the walk, so enough hidden rows nearer the query can push a readable item
    out of that arm's list, and it then counts under lexical alone."""

    lexical: int
    dense: int


class RecallProvenance(Strict):
    """Which strategy answered a recall, and what its arms contributed.

    ``strategy`` is the dispatched strategy's name and equals every item's
    ``strategy``. Read it before comparing scores: ``score`` is on the answering
    strategy's own scale and is comparable only among items in one response.
    ``dense_available`` is whether a dense arm could contribute to this answer at all.
    """

    strategy: str
    arms: ArmCounts
    dense_available: bool


class MemoryWritten(Strict):
    """What a successful write returns: the reference, and what was actually stored.

    The stored audience and purposes travel back because both can be *narrower* than
    what was asked for — a derive narrows to its sources' meet and intersection — and a
    caller that was not told would believe it had stored the wider set.
    """

    ref: str
    kind: str
    audience: str
    purposes: tuple[str, ...]
    recorded_at: datetime
    revision: int
    mentions: tuple[EntityHead, ...]


class MemoryCorrected(Strict):
    """What a correction returns: the record it fixed, at its new revision.

    **No count of what the closure invalidated, and no list of it.** The affected
    rows are other memories, whose existence is exactly what a caller who may read
    this one is not entitled to learn; a "3 derivatives invalidated" field would
    disclose the shape of somebody else's graph on every correction, and no single
    response would look wrong.
    """

    ref: str
    kind: str
    revision: int
    corrected_at: datetime


class MemorySuperseded(Strict):
    """What a supersession returns: the replacement, and where it came from.

    ``replacement`` is the same :class:`MemoryWritten` every other write path
    answers with, nested rather than restated, so a field added to a write's answer
    cannot be forgotten here. ``predecessor_revision`` is the predecessor's **new**
    revision, which is what a caller holding the old one needs in order to know its
    copy has moved.

    Closure size is absent here for the reason :class:`MemoryCorrected` gives.
    """

    replacement: MemoryWritten
    predecessor_ref: str
    predecessor_revision: int


class EntityItem(Strict):
    """One entity, with a count computed from this caller's eligible mentions only.

    ``mention_count`` is not a total. § A13 is explicit that entity output computes
    counts only from eligible results and exposes no hidden total: a count that
    included memories the caller may not read would report their existence.
    """

    ref: str
    kind: str
    name: str
    backing_ref: str | None
    mention_count: int


class EntityListInput(Strict):
    kind: EntityKind | None = None
    limit: int = Field(
        default=ENTITY_LIST_LIMIT_DEFAULT,
        ge=ENTITY_LIST_LIMIT_MIN,
        le=ENTITY_LIST_LIMIT_MAX,
    )


class EntityList(Strict):
    items: tuple[EntityItem, ...]


class EntityGetInput(Strict):
    entity_ref: str
