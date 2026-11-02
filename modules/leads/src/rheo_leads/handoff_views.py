"""Viewer-safe Current presentation for one authorized Leads handoff.

This module imports no Current implementation and reads no Current table. It accepts
only a Leads handoff reference, authorizes that handoff and its opportunity first,
then calls three literal registered Current READs with the original viewer context.
"""

from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ValidationError
from rheo_contracts import RecordRef, RecordRefMalformed, SafetyClass, WorkspaceContext
from rheo_core.operations.refusals import (
    OPERATION_NOT_PERMITTED,
    ROLE_NOT_PERMITTED,
    OperationRefused,
)
from rheo_core.operations.registry import REGISTRY
from rheo_core.operations.transaction import call_in_transaction
from rheo_core.refs.resolver import NOT_FOUND, UnitOfWork

from rheo_leads import current_links
from rheo_leads import pipeline_common as h
from rheo_leads import pipeline_contracts as c
from rheo_leads.references import ref
from rheo_leads.storage import pipeline as p

LINK_READ: Final = "current.item.link_state"
ITEM_READ: Final = "current.item.get"
PROJECT_READ: Final = "current.project.get"
CURRENT_READS: Final = (LINK_READ, ITEM_READ, PROJECT_READ)
DENIED: Final = frozenset({OPERATION_NOT_PERMITTED, ROLE_NOT_PERMITTED})

ITEM_MESSAGES: Final = {
    "deleted": "Current item was deleted.",
    "denied": "Current item access is denied.",
    "unavailable": "Current item is unavailable.",
}
PROJECT_MESSAGES: Final = {
    "never_assigned": "No Current project was assigned.",
    "cleared": "The Current project assignment was cleared.",
    "deleted": "The Current project was deleted.",
    "denied": "Current project access is denied.",
    "unavailable": "Current project is unavailable.",
}


@dataclass(frozen=True, slots=True)
class ReadResult:
    state: Literal["ok", "denied", "not_found", "unavailable"]
    value: BaseModel | None = None


def _read(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    name: str,
    payload: dict[str, object],
) -> ReadResult:
    """One named READ in a savepoint, retaining ``ctx`` without substitution."""
    registered = REGISTRY.lookup(name)
    if (
        registered is None
        or registered.declaration.safety_class is not SafetyClass.READ
    ):
        return ReadResult("unavailable")
    try:
        with uow.connection.begin_nested():
            answer = call_in_transaction(ctx, uow, name, payload)
    except OperationRefused as refused:
        if refused.state in DENIED:
            return ReadResult("denied")
        if refused.state == NOT_FOUND:
            return ReadResult("not_found")
        return ReadResult("unavailable")
    except Exception:
        # The savepoint contains an expected provider/handler read failure. If the
        # outer transaction itself is broken, its eventual commit still fails and
        # the whole operation is unavailable rather than returning partial success.
        return ReadResult("unavailable")
    return ReadResult("ok", answer)


def _history(record: dict[str, Any]) -> c.CurrentViewHistory:
    return c.CurrentViewHistory(
        ref=ref("handoff", record["id"]),
        opportunity_ref=ref("opportunity", record["opportunity_id"]),
        purpose=record["purpose"],
        state=record["state"],
        attempt_count=record["attempt_count"],
        requested_at=record["requested_at"],
        resolved_at=record["resolved_at"],
    )


def _item_state(
    state: Literal["deleted", "denied", "unavailable"],
) -> c.CurrentItemState:
    return c.CurrentItemState(state=state, message=ITEM_MESSAGES[state])


def _project_state(
    state: Literal["never_assigned", "cleared", "deleted", "denied", "unavailable"],
) -> c.CurrentProjectState:
    return c.CurrentProjectState(state=state, message=PROJECT_MESSAGES[state])


def _allowlisted(model: BaseModel, output: type[BaseModel]) -> BaseModel:
    dumped = model.model_dump(mode="python")
    return output.model_validate(
        {field: dumped[field] for field in output.model_fields}
    )


