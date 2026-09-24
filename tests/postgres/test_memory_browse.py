"""The read surface the memory screens call: ``read()`` over an entity container, with
and without a target; ``recallatron.memory.get``; and the pinned operation inventory.

Seams under test: the three operations through ``dispatch`` against a real installed
and enabled Recallatron, and ``recallatron_read`` through the MCP tool façade. Rows are
written straight through the module's repository, as every synthetic fixture in this
suite is, so each test controls exactly which memories, entities and mentions exist.

**This is the run's disclosure-shaped seam.** A targetless entity read is a new way to
ask "what does this entity's window hold", and a bug here shows one member another
member's private memory — or merely counts it — with no screen looking wrong. Two
named tests carry that risk:

- :func:`test_entity_window_excludes_and_does_not_count_an_unreadable_mentioning_memory`
  places an unreadable mentioning memory where it would sit **inside** the window if
  it counted, and holds every count against the same read after the memory is gone.
  Its case (ii) — a workspace-audience memory whose source the caller cannot read —
  survives the row-local prefilter and is removed only by full evaluation, so it is
  the case that catches a count taken from the candidate list.
- :func:`test_entity_container_visibility_is_current_mode_in_both_branches` pins that
  ``include_invalidated`` widens which *members* a window may show and never which
  *entities* a caller may open one over.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from harness.registry import add_member
from rheo_app_mcp.tools import call_tool
from rheo_contracts import Role, SafetyClass, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import register_resolver
from rheo_core.storage.backend import UnitOfWork
from rheo_recallatron import MANIFEST
from rheo_recallatron import eligibility as memory_eligibility
from rheo_recallatron.configuration import MEMORY_RECORD_TYPE
from rheo_recallatron.contracts import EntityItem
from rheo_recallatron.eligibility import ReferenceBudget, memory_reference
from rheo_recallatron.embedding.operations import EMBEDDING_COVERAGE
from rheo_recallatron.entities import ENTITY_GET, normalized_name
from rheo_recallatron.operations import (
    MEMORY_GET,
    MEMORY_READ,
    MemoryItem,
    ReadWindow,
)
from rheo_recallatron.references import entity_reference
from rheo_recallatron.resolvers import resolve_memory
from rheo_recallatron.storage import tables as memory_tables
from rheo_recallatron.storage.repository import (
    MemoryEntityRow,
    MemoryLinkRow,
    MemoryMentionRow,
    MemoryPurposeRow,
    MemoryRow,
    delete_memory,
    insert_memory,
    insert_memory_entity,
    insert_memory_link,
    insert_memory_mention,
    insert_memory_purpose,
)
from sqlalchemy import Connection, Engine, insert

pytestmark = pytest.mark.postgres

_MEMORY_MODULE = MANIFEST.module_id
_RESPOND = "respond"
_NOT_FOUND: Final = "not_found"
_INPUT_INVALID: Final = "input_invalid"


# --- the workspace --------------------------------------------------------------------


@dataclass(frozen=True)
class BrowseWorkspace:
    """A workspace with Recallatron installed, enabled and migrated, and a second
    member beside its owner."""

    cluster: ClusterSession
    workspace: UUID
    owner_account_id: UUID
    other_account_id: UUID
    surfaces: LoadedSurfaces
    database_name: str
    engine: Engine

    def owner(self) -> WorkspaceContext:
        return self._context(self.owner_account_id, Role.OWNER)

    def other(self) -> WorkspaceContext:
        """The second member: the account whose private memories the owner's reads
        must neither show nor count."""
        return self._context(self.other_account_id, Role.MEMBER)

    def _context(self, account_id: UUID, role: Role) -> WorkspaceContext:
        ctx = context_for_harness(self.workspace, account_id, role)
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    @contextmanager
    def unit(self) -> Iterator[Connection]:
        with UnitOfWork(self.engine, self.database_name) as uow:
            yield uow.connection
            uow.commit()

    def call(
        self, ctx: WorkspaceContext, name: str, payload: dict[str, object]
    ) -> OperationOutcome:
        return dispatch(
            ctx,
            name,
            payload,
            registry=self.surfaces.operations,
            consumers=ConsumerRegistry(),
        )

    def read(self, ctx: WorkspaceContext, **payload: object) -> OperationOutcome:
        return self.call(ctx, MEMORY_READ, payload)

    def get(self, ctx: WorkspaceContext, **payload: object) -> OperationOutcome:
        return self.call(ctx, MEMORY_GET, payload)


@pytest.fixture
def browse(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[BrowseWorkspace]:
    """Recallatron loaded through the harness's one recipe and enabled.

    The memory resolver goes on the process-wide table too, where a real loader puts
    it: an entity's backing ref is resolved through ``resolve_in``'s default, so
    without it no backing ref could ever resolve and the zero-mention entity below
    would be invisible for the wrong reason.
    """
    register_core_operations()
    register_resolver(
        _MEMORY_MODULE, MEMORY_RECORD_TYPE, resolve_memory, origin=_MEMORY_MODULE
    )
    with loaded_probe_modules(monkeypatch, _MEMORY_MODULE) as surfaces:
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext), bootstrap
        install_and_enable_module(cluster.backend, bootstrap, workspace, _MEMORY_MODULE)
        other = add_member(
            cluster.backend, workspace, Role.MEMBER, display_name="member-two"
        )
        database_name = cluster.registry_row(workspace).database_name
        yield BrowseWorkspace(
            cluster=cluster,
            workspace=workspace,
            owner_account_id=owner_account_id,
            other_account_id=other,
            surfaces=surfaces,
            database_name=database_name,
            engine=cluster.backend.pools.engine_for(database_name),
        )


# --- seeding ---------------------------------------------------------------------


def _at(offset_seconds: int) -> datetime:
    """A ``recorded_at`` inside any window, ordered by offset. Relative to the clock so
    it never ages out of anything."""
    return datetime.now(UTC) - timedelta(days=1) + timedelta(seconds=offset_seconds)


def _row(
    title: str,
    *,
    at: int,
    audience_id: UUID | None = None,
    invalidation_reason: str | None = None,
) -> MemoryRow:
    """A memory row: workspace audience unless ``audience_id`` names its member."""
    recorded = _at(at)
    return MemoryRow(
        id=uuid7(),
        kind="note",
        title=title,
        body=f"a synthetic note titled {title}",
        audience_kind="workspace" if audience_id is None else "member",
        audience_id=audience_id,
        confidence=None,
        occurred_at=None,
        recorded_at=recorded,
        recorded_by_kind="account",
        recorded_by_id=None,
        origin="told",
        revision=1,
        corrected_at=None,
        superseded_by_id=None,
        invalidated_at=None if invalidation_reason is None else recorded,
        invalidation_reason=invalidation_reason,
        source_namespace=None,
        external_source_key=None,
    )


def _write(
    conn: Connection,
    row: MemoryRow,
    *,
    mentions: Sequence[UUID] = (),
    derived_from: Sequence[UUID] = (),
) -> MemoryRow:
    insert_memory(conn, row)
    insert_memory_purpose(conn, MemoryPurposeRow(memory_id=row.id, purpose=_RESPOND))
    for source in derived_from:
        insert_memory_link(
            conn,
            MemoryLinkRow(
                memory_id=row.id,
                ref=memory_reference(source),
                relation="derived_from",
                created_at=row.recorded_at,
                supersession_lineage=False,
            ),
        )
    for entity_id in mentions:
        insert_memory_mention(
            conn, MemoryMentionRow(memory_id=row.id, entity_id=entity_id, role=None)
        )
    return row


def _entity(conn: Connection, name: str, *, backing_ref: str | None = None) -> UUID:
    row = insert_memory_entity(
        conn,
        MemoryEntityRow(
            id=uuid7(),
            kind="project",
            name=name,
            normalized_name=normalized_name(name),
            ref=backing_ref,
            created_at=_at(0),
            source_namespace=None,
            external_source_key=None,
        ),
    )
    return row.id


def _mentioned_many(conn: Connection, entity_id: UUID, count: int, *, at: int) -> None:
    """``count`` readable memories mentioning one entity, in three round trips.

    The 500/501 boundary needs an entity that large, and a row-at-a-time loop makes
    that case slow enough to discourage writing it.
    """
    rows = [_row(f"bulk {index:04d}", at=at + index) for index in range(count)]
    conn.execute(insert(memory_tables.memory), [asdict(row) for row in rows])
    conn.execute(
        insert(memory_tables.memory_purpose),
        [{"memory_id": row.id, "purpose": _RESPOND} for row in rows],
    )
    conn.execute(
        insert(memory_tables.memory_mention),
        [{"memory_id": row.id, "entity_id": entity_id, "role": None} for row in rows],
    )


# --- reading the answers ---------------------------------------------------------


def _window(outcome: OperationOutcome) -> ReadWindow:
    assert outcome.ok, outcome
    assert isinstance(outcome.result, ReadWindow), outcome
    return outcome.result


def _refused(outcome: OperationOutcome, state: str) -> None:
    """A refusal and nothing else: no result, and the state in the error too."""
    assert outcome.state == state, outcome
    assert outcome.result is None, outcome
    assert outcome.error is not None and outcome.error.error_code == state, outcome


def _titles(window: ReadWindow) -> list[str]:
    return [item.title for item in window.items]


def _shape(window: ReadWindow) -> tuple[int, int, int, bool, int | None]:
    """Every number a window reports, the tuple the uncounted assertion compares."""
    return (
        window.total,
        window.window_start,
        window.window_end,
        window.has_more,
        window.target_position,
    )


def _seven_mentions(browse: BrowseWorkspace) -> tuple[str, list[MemoryRow]]:
    """A visible entity mentioned by seven readable memories, oldest first, and one
    readable memory beside them that does not mention it."""
    with browse.unit() as conn:
        entity_id = _entity(conn, "Harbour Survey")
        members = [
            _write(conn, _row(f"m{index}", at=10 * (index + 1)), mentions=[entity_id])
            for index in range(7)
        ]
        _write(conn, _row("unrelated", at=35))
    return entity_reference(entity_id), members


# --- read() over an entity container -------------------------------------------------


def test_a_targetless_entity_read_answers_its_newest_window(
    browse: BrowseWorkspace,
) -> None:
    entity, _ = _seven_mentions(browse)

    newest = _window(browse.read(browse.owner(), container_ref=entity, context=2))
    assert _titles(newest) == ["m2", "m3", "m4", "m5", "m6"]
    assert _shape(newest) == (7, 2, 7, True, None)

    everything = _window(browse.read(browse.owner(), container_ref=entity, context=10))
    assert _titles(everything) == [f"m{index}" for index in range(7)]
    assert _shape(everything) == (7, 0, 7, False, None)

    only_newest = _window(browse.read(browse.owner(), container_ref=entity, context=0))
    assert _titles(only_newest) == ["m6"]
    assert _shape(only_newest) == (7, 6, 7, True, None)


def test_a_targeted_entity_read_recentres_beside_an_open_window(
    browse: BrowseWorkspace,
) -> None:
    """The ``?around=`` step: take a reference out of the window already open and ask
    for the window centred on it."""
    entity, members = _seven_mentions(browse)
    opened = _window(browse.read(browse.owner(), container_ref=entity, context=1))
    assert _titles(opened) == ["m4", "m5", "m6"]

    around = opened.items[0].ref
    assert around == memory_reference(members[4].id)
    recentred = _window(
        browse.read(browse.owner(), container_ref=entity, target_ref=around, context=1)
    )
    assert _titles(recentred) == ["m3", "m4", "m5"]
    assert _shape(recentred) == (7, 3, 6, True, 1)
    assert recentred.target_position is not None
    assert recentred.items[recentred.target_position].ref == around

    oldest = _window(
        browse.read(
            browse.owner(),
            container_ref=entity,
            target_ref=memory_reference(members[0].id),
            context=1,
        )
    )
    assert _titles(oldest) == ["m0", "m1"]
    assert _shape(oldest) == (7, 0, 2, True, 0)


def test_a_visible_entity_with_no_readable_mentions_answers_an_empty_window(
    browse: BrowseWorkspace,
) -> None:
    """Edge case 1: an entity with nothing to show is an empty window, not a refusal.

    The entity is visible through its readable backing ref alone — an entity with no
    backing ref and no readable mention would be ``not_found`` instead, which is a
    different case. It is also mentioned by the other member's private memory, which
    must leave the owner's window empty and its total at zero rather than one.
    """
    with browse.unit() as conn:
        backing = _write(conn, _row("a backing record", at=1))
        entity_id = _entity(
            conn, "Quiet Entity", backing_ref=memory_reference(backing.id)
        )
    entity = entity_reference(entity_id)

    got = browse.call(browse.owner(), ENTITY_GET, {"entity_ref": entity})
    assert got.ok and isinstance(got.result, EntityItem), got
    assert got.result.mention_count == 0

    empty = _window(browse.read(browse.owner(), container_ref=entity))
    assert empty.items == ()
    assert _shape(empty) == (0, 0, 0, False, None)

    with browse.unit() as conn:
        _write(
            conn,
            _row(
                "private to the other member", at=2, audience_id=browse.other_account_id
            ),
            mentions=[entity_id],
        )
    still_empty = _window(browse.read(browse.owner(), container_ref=entity))
    assert still_empty.items == ()
    assert _shape(still_empty) == (0, 0, 0, False, None)
    # The other member does see it: the owner's zero is exclusion, not absence.
    theirs = _window(browse.read(browse.other(), container_ref=entity))
    assert _titles(theirs) == ["private to the other member"]


def test_an_entity_invisible_to_entity_get_is_not_found_to_read(
    browse: BrowseWorkspace,
) -> None:
    """Visibility is ``entity.get``'s answer, checked before membership.

    The targeted read names a memory the owner may read and which does not mention the
    entity: were membership checked first it would answer
    ``container_membership_required`` and confirm the entity exists. Visibility first
    makes it the same ``not_found``, with the same words, as an entity that never
    existed.
    """
    with browse.unit() as conn:
        hidden_id = _entity(conn, "Hidden Entity")
        _write(
            conn,
            _row("the other member's", at=1, audience_id=browse.other_account_id),
            mentions=[hidden_id],
        )
        readable = _write(conn, _row("readable, mentions nothing", at=2))
    hidden = entity_reference(hidden_id)
    unknown = entity_reference(uuid7())
    owner = browse.owner()

    _refused(browse.call(owner, ENTITY_GET, {"entity_ref": hidden}), _NOT_FOUND)
    answers: list[str] = []
    for entity in (hidden, unknown):
        for target in (None, memory_reference(readable.id)):
            payload: dict[str, object] = {"container_ref": entity}
            if target is not None:
                payload["target_ref"] = target
            outcome = browse.call(owner, MEMORY_READ, payload)
            _refused(outcome, _NOT_FOUND)
            assert outcome.error is not None
            answers.append(outcome.error.error_text)
    assert len(set(answers)) == 1, answers

    # Positive control: the member who can read the mention opens the window.
    assert _titles(_window(browse.read(browse.other(), container_ref=hidden))) == [
        "the other member's"
    ]


def test_a_visible_entity_refuses_a_target_that_does_not_mention_it(
    browse: BrowseWorkspace,
) -> None:
    entity, _ = _seven_mentions(browse)
    with browse.unit() as conn:
        outsider = _write(conn, _row("readable, not a mention", at=99))
    _refused(
        browse.read(
            browse.owner(),
            container_ref=entity,
            target_ref=memory_reference(outsider.id),
        ),
        "container_membership_required",
    )


def test_an_entity_read_scans_five_hundred_members_and_refuses_the_501st(
    browse: BrowseWorkspace,
) -> None:
    """The 500/501 boundary, in both branches. The refusal is fixed and content-free:
    no count, no identity, no window."""
    with browse.unit() as conn:
        entity_id = _entity(conn, "Busy Entity")
        _mentioned_many(conn, entity_id, 499, at=1)
        target = _write(conn, _row("the target", at=600), mentions=[entity_id])
    entity = entity_reference(entity_id)
    owner = browse.owner()
    targeted: dict[str, object] = {
        "container_ref": entity,
        "target_ref": memory_reference(target.id),
    }

    newest = _window(browse.read(owner, container_ref=entity))
    assert newest.total == 500
    assert _titles(newest)[-1] == "the target"
    assert _shape(_window(browse.call(owner, MEMORY_READ, targeted))) == (
        500,
        497,
        500,
        True,
        2,
    )

    with browse.unit() as conn:
        _write(conn, _row("the five hundred and first", at=700), mentions=[entity_id])
    for payload in ({"container_ref": entity}, targeted):
        outcome = browse.call(owner, MEMORY_READ, payload)
        _refused(outcome, "window_scan_limit")
        assert outcome.error is not None
        assert "500" not in outcome.error.error_text
        assert "501" not in outcome.error.error_text


def test_an_entity_read_that_exhausts_the_shared_budget_refuses_reference_scan_limit(
    browse: BrowseWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§ A13's shared budget, driven against a deliberately small one.

    Four thousand and ninety-six references is not a fixture worth reading, so the
    budget every request builds is made small and the rule is what is under test:
    an overflow anywhere in the read — visibility, the target, the window — refuses
    ``reference_scan_limit`` with nothing partial beside it.
    """
    with browse.unit() as conn:
        entity_id = _entity(conn, "Connected Entity")
        members: list[MemoryRow] = []
        for index in range(6):
            source = _write(conn, _row(f"source {index}", at=index))
            members.append(
                _write(
                    conn,
                    _row(f"member {index}", at=100 + index),
                    mentions=[entity_id],
                    derived_from=[source.id],
                )
            )
    entity = entity_reference(entity_id)
    target = memory_reference(members[2].id)

    # Positive control under the real budget, so the refusal below is the budget's.
    assert _window(browse.read(browse.owner(), container_ref=entity)).total == 6

    monkeypatch.setattr(
        memory_eligibility,
        "ReferenceBudget",
        functools.partial(ReferenceBudget, limit=8),
    )
    _refused(browse.read(browse.owner(), container_ref=entity), "reference_scan_limit")
    _refused(
        browse.read(browse.owner(), container_ref=entity, target_ref=target),
        "reference_scan_limit",
    )


