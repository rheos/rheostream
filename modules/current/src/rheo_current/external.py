"""The approved external destination: one source becomes one standalone item.

Core reaches this only through an approved external action's continuation, which
calls :data:`DESTINATION`'s ``execute`` with the held caller and a savepoint. Two
retry layers stay separate. Core's retained KEYED result replays the approved request
without calling here at all; when that row is absent or expired, the source link and
``uq_item_source`` still converge every create for one source onto one item.

This path never calls :func:`rheo_current.items.repeat_signal`: a retry must not
count an event, reopen, wake, raise priority or touch a project.
"""

from typing import Any, Final

from rheo_contracts import RecordRef, WorkspaceContext
from rheo_core.modules.manifest import DestinationMode, DestinationRegistration
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.storage.repositories import list_module_states
from sqlalchemy import insert, select

from rheo_current import common as h
from rheo_current import contracts as c
from rheo_current.references import MODULE_ID, ref
from rheo_current.storage import tables as t

OPERATION: Final = "current.item.create_external"
DESTINATION_REF: Final = "current"
SOURCE_KIND: Final = "handoff"
"""The ``source_kind`` every external item carries beside its ``source_ref``."""
SOURCE_TOMBSTONED: Final = "source_tombstoned"


def _refused(state: str, detail: str) -> Exception:
    """A typed permanent no-effect refusal, which core settles without a retry."""
    from rheo_core.operations.continuation import (
        ExternalDestinationError,
        ExternalFailureClass,
    )

    return ExternalDestinationError(
        ExternalFailureClass.PERMANENT_REFUSAL_NO_EFFECT, state, detail
    )


def _link(uow: UnitOfWork, source_ref: str) -> dict[str, Any] | None:
    found = (
        uow.connection.execute(
            select(t.source_item_link)
            .where(t.source_item_link.c.source_ref == source_ref)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    return None if found is None else dict(found)


def _item_exists(uow: UnitOfWork, item_ref: str) -> bool:
    identifier = RecordRef.parse(item_ref).id
    return (
        uow.connection.execute(
            select(t.item.c.id).where(t.item.c.id == identifier)
        ).first()
        is not None
    )


def create(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.ExternalItemCreate
) -> c.ExternalItemCreated:
    """Insert or return the one item for ``data.source_ref``, with its source link.

    The workspace lifecycle lock orders concurrent creates for one source, so the
    second sees the first's link. A link whose item was deleted is a tombstone and
    refuses: an intentionally deleted item never comes back from the same source.
    """
    h.lock(uow)
    link = _link(uow, data.source_ref)
    if link is not None:
        if link["item_state"] == "deleted" or not _item_exists(uow, link["item_ref"]):
            raise _refused(
                SOURCE_TOMBSTONED,
                "this source's item was deleted; its tombstone refuses recreation",
            )
        return c.ExternalItemCreated(item_ref=link["item_ref"])
    moment = h.now()
    existing = (
        uow.connection.execute(
            select(t.item)
            .where(
                t.item.c.source_kind == SOURCE_KIND,
                t.item.c.source_ref == data.source_ref,
            )
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        # The source index already holds an item this path did not link (a direct
        # create with the same pair): converge on it rather than make a second one.
        identifier = existing["id"]
        project = existing["project_id"]
        item_ref = ref("item", identifier)
        uow.connection.execute(
            insert(t.source_item_link).values(
                source_ref=data.source_ref,
                item_ref=item_ref,
                revision=1,
                item_state="available",
                project_ref=None if project is None else ref("project", project),
                last_project_ref=None,
                project_state="never_assigned" if project is None else "linked",
                changed_at=moment,
            )
        )
        return c.ExternalItemCreated(item_ref=item_ref)
    try:
        h.usable_area(uow, data.area)
    except OperationRefused as refused:
        raise _refused(refused.state, refused.detail or refused.state) from None
    identifier = uuid7()
    record = {
        "id": identifier,
        "title": data.title,
        "body": data.body,
        "status": "open",
        "priority": data.priority,
        "area": data.area,
        "project_id": None,
        "due_on": data.due_on,
        "snoozed_until": None,
        "provenance": "handoff",
        "source_kind": SOURCE_KIND,
        "source_ref": data.source_ref,
        "event_count": 1,
        "revision": 1,
        "created_at": moment,
        "updated_at": moment,
        "closed_at": None,
    }
    uow.connection.execute(insert(t.item).values(**record))
    item_ref = ref("item", identifier)
    uow.connection.execute(
        insert(t.source_item_link).values(
            source_ref=data.source_ref,
            item_ref=item_ref,
            revision=1,
            item_state="available",
            project_ref=None,
            last_project_ref=None,
            project_state="never_assigned",
            changed_at=moment,
        )
    )
    h.publish_item_event(ctx, uow, "created", record)
    return c.ExternalItemCreated(item_ref=item_ref)


def _installed() -> bool:
    from rheo_core.modules.loader import loaded_manifests

    return MODULE_ID in loaded_manifests()


def _enabled(ctx: WorkspaceContext) -> bool:
    return MODULE_ID in ctx.enabled_modules


def _ready(ctx: WorkspaceContext, uow: UnitOfWork) -> bool:
    """Enabled in this workspace's own rows and matching the loaded code."""
    from rheo_core.modules.readiness import readiness_problem

    state = next(
        (s for s in list_module_states(uow.connection) if s.module_id == MODULE_ID),
        None,
    )
    return (
        state is not None
        and state.state == "enabled"
        and readiness_problem(uow.connection, state) is None
    )


def _execute(
    ctx: WorkspaceContext, uow: UnitOfWork, data: Any
) -> c.ExternalItemCreated:
    if not isinstance(data, c.ExternalItemCreate):
        raise _refused(
            "input_invalid", "the destination accepts one ExternalItemCreate"
        )
    return create(ctx, uow, data)


DESTINATION: Final = DestinationRegistration(
    destination_ref=DESTINATION_REF,
    operation_name=OPERATION,
    mode=DestinationMode.IN_PROCESS,
    is_installed=_installed,
    is_enabled=_enabled,
    is_ready=_ready,
    # In process, on this workspace's own database: there is no credential to check.
    credentials_available=None,
    execute=_execute,
    reconcile=None,
)
