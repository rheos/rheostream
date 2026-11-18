"""Current's lasting source link: lifecycle, public read, change event and restore.

Synthetic, local-only data throughout. Handoff items are made by Current's registered
destination (the approved path itself is proven in ``test_current_external.py``);
every later change goes through the real dispatcher: ``current.item.update`` for
assign, move and clear, and ``core.record.delete`` with its approval for deletion.
"""

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from harness.registry import add_member
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic_core import to_jsonable_python
from rheo_contracts import ActorKind, RecordRef, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.factories import context_for_event_consumer, context_from_token
from rheo_core.events import ConsumerRegistry
from rheo_core.modules import DESTINATIONS
from rheo_core.operations import REGISTRY, dispatch, register_core_operations
from rheo_core.operations.continuation import (
    EXTERNAL_ACTION_EXECUTE,
    ExternalDestinationError,
    run_external_continuation,
)
from rheo_core.operations.core_ops import TOKEN_ISSUE
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.operations.idempotency_tables import external_action
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import register_resolver
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_tables import outbox_event
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_current.contracts import ExternalItemCreate, SourceItemLink
from rheo_current.export import export_records, import_records
from rheo_current.resolvers import resolve_record
from rheo_current.storage import tables as t
from sqlalchemy import select, update

pytestmark = pytest.mark.postgres

CREATE = "current.item.create_external"
LINK_STATE = "current.item.link_state"
CHANGED = "current.source_item_link.changed"
SOURCE = "leads.handoff:00000000-0000-7000-8000-0000000000a1"
OTHER = "leads.handoff:00000000-0000-7000-8000-0000000000a2"


def payload(source: str = SOURCE, key: str = "k#1") -> dict[str, object]:
    return {
        "destination_ref": "current",
        "source_ref": source,
        "title": "Synthetic handoff item",
        "body": "Public-safe fixture body",
        "priority": "normal",
        "idempotency_key": key,
    }


@dataclass
class Links:
    cluster: ClusterSession
    workspace: UUID
    owner: WorkspaceContext
    surfaces: LoadedSurfaces
    consumers: ConsumerRegistry

    def call(
        self, name: str, ctx: WorkspaceContext | None = None, **body: Any
    ) -> OperationOutcome:
        return dispatch(
            ctx or self.owner,
            name,
            body,
            registry=self.surfaces.operations,
            consumers=self.consumers,
        )

    def ok(self, name: str, **body: Any) -> Any:
        outcome = self.call(name, **body)
        assert outcome.ok, outcome
        return outcome.result

    def handoff(self, source: str = SOURCE) -> str:
        """One standalone item from Current's registered destination, committed."""
        found = DESTINATIONS.lookup("current")
        assert found is not None
        with open_unit_of_work(self.owner) as uow:
            made = found.registration.execute(
                self.owner,
                HandlerUnitOfWork(uow, consumers=self.consumers),
                ExternalItemCreate.model_validate(payload(source)),
            )
            uow.commit()
        return str(made.item_ref)  # type: ignore[attr-defined]

    def project(self, title: str) -> str:
        return str(self.ok("current.project.create", title=title).ref)

    def revision(self, item_ref: str) -> int:
        return int(self.ok("current.item.get", ref=item_ref).revision)

    def assign(self, item_ref: str, project_ref: str) -> Any:
        return self.ok(
            "current.item.update",
            ref=item_ref,
            revision=self.revision(item_ref),
            project_ref=project_ref,
        )

    def clear(self, item_ref: str) -> Any:
        return self.ok(
            "current.item.update",
            ref=item_ref,
            revision=self.revision(item_ref),
            clear=["project_ref"],
        )

    def delete(self, reference: str) -> None:
        held = self.call("core.record.delete", ref=reference)
        assert held.state == "approval_required", held
        done = self.call("core.approval.approve", approval_id=str(held.approval_id))
        assert done.ok and done.result.state != "refused", done

    def link(self, source: str = SOURCE) -> SourceItemLink:
        found = self.ok(LINK_STATE, source_ref=source)
        assert isinstance(found, SourceItemLink)
        return found

    def link_row(self, source: str = SOURCE) -> dict[str, Any]:
        with open_unit_of_work(self.owner) as uow:
            return dict(
                uow.connection.execute(
                    select(t.source_item_link).where(
                        t.source_item_link.c.source_ref == source
                    )
                )
                .mappings()
                .one()
            )

    def events(self) -> list[dict[str, Any]]:
        """Every link change event, in outbox order."""
        with open_unit_of_work(self.owner) as uow:
            return [
                dict(row)
                for row in uow.connection.execute(
                    select(
                        outbox_event.c.subject_ref,
                        outbox_event.c.subject_revision,
                        outbox_event.c.schema_version,
                        outbox_event.c.data,
                    )
                    .where(outbox_event.c.type == CHANGED)
                    .order_by(outbox_event.c.position)
                ).mappings()
            ]

    def token(self, ctx: WorkspaceContext, *operations: str) -> WorkspaceContext:
        """A context from a real ``mcp`` token whose set names exactly these."""
        issued = self.call(TOKEN_ISSUE, ctx, kind="mcp", operations=list(operations))
        assert issued.ok, issued
        token_ctx = context_from_token(str(issued.result.value), "mcp")
        assert isinstance(token_ctx, WorkspaceContext), token_ctx
        assert token_ctx.actor.kind is ActorKind.TOKEN
        assert token_ctx.operation_set == frozenset(operations)
        return token_ctx

    def member(self, name: str) -> WorkspaceContext:
        account = add_member(
            self.cluster.backend, self.workspace, Role.MEMBER, display_name=name
        )
        ctx = context_for_harness(self.workspace, account, Role.MEMBER)
        assert isinstance(ctx, WorkspaceContext)
        return ctx