def test_a_link_container_without_a_target_is_input_invalid(
    browse: BrowseWorkspace,
) -> None:
    """Only an entity container may be read with no target. A record reference, a
    malformed one, and a foreign one all refuse before anything is looked up."""
    with browse.unit() as conn:
        container = _write(conn, _row("container", at=1))
        member = _write(conn, _row("member", at=2), derived_from=[container.id])
    link_container = memory_reference(container.id)
    owner = browse.owner()

    for container_ref in (
        link_container,
        "not-a-reference",
        f"harness.note:{uuid7()}",
    ):
        _refused(browse.read(owner, container_ref=container_ref), _INPUT_INVALID)

    # The link container itself is unchanged with a target.
    linked = _window(
        browse.read(
            owner, container_ref=link_container, target_ref=memory_reference(member.id)
        )
    )
    assert _titles(linked) == ["member"]
    assert linked.target_position == 0


# --- the two named privacy tests -----------------------------------------------------

_OTHER_MEMBERS_PRIVATE = "other_members_private"
_UNREADABLE_SOURCE = "unreadable_source"


@pytest.mark.parametrize("targeted", [False, True], ids=["targetless", "targeted"])
@pytest.mark.parametrize("unreadable", [_OTHER_MEMBERS_PRIVATE, _UNREADABLE_SOURCE])
def test_entity_window_excludes_and_does_not_count_an_unreadable_mentioning_memory(
    browse: BrowseWorkspace, unreadable: str, targeted: bool
) -> None:
    """An unreadable mentioning memory is absent **and** uncounted.

    Seven readable memories ``m0``..``m6`` mention the entity; the unreadable one is
    recorded between ``m5`` and ``m6``, so it sits inside both the newest window and
    the window centred on ``m5`` if it counted. The two kinds:

    (i) the other member's ``member``-audience memory — the row-local prefilter drops
        it in SQL;
    (ii) a ``workspace``-audience memory derived from the other member's private
         memory — it passes the prefilter and is removed only by full evaluation, which
         is the case a count taken from the candidate list gets wrong.

    Uncounted is asserted as equality with the identical read after the memory is
    physically deleted: every number the window reports, and every item in it.
    """
    with browse.unit() as conn:
        entity_id = _entity(conn, "Shared Entity")
        members = [
            _write(conn, _row(f"m{index}", at=10 * (index + 1)), mentions=[entity_id])
            for index in range(7)
        ]
        if unreadable == _OTHER_MEMBERS_PRIVATE:
            hidden = _write(
                conn,
                _row("hidden", at=65, audience_id=browse.other_account_id),
                mentions=[entity_id],
            )
        else:
            private_source = _write(
                conn,
                _row("private source", at=1, audience_id=browse.other_account_id),
            )
            hidden = _write(
                conn,
                _row("hidden", at=65),
                mentions=[entity_id],
                derived_from=[private_source.id],
            )
    entity = entity_reference(entity_id)
    hidden_ref = memory_reference(hidden.id)
    payload: dict[str, object] = {"container_ref": entity, "context": 2}
    if targeted:
        payload["target_ref"] = memory_reference(members[5].id)

    # The fixture's premise: the other member can read it, so it is a real mention of
    # a real memory and only this caller's reach excludes it.
    assert browse.get(browse.other(), ref=hidden_ref).ok

    before = _window(browse.call(browse.owner(), MEMORY_READ, payload))
    assert hidden_ref not in {item.ref for item in before.items}

    with browse.unit() as conn:
        assert delete_memory(conn, hidden.id)
    after = _window(browse.call(browse.owner(), MEMORY_READ, payload))

    assert _shape(before) == _shape(after)
    assert [item.ref for item in before.items] == [item.ref for item in after.items]
    if targeted:
        assert _shape(before) == (7, 3, 7, True, 2)
        assert _titles(before) == ["m3", "m4", "m5", "m6"]
    else:
        assert _shape(before) == (7, 2, 7, True, None)
        assert _titles(before) == ["m2", "m3", "m4", "m5", "m6"]


