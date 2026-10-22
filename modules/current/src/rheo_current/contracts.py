"""Bounded wire shapes for items, projects, areas and the summary."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from rheo_current.references import ItemRef, ProjectRef

Status = Literal["open", "waiting", "snoozed", "done"]
Priority = Literal["critical", "high", "normal", "low"]
Provenance = Literal["manual", "agent", "email", "alert", "handoff"]
ProjectStatus = Literal["active", "paused", "done"]
CreateAction = Literal["created", "grouped", "reopened", "woke"]
AreaKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
Title = Annotated[str, Field(min_length=1, max_length=500)]
Body = Annotated[str, Field(max_length=20000)]
# One definition for create and close by source, so the two bounds cannot drift.
SourceKind = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
SourceRef = Annotated[str, Field(min_length=1, max_length=512)]


def _calendar_date(value: object) -> date:
    if type(value) is date:
        return value
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("use a calendar date in YYYY-MM-DD format")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("use a calendar date in YYYY-MM-DD format")
    return parsed


CalendarDate = Annotated[date, BeforeValidator(_calendar_date)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Empty(Strict):
    pass


# --- items -----------------------------------------------------------------------


class ItemInput(Strict):
    ref: ItemRef


class ItemCreate(Strict):
    title: Title
    body: Body = ""
    priority: Priority = "normal"
    area: AreaKey | None = None
    project_ref: ProjectRef | None = None
    due_on: CalendarDate | None = None
    provenance: Provenance = "manual"
    source_kind: SourceKind | None = None
    source_ref: SourceRef | None = None
    fingerprint: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def _source_pair(self) -> "ItemCreate":
        if (self.source_kind is None) != (self.source_ref is None):
            raise ValueError("source_kind and source_ref go together")
        if self.fingerprint is not None and self.source_ref is None:
            raise ValueError("fingerprint needs source_ref")
        return self


class ItemCloseBySource(Strict):
    source_kind: SourceKind
    source_ref: SourceRef
    note: str | None = Field(default=None, min_length=1, max_length=2000)


class ItemClosed(Strict):
    closed: bool
    ref: str | None
    """The item holding the pair, closed or not; null when no item holds it, and
    always null for a caller that cannot read items."""


def _no_overlap(model: BaseModel, clear: list[str]) -> None:
    if len(set(clear)) != len(clear):
        raise ValueError("clear names a field twice")
    if any(getattr(model, name) is not None for name in clear):
        raise ValueError("a field cannot be both set and cleared")


class ItemUpdate(ItemInput):
    """Null or absent leaves a field as it is; ``clear`` empties an optional one.

    Absence cannot carry meaning here: the MCP tool facade forwards every field,
    defaults included, so an explicit list is the only reliable way to clear.
    """

    revision: int = Field(ge=1)
    title: Title | None = None
    body: Body | None = None
    priority: Priority | None = None
    area: AreaKey | None = None
    project_ref: ProjectRef | None = None
    due_on: CalendarDate | None = None
    clear: list[Literal["area", "project_ref", "due_on"]] = Field(
        default_factory=list, max_length=3
    )

    @model_validator(mode="after")
    def _clear(self) -> "ItemUpdate":
        _no_overlap(self, list(self.clear))
        return self


class ItemStatusInput(ItemInput):
    revision: int = Field(ge=1)
    status: Status
    snoozed_until: AwareDatetime | None = None


class CommentInput(ItemInput):
    body: Annotated[str, Field(min_length=1, max_length=20000)]


class DependencyInput(Strict):
    item_ref: ItemRef
    blocker_ref: ItemRef


class ItemList(Strict):
    status: Literal["active", "open", "waiting", "snoozed", "done", "all"] = "active"
    area: AreaKey | None = None
    project_ref: ProjectRef | None = None
    priority: Priority | None = None
    due_before: CalendarDate | None = None
    overdue: bool | None = None
    woke: bool | None = None
    blocked: bool | None = None
    query: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100000)


class Comment(Strict):
    id: UUID
    kind: str
    body: str
    author_kind: str
    author_id: UUID | None
    created_at: AwareDatetime


class Blocker(Strict):
    ref: str
    title: str
    status: str


class ProjectSummary(Strict):
    ref: str
    title: str
    status: str


class ItemSummary(Strict):
    ref: str
    title: str
    status: str
    priority: str
    area: str | None
    project_ref: str | None
    due_on: date | None
    snoozed_until: AwareDatetime | None
    woke: bool
    overdue: bool
    blocked: bool
    event_count: int
    revision: int
    updated_at: AwareDatetime


class ItemDetail(ItemSummary):
    body: str
    provenance: str
    source_kind: str | None
    source_ref: str | None
    created_at: AwareDatetime
    closed_at: AwareDatetime | None
    project: ProjectSummary | None
    blockers: list[Blocker]
    blocking: list[Blocker]
    comments: list[Comment]


class ItemCreated(Strict):
    """What a create did. A caller that can read items gets ``{action, item}``; one
    that cannot (a writer token) gets ``{action, ref}``, so creating with a known
    source pair is never a way to read the item. The absent key is left out of the
    dump, not sent as null, which keeps the reading caller's result as it was."""

    action: CreateAction
    item: ItemDetail | None = Field(default=None, exclude_if=lambda v: v is None)
    ref: str | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def _item_or_ref(self) -> "ItemCreated":
        if (self.item is None) == (self.ref is None):
            raise ValueError("a create result carries either the item or its ref")
        return self


class ItemPage(Strict):
    items: list[ItemSummary]
    next_offset: int | None


# --- projects --------------------------------------------------------------------


class ProjectInput(Strict):
    ref: ProjectRef


class ProjectCreate(Strict):
    title: Title
    description: Body = ""
    status: ProjectStatus = "active"
    area: AreaKey | None = None
    due_on: CalendarDate | None = None


class ProjectUpdate(ProjectInput):
    """Null or absent leaves a field as it is; ``clear`` empties an optional one."""

    revision: int = Field(ge=1)
    title: Title | None = None
    description: Body | None = None
    status: ProjectStatus | None = None
    area: AreaKey | None = None
    due_on: CalendarDate | None = None
    clear: list[Literal["area", "due_on"]] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def _clear(self) -> "ProjectUpdate":
        _no_overlap(self, list(self.clear))
        return self


class ProjectList(Strict):
    status: ProjectStatus | None = None
    area: AreaKey | None = None


class ProjectOutput(Strict):
    ref: str
    title: str
    description: str
    status: str
    area: str | None
    due_on: date | None
    revision: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    item_counts: dict[str, int]


class ProjectPage(Strict):
    items: list[ProjectOutput]


# --- areas -----------------------------------------------------------------------


class AreaList(Strict):
    include_retired: bool = False


class AreaCreate(Strict):
    key: AreaKey
    label: Annotated[str, Field(min_length=1, max_length=80)]


class AreaUpdate(Strict):
    key: AreaKey
    label: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    retired: bool | None = None


class Area(Strict):
    key: str
    label: str
    position: int
    retired: bool


class AreaPage(Strict):
    items: list[Area]


# --- summary, deletion, events ----------------------------------------------------


class AreaCount(Strict):
    area: str | None
    active: int


class Summary(Strict):
    by_status: dict[str, int]
    active: int
    woke: int
    overdue: int
    blocked: int
    by_area: list[AreaCount]


class DeleteItemInput(Strict):
    ref: ItemRef


class DeleteProjectInput(Strict):
    ref: ProjectRef


class ItemEvent(Strict):
    ref: str
    revision: int