def _is_current_ref(value: str, record_type: str) -> bool:
    try:
        parsed = RecordRef.parse(value)
    except RecordRefMalformed:
        return False
    return parsed.module == "current" and parsed.record_type == record_type


def _live_item(
    read: ReadResult, expected_ref: str
) -> c.CurrentItemLive | c.CurrentItemState:
    if read.state == "denied":
        return _item_state("denied")
    if read.state == "not_found":
        return _item_state("deleted")
    if read.state != "ok" or read.value is None:
        return _item_state("unavailable")
    dumped = read.value.model_dump(mode="python")
    if dumped.get("ref") != expected_ref:
        return _item_state("unavailable")
    try:
        content = _allowlisted(read.value, c.CurrentItemContent)
    except (KeyError, ValidationError):
        return _item_state("unavailable")
    assert isinstance(content, c.CurrentItemContent)
    return c.CurrentItemLive(ref=expected_ref, content=content)


def _live_project(
    read: ReadResult, expected_ref: str
) -> c.CurrentProjectLive | c.CurrentProjectState:
    if read.state == "denied":
        return _project_state("denied")
    if read.state == "not_found":
        return _project_state("deleted")
    if read.state != "ok" or read.value is None:
        return _project_state("unavailable")
    dumped = read.value.model_dump(mode="python")
    if dumped.get("ref") != expected_ref:
        return _project_state("unavailable")
    try:
        content = _allowlisted(read.value, c.CurrentProjectContent)
    except (KeyError, ValidationError):
        return _project_state("unavailable")
    assert isinstance(content, c.CurrentProjectContent)
    return c.CurrentProjectLive(ref=expected_ref, content=content)


def current_view(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.CurrentViewInput
) -> c.CurrentView:
    """Aggregate Current lifecycle and content without trusting Leads' projection."""
    handoff = h.row(uow, p.handoff, data.handoff_ref.id)
    # The Leads subject is authorized before any Current operation is reached. In
    # this release its authorization is module/role plus existence; reading the
    # opportunity makes the subject relationship explicit and refuses an orphan.
    h.row(uow, p.opportunity, handoff["opportunity_id"])
    history = _history(handoff)
    source_ref = data.handoff_ref.format()

    link_read = _read(ctx, uow, LINK_READ, {"source_ref": source_ref})
    if link_read.state != "ok" or link_read.value is None:
        state: Literal["denied", "unavailable"] = (
            "denied" if link_read.state == "denied" else "unavailable"
        )
        return c.CurrentView(
            history=history,
            item=_item_state(state),
            project=_project_state(state),
        )
    try:
        link = current_links.CurrentLink.model_validate(
            link_read.value.model_dump(mode="json")
        )
    except ValidationError:
        return c.CurrentView(
            history=history,
            item=_item_state("unavailable"),
            project=_project_state("unavailable"),
        )
    if (
        link.source_ref != source_ref
        or not _is_current_ref(link.item_ref, "item")
        or (
            link.project_ref is not None
            and not _is_current_ref(link.project_ref, "project")
        )
    ):
        return c.CurrentView(
            history=history,
            item=_item_state("unavailable"),
            project=_project_state("unavailable"),
        )

    if link.item_state == "deleted":
        item: c.CurrentItemLive | c.CurrentItemState = _item_state("deleted")
    else:
        item = _live_item(
            _read(ctx, uow, ITEM_READ, {"ref": link.item_ref}), link.item_ref
        )

    if link.project_state == "linked":
        assert link.project_ref is not None  # CurrentLink's cross-field validator.
        project: c.CurrentProjectLive | c.CurrentProjectState = _live_project(
            _read(ctx, uow, PROJECT_READ, {"ref": link.project_ref}),
            link.project_ref,
        )
    else:
        project = _project_state(link.project_state)
    return c.CurrentView(history=history, item=item, project=project)