def test_entity_container_visibility_is_current_mode_in_both_branches(
    browse: BrowseWorkspace,
) -> None:
    """``include_invalidated`` widens a window's members, never which entities open one.

    The entity's only mention is on a derivative invalidated by a correction of its
    source: retained history, not current, and with no backing ref. ``entity.get``
    cannot see it, so ``read()`` cannot open a window over it in either branch — even
    in history mode, where the derivative itself is readable. And a current-visible
    entity still opens a history-mode window centred on a retained member: visibility
    is never stricter than ``entity.get``'s own answer once a target exists.
    """
    with browse.unit() as conn:
        source = _write(conn, _row("the corrected source", at=1))
        historical_id = _entity(conn, "Historical Entity")
        derivative = _write(
            conn,
            _row("a retained derivative", at=2, invalidation_reason="source_corrected"),
            mentions=[historical_id],
            derived_from=[source.id],
        )
        current_id = _entity(conn, "Current Entity")
        _write(conn, _row("a live mention", at=3), mentions=[current_id])
        retained = _write(
            conn,
            _row("a retained mention", at=4, invalidation_reason="source_corrected"),
            mentions=[current_id],
            derived_from=[source.id],
        )
    historical = entity_reference(historical_id)
    owner = browse.owner()

    # The premise: entity.get cannot see it, and the derivative itself is readable as
    # history — so any window over it would come from a mode leak, not from nothing.
    _refused(browse.call(owner, ENTITY_GET, {"entity_ref": historical}), _NOT_FOUND)
    assert browse.get(
        owner, ref=memory_reference(derivative.id), include_invalidated=True
    ).ok

    targeted = browse.read(
        owner,
        container_ref=historical,
        target_ref=memory_reference(derivative.id),
        include_invalidated=True,
    )
    _refused(targeted, _NOT_FOUND)
    targetless = browse.read(owner, container_ref=historical, include_invalidated=True)
    _refused(targetless, _NOT_FOUND)

    current = _window(
        browse.read(
            owner,
            container_ref=entity_reference(current_id),
            target_ref=memory_reference(retained.id),
            include_invalidated=True,
        )
    )
    assert _titles(current) == ["a live mention", "a retained mention"]
    assert current.target_position == 1