def install(
    cluster: ClusterSession, workspace: UUID, account: UUID
) -> WorkspaceContext:
    operator = context_for_operator(workspace)
    assert isinstance(operator, WorkspaceContext)
    install_and_enable_module(cluster.backend, operator, workspace, "current")
    owner = context_for_harness(workspace, account, Role.OWNER)
    assert isinstance(owner, WorkspaceContext)
    return owner


@pytest.fixture
def links(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Links]:
    with loaded_probe_modules(monkeypatch, "current") as surfaces:
        register_core_operations()
        register_core_operations(surfaces.operations)
        monkeypatch.setattr(
            REGISTRY, "_operations", dict(surfaces.operations._operations)
        )
        for kind in ("item", "project"):
            register_resolver("current", kind, resolve_record, origin="current")
        owner = install(cluster, workspace, owner_account_id)
        yield Links(cluster, workspace, owner, surfaces, ConsumerRegistry())


def expected(link: SourceItemLink) -> dict[str, Any]:
    return link.model_dump(mode="json")


# --- lifecycle through the existing item edit path --------------------------------


def test_assign_move_clear_advance_one_revision_and_one_event_each(
    links: Links,
) -> None:
    h = links
    item_ref = h.handoff()
    first = h.link()
    assert (first.revision, first.project_state, first.project_ref) == (
        1,
        "never_assigned",
        None,
    )
    p, q = h.project("Synthetic P"), h.project("Synthetic Q")
    seen: list[dict[str, Any]] = []

    h.assign(item_ref, p)
    assigned = h.link()
    assert (assigned.revision, assigned.project_state) == (2, "linked")
    assert (assigned.project_ref, assigned.last_project_ref) == (p, None)
    seen.append(expected(assigned))

    # The same assignment again, and an edit that leaves the project alone, are
    # link no-ops: the item's own revision moves, the link's does not.
    before = h.revision(item_ref)
    h.assign(item_ref, p)
    h.ok(
        "current.item.update",
        ref=item_ref,
        revision=h.revision(item_ref),
        title="Renamed",
    )
    assert h.revision(item_ref) == before + 2
    assert h.link() == assigned

    h.assign(item_ref, q)
    moved = h.link()
    assert (moved.revision, moved.project_state) == (3, "linked")
    assert (moved.project_ref, moved.last_project_ref) == (q, p)
    seen.append(expected(moved))

    h.clear(item_ref)
    cleared = h.link()
    assert (cleared.revision, cleared.project_state) == (4, "cleared")
    assert (cleared.project_ref, cleared.last_project_ref) == (None, q)
    seen.append(expected(cleared))

    h.clear(item_ref)  # clearing an already-empty project changes nothing
    assert h.link() == cleared

    h.assign(item_ref, p)
    again = h.link()
    assert (again.revision, again.project_state) == (5, "linked")
    assert (again.project_ref, again.last_project_ref) == (p, q)
    seen.append(expected(again))

    events = h.events()
    # One event per change, in revision order, each carrying the full read shape.
    assert [e["data"] for e in events] == seen
    assert [e["subject_revision"] for e in events] == [2, 3, 4, 5]
    assert {e["subject_ref"] for e in events} == {item_ref}
    assert {e["schema_version"] for e in events} == {1}
    # The item reference never moves, whatever happens to the project.
    assert {e["data"]["item_ref"] for e in events} == {item_ref}
    assert h.link_row()["item_ref"] == item_ref