# --- the tool keeps requiring a target -----------------------------------------------


def test_the_read_tool_still_requires_a_target(browse: BrowseWorkspace) -> None:
    """``recallatron_read`` validates against ``ReadToolInput`` before dispatch, so the
    operation's new targetless shape is not reachable through the tool."""
    entity, members = _seven_mentions(browse)
    owner = browse.owner()

    def tool(arguments: dict[str, object]) -> OperationOutcome:
        return call_tool(
            owner,
            "recallatron_read",
            arguments,
            consumers=None,
            tools=browse.surfaces.tools,
            registry=browse.surfaces.operations,
        )

    missing = tool({"container_ref": entity})
    _refused(missing, _INPUT_INVALID)
    assert missing.error is not None and "target_ref" in missing.error.error_text

    # The same entity container with a target is fine through the tool.
    reached = tool(
        {"container_ref": entity, "target_ref": memory_reference(members[3].id)}
    )
    assert _window(reached).target_position == 2


# --- recallatron.memory.get ----------------------------------------------------------


def _item(outcome: OperationOutcome) -> MemoryItem:
    assert outcome.ok, outcome
    assert isinstance(outcome.result, MemoryItem), outcome
    return outcome.result


def test_memory_get_opens_one_readable_memory(browse: BrowseWorkspace) -> None:
    with browse.unit() as conn:
        source = _write(conn, _row("the source", at=1))
        found = _write(conn, _row("found", at=2), derived_from=[source.id])
    item = _item(browse.get(browse.owner(), ref=memory_reference(found.id)))
    assert item.ref == memory_reference(found.id)
    assert item.title == "found"
    assert [(link.ref, link.display) for link in item.links] == [
        (memory_reference(source.id), "the source")
    ]
    # Members read it too: READ_ROLES, not owner-only.
    theirs = _item(browse.get(browse.other(), ref=memory_reference(found.id)))
    assert theirs.title == "found"


def test_memory_get_refuses_gone_and_not_yours_identically(
    browse: BrowseWorkspace,
) -> None:
    """Missing, deleted, and somebody else's private memory: one answer, one wording,
    no field that tells them apart."""
    with browse.unit() as conn:
        theirs = _write(conn, _row("theirs", at=1, audience_id=browse.other_account_id))
        deleted = _write(conn, _row("deleted", at=2))
        private_source = _write(
            conn, _row("private source", at=3, audience_id=browse.other_account_id)
        )
        derived = _write(conn, _row("derived", at=4), derived_from=[private_source.id])
    with browse.unit() as conn:
        assert delete_memory(conn, deleted.id)

    answers: set[tuple[str, str, str]] = set()
    for memory_id in (uuid7(), deleted.id, theirs.id, derived.id):
        outcome = browse.get(browse.owner(), ref=memory_reference(memory_id))
        _refused(outcome, _NOT_FOUND)
        assert outcome.error is not None
        answers.add((outcome.state, outcome.error.error_code, outcome.error.error_text))
    assert len(answers) == 1, answers

    # The other member reads its own.
    assert _item(browse.get(browse.other(), ref=memory_reference(theirs.id)))

    for bad in ("not-a-reference", entity_reference(uuid7())):
        _refused(browse.get(browse.owner(), ref=bad), _INPUT_INVALID)