def test_an_ordinary_item_has_no_link_and_its_edits_publish_nothing(
    links: Links,
) -> None:
    h = links
    plain = h.ok("current.item.create", title="Ordinary").item.ref
    h.assign(plain, h.project("Synthetic P"))
    assert h.events() == []
    assert h.call(LINK_STATE, source_ref=SOURCE).state == "not_found"


# --- deletion ----------------------------------------------------------------------


def test_item_deletion_keeps_the_live_project_then_project_deletion_moves_it(
    links: Links,
) -> None:
    h = links
    item_ref = h.handoff()
    p = h.project("Synthetic P")
    h.assign(item_ref, p)
    h.delete(item_ref)
    gone = h.link()
    # Only the item's state changed: the project is still live and still read.
    assert (gone.revision, gone.item_state, gone.project_state) == (
        3,
        "deleted",
        "linked",
    )
    assert (gone.item_ref, gone.project_ref, gone.last_project_ref) == (
        item_ref,
        p,
        None,
    )
    assert h.call("current.item.get", ref=item_ref).state == "not_found"
    assert h.ok("current.project.get", ref=p).ref == p
    # The tombstone still refuses the same source.
    with pytest.raises(ExternalDestinationError, match="source_tombstoned"):
        h.handoff()

    h.delete(p)
    history = h.link()
    assert (history.revision, history.item_state, history.project_state) == (
        4,
        "deleted",
        "deleted",
    )
    assert (history.item_ref, history.project_ref, history.last_project_ref) == (
        item_ref,
        None,
        p,
    )
    assert [e["data"] for e in h.events()][-2:] == [expected(gone), expected(history)]


def test_project_deletion_under_a_live_item_records_it_and_a_later_assign_links(
    links: Links,
) -> None:
    h = links
    one, two = h.handoff(), h.handoff(OTHER)
    p, q = h.project("Synthetic P"), h.project("Synthetic Q")
    h.assign(one, p)
    h.assign(two, p)
    h.delete(p)
    for source, item_ref in ((SOURCE, one), (OTHER, two)):
        link = h.link(source)
        assert (link.item_state, link.project_state, link.revision) == (
            "available",
            "deleted",
            3,
        )
        assert (link.item_ref, link.project_ref, link.last_project_ref) == (
            item_ref,
            None,
            p,
        )
        assert h.ok("current.item.get", ref=item_ref).project_ref is None
    # One event per link the deletion touched, each with its link's final shape.
    last_two = h.events()[-2:]
    assert {e["subject_ref"] for e in last_two} == {one, two}
    assert {e["data"]["project_state"] for e in last_two} == {"deleted"}
    assert len(h.events()) == 4
    h.clear(one)  # nothing live to clear: a no-op
    assert h.link().revision == 3
    h.assign(one, q)
    relinked = h.link()
    assert (relinked.project_state, relinked.project_ref) == ("linked", q)
    assert (relinked.last_project_ref, relinked.revision) == (p, 4)