def test_memory_get_admits_retained_history_only_when_asked(
    browse: BrowseWorkspace,
) -> None:
    with browse.unit() as conn:
        retained = _write(
            conn, _row("retained", at=1, invalidation_reason="source_corrected")
        )
        erased = _write(
            conn, _row("erased", at=2, invalidation_reason="source_deleted")
        )
    owner = browse.owner()

    _refused(browse.get(owner, ref=memory_reference(retained.id)), _NOT_FOUND)
    history = _item(
        browse.get(owner, ref=memory_reference(retained.id), include_invalidated=True)
    )
    assert history.invalidation_reason == "source_corrected"
    for include_invalidated in (False, True):
        _refused(
            browse.get(
                owner,
                ref=memory_reference(erased.id),
                include_invalidated=include_invalidated,
            ),
            _NOT_FOUND,
        )


# --- AC 8 / AC 18: the operation inventory -------------------------------------------

_REGISTERED_1A2: Final = frozenset(
    {
        "recallatron.memory.recall",
        "recallatron.memory.read",
        "recallatron.memory.remember",
        "recallatron.memory.derive",
        "recallatron.memory.correct",
        "recallatron.memory.supersede",
        "recallatron.entity.list",
        "recallatron.entity.get",
        "recallatron.memory.dedup_candidates",
        "recallatron.embedding.rebuild",
    }
)
"""What run 1a2 shipped, written out rather than derived: deriving it from the manifest
would make the pin a tautology over whatever the manifest now declares."""

_ADDED_1A3: Final = frozenset(
    {"recallatron.memory.get", "recallatron.embedding.coverage"}
)


def test_recallatron_registers_exactly_the_1a2_set_plus_get_and_coverage(
    browse: BrowseWorkspace,
) -> None:
    """No new list-class name lands silently: the declared set and the loaded registry
    are both exactly 1a2's plus the two single-answer reads this run adds."""
    declared = {declaration.name for declaration, _ in MANIFEST.operations}
    assert declared == _REGISTERED_1A2 | _ADDED_1A3
    loaded = {
        name
        for name in browse.surfaces.operations.names()
        if name.startswith(f"{_MEMORY_MODULE}.")
    }
    assert loaded == _REGISTERED_1A2 | _ADDED_1A3
    assert {MEMORY_GET, EMBEDDING_COVERAGE} == _ADDED_1A3

    tooled = {tool.operation for tool in MANIFEST.tools}
    for declaration, _ in MANIFEST.operations:
        if declaration.name not in _ADDED_1A3:
            continue
        assert declaration.safety_class is SafetyClass.READ, declaration.name
        assert declaration.audit is None, declaration.name
        assert declaration.long_running is False, declaration.name
        assert declaration.name not in tooled, declaration.name
    # Single-record, not list-class: one memory in, one memory out.
    get_declaration = next(
        declaration
        for declaration, _ in MANIFEST.operations
        if declaration.name == MEMORY_GET
    )
    assert get_declaration.output is MemoryItem