def test_concurrent_move_and_project_deletion_leave_one_ordered_history(
    links: Links,
) -> None:
    """The workspace lock orders a move against an approved project deletion; either
    order leaves a link whose revision counts exactly its published changes."""
    h = links
    item_ref = h.handoff()
    p, q = h.project("Synthetic P"), h.project("Synthetic Q")
    h.assign(item_ref, p)
    revision = h.revision(item_ref)
    held = h.call("core.record.delete", ref=p)
    assert held.state == "approval_required", held
    barrier = threading.Barrier(2, timeout=60)
    outcomes: dict[str, OperationOutcome] = {}
    failures: list[BaseException] = []

    def run(name: str, operation: str, body: dict[str, Any]) -> None:
        try:
            barrier.wait()
            outcomes[name] = h.call(operation, **body)
        except BaseException as exc:  # surfaced below, never swallowed
            failures.append(exc)

    threads = [
        threading.Thread(
            target=run,
            args=(
                "move",
                "current.item.update",
                {"ref": item_ref, "revision": revision, "project_ref": q},
            ),
        ),
        threading.Thread(
            target=run,
            args=(
                "delete",
                "core.approval.approve",
                {"approval_id": str(held.approval_id)},
            ),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    link = h.link()
    events = h.events()
    assert link.revision == len(events) + 1
    assert [e["subject_revision"] for e in events] == list(range(2, link.revision + 1))
    assert events[-1]["data"] == expected(link)
    if outcomes["move"].ok:
        # The move won: P had no linked item left when its deletion ran.
        assert (link.project_state, link.project_ref) == ("linked", q)
        assert link.last_project_ref == p
    else:
        # The deletion won and moved the item's revision, so the move was stale.
        assert outcomes["move"].state == "record_stale"
        assert (link.project_state, link.project_ref) == ("deleted", None)
        assert link.last_project_ref == p


# --- the public read and its independent authorization -----------------------------


def test_link_state_is_authorized_on_its_own_not_through_item_content(
    links: Links,
) -> None:
    h = links
    item_ref = h.handoff()
    h.delete(item_ref)
    member = h.member("member-reader")
    lifecycle_only = h.token(member, LINK_STATE)
    content_only = h.token(member, "current.item.get")
    # The lifecycle read answers for a deleted item, with no item-content permission.
    read = h.call(LINK_STATE, lifecycle_only, source_ref=SOURCE)
    assert read.ok, read
    assert read.result == h.link()
    assert read.result.item_state == "deleted"
    denied = h.call("current.item.get", lifecycle_only, ref=item_ref)
    assert denied.state == "operation_not_permitted", denied
    # Item-content permission is not lifecycle permission.
    refused = h.call(LINK_STATE, content_only, source_ref=SOURCE)
    assert refused.state == "operation_not_permitted", refused
    # The restricted service context a background reader would hold.
    with open_unit_of_work(h.owner) as uow:
        service = context_for_event_consumer(
            h.workspace,
            uow,
            request_id=uuid7(),
            operation_names=frozenset({LINK_STATE}),
        )
    assert isinstance(service, WorkspaceContext)
    assert h.call(LINK_STATE, service, source_ref=SOURCE).result == h.link()
    # Readiness is the caller's own workspace's: with Current off, nothing answers.
    disabled = h.owner.model_copy(update={"enabled_modules": frozenset()})
    assert h.call(LINK_STATE, disabled, source_ref=SOURCE).state == "module_disabled"
    assert h.call(LINK_STATE, source_ref=OTHER).state == "not_found"


def test_a_members_token_can_only_stage_the_external_create(links: Links) -> None:
    """A token holding the external create stages an approval and nothing else: no
    Current row appears, it cannot approve its own request, no token can carry the
    approval at all, and the worker runs nothing while the approval is undecided."""
    h = links
    member = h.member("member-stager")
    token = h.token(member, CREATE)
    staged = h.call(CREATE, token, **payload())
    assert staged.state == "approval_required", staged
    assert staged.approval_id is not None
    approve = h.call(
        "core.approval.approve", token, approval_id=str(staged.approval_id)
    )
    assert approve.state == "operation_not_permitted", approve
    refused = h.call(
        TOKEN_ISSUE, member, kind="mcp", operations=[CREATE, "core.approval.approve"]
    )
    assert not refused.ok, refused
    kinds = JobKindRegistry()
    kinds.register_continuation(EXTERNAL_ACTION_EXECUTE, run_external_continuation)
    visit_workspace(
        DueWorkspace(workspace_id=h.workspace, observed_due_at=None),
        kinds=kinds,
        consumers=h.consumers,
        backend=h.cluster.backend,
        owner="current-source-link-test",
        clock=lambda: datetime.now(UTC) + timedelta(seconds=1),
        jitter=None,
    )
    with open_unit_of_work(h.owner) as uow:
        assert list(uow.connection.execute(select(t.item))) == []
        assert list(uow.connection.execute(select(t.source_item_link))) == []
        [action] = list(uow.connection.execute(select(external_action)).mappings())
    assert action["state"] not in {"succeeded", "executing"}, dict(action)
    assert action["result_ref"] is None


# --- export and restore ------------------------------------------------------------


def _states(h: Links) -> dict[str, str]:
    """Five sources, one in each shape restore must keep:

    a1 available and linked; a2 deleted with its project surviving; a3 cleared;
    a4 deleted with its project deleted too; a5 never assigned.
    """
    sources = {
        name: f"leads.handoff:00000000-0000-7000-8000-0000000000{name}"
        for name in ("b1", "b2", "b3", "b4", "b5")
    }
    refs = {name: h.handoff(source) for name, source in sources.items()}
    p, r = h.project("Survives"), h.project("Removed")
    h.assign(refs["b1"], p)
    h.assign(refs["b2"], p)
    h.delete(refs["b2"])
    h.assign(refs["b3"], p)
    h.clear(refs["b3"])
    h.assign(refs["b4"], r)
    h.delete(refs["b4"])
    h.delete(r)
    return sources


def _export(ctx: WorkspaceContext) -> list[dict[str, Any]]:
    with open_unit_of_work(ctx) as uow:
        rows: list[dict[str, Any]] = to_jsonable_python(
            export_records(SimpleNamespace(connection=uow.connection))
        )
    return rows


def test_export_restore_round_trips_every_link_state_and_its_revision(
    links: Links, make_workspace: MakeWorkspace, owner_account_id: UUID
) -> None:
    h = links
    sources = _states(h)
    before = {name: h.link(source) for name, source in sources.items()}
    assert {
        name: (link.item_state, link.project_state) for name, link in before.items()
    } == {
        "b1": ("available", "linked"),
        "b2": ("deleted", "linked"),
        "b3": ("available", "cleared"),
        "b4": ("deleted", "deleted"),
        "b5": ("available", "never_assigned"),
    }
    rows = _export(h.owner)
    schema = json.loads(
        resources.files("rheo_current").joinpath("export.schema.json").read_text()
    )
    validator = Draft202012Validator(schema)
    for row in rows:
        validator.validate(row)
    assert sum(r["record_type"] == "current.source_item_link" for r in rows) == 5

    other = make_workspace()
    restored_ctx = install(h.cluster, other, owner_account_id)
    with open_unit_of_work(restored_ctx) as uow:
        import_records(uow, rows)()
        uow.commit()
    restored = Links(h.cluster, other, restored_ctx, h.surfaces, h.consumers)
    assert _export(restored_ctx) == rows
    for name, source in sources.items():
        assert restored.link(source) == before[name], name
    # The surviving project is still there for the deleted item's live link.
    assert restored.ok("current.project.get", ref=before["b2"].project_ref).ref == (
        before["b2"].project_ref
    )
    # The tombstones still refuse, and the lifecycle continues from the revision.
    for name in ("b2", "b4"):
        with pytest.raises(ExternalDestinationError, match="source_tombstoned"):
            restored.handoff(sources[name])
    restored.clear(before["b1"].item_ref)
    after = restored.link(sources["b1"])
    assert (after.revision, after.project_state) == (
        before["b1"].revision + 1,
        "cleared",
    )


def _corrupt(rows: list[dict[str, Any]], source: str, **change: Any) -> list[Any]:
    return [
        {**row, **change}
        if row["record_type"] == "current.source_item_link"
        and row["source_ref"] == source
        else row
        for row in rows
    ]


def test_restore_refuses_a_link_and_items_that_disagree(
    links: Links, make_workspace: MakeWorkspace, owner_account_id: UUID
) -> None:
    """Restore keeps a handoff item and its link together or takes neither."""
    h = links
    sources = _states(h)
    rows = _export(h.owner)
    linked = h.link(sources["b1"])
    deleted = h.link(sources["b2"])
    project_ids = {row["id"] for row in rows if row["record_type"] == "current.project"}
    unknown_project = f"current.project:{uuid7()}"
    assert RecordRef.parse(unknown_project).id not in project_ids
    broken: dict[str, list[Any]] = {
        # A handoff item whose link was dropped.
        "item without link": [
            r
            for r in rows
            if not (
                r["record_type"] == "current.source_item_link"
                and r["source_ref"] == sources["b1"]
            )
        ],
        # A live link whose item was dropped.
        "link without item": [
            r
            for r in rows
            if not (
                r["record_type"] == "current.item"
                and f"current.item:{r['id']}" == linked.item_ref
            )
        ],
        # A tombstone whose item is present again.
        "tombstone with its item back": [
            *rows,
            {
                **next(
                    r
                    for r in rows
                    if r["record_type"] == "current.item"
                    and f"current.item:{r['id']}" == linked.item_ref
                ),
                "id": str(RecordRef.parse(deleted.item_ref).id),
                "source_ref": sources["b2"],
            },
        ],
        # A tombstone that claims to be live, with no item to back it.
        "tombstone claimed live": _corrupt(rows, sources["b2"], item_state="available"),
        "live link to a missing project": _corrupt(
            rows, sources["b2"], project_ref=unknown_project
        ),
        "link disagrees with the item's project": _corrupt(
            rows,
            sources["b1"],
            project_state="cleared",
            project_ref=None,
            last_project_ref=linked.project_ref,
        ),
        "link for another item's source": _corrupt(
            rows, sources["b1"], source_ref="leads.handoff:somewhere-else"
        ),
        "linked without a live ref": _corrupt(rows, sources["b1"], project_ref=None),
        "not an item reference": _corrupt(
            rows, sources["b2"], item_ref=deleted.item_ref.replace("item", "project")
        ),
        "two links for one item": [
            *rows,
            {
                **next(
                    r
                    for r in rows
                    if r["record_type"] == "current.source_item_link"
                    and r["source_ref"] == sources["b2"]
                ),
                "source_ref": "leads.handoff:a-second-source",
            },
        ],
        "revision zero": _corrupt(rows, sources["b5"], revision=0),
        "unknown state": _corrupt(rows, sources["b5"], project_state="lost"),
    }
    for reason, artifact in broken.items():
        target = install(h.cluster, make_workspace(), owner_account_id)
        with open_unit_of_work(target) as uow:
            with pytest.raises(Exception, match="artifact_invalid"):
                import_records(uow, artifact)
            assert list(uow.connection.execute(select(t.item))) == [], reason


def test_two_links_cannot_share_one_item(links: Links) -> None:
    """The unique index on ``item_ref`` refuses a write that would point a second
    source's link at an item another link already holds.

    It proves uniqueness only: the database does not stop ``item_ref`` changing to an
    unused item. That ``item_ref`` stays fixed is pinned by the write-path tests,
    because no lifecycle path rewrites it."""
    h = links
    one, two = h.handoff(), h.handoff(OTHER)
    with open_unit_of_work(h.owner) as uow:
        with pytest.raises(Exception, match="uq_source_item_link_item"):
            uow.connection.execute(
                update(t.source_item_link)
                .where(t.source_item_link.c.source_ref == OTHER)
                .values(item_ref=one)
            )
    assert (h.link().item_ref, h.link(OTHER).item_ref) == (one, two)
