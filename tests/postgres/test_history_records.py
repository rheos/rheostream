"""Searchable predecessor evidence stays separate and owner-only."""

from collections.abc import Iterator
from datetime import UTC, datetime
from functools import partial
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import install_and_enable_module, loaded_probe_modules
from harness.registry import add_member
from rheo_contracts import Role, WorkspaceContext
from rheo_core.approvals import APPROVAL_APPROVE
from rheo_core.approvals import operations as approval_operations
from rheo_core.approvals.gate import execute_approved
from rheo_core.boundary import context_for_harness
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import OPERATION_GET, dispatch, register_core_operations
from rheo_core.operations.registry import REGISTRY
from rheo_core.refs import uuid7
from rheo_core.storage.backend import UnitOfWork
from rheo_recallatron import MANIFEST
from rheo_recallatron.history import (
    HISTORY_ERASE,
    HISTORY_GET,
    HISTORY_SEARCH,
    HistoryItem,
    HistorySearchResult,
)
from rheo_recallatron.migration.curated_import import import_curated
from rheo_recallatron.migration.extract import ExtractUnit, dump_extract, parse_extract
from rheo_recallatron.migration.history_extract import (
    HistoryExtractUnit,
    dump_history_extract,
    parse_history_extract,
)
from rheo_recallatron.migration.history_import import (
    import_history,
    opaque_history_source_key,
)
from rheo_recallatron.storage.tables import history_record, memory
from sqlalchemy import insert, select, update

pytestmark = pytest.mark.postgres


@pytest.fixture
def history_workspace(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[tuple[WorkspaceContext, WorkspaceContext, object, str, object]]:
    register_core_operations()
    with loaded_probe_modules(monkeypatch, MANIFEST.module_id) as surfaces:
        register_core_operations(surfaces.operations)
        monkeypatch.setattr(
            REGISTRY,
            "_operations",
            {**REGISTRY._operations, **surfaces.operations._operations},
        )
        monkeypatch.setattr(
            approval_operations,
            "execute_approved",
            partial(execute_approved, registry=surfaces.operations),
        )
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext)
        install_and_enable_module(
            cluster.backend, bootstrap, workspace, MANIFEST.module_id
        )
        owner = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(owner, WorkspaceContext)
        member_id = add_member(
            cluster.backend, workspace, Role.MEMBER, display_name="history-member"
        )
        member = context_for_harness(workspace, member_id, Role.MEMBER)
        assert isinstance(member, WorkspaceContext)
        database_name = cluster.registry_row(workspace).database_name
        engine = cluster.backend.pools.engine_for(database_name)
        yield owner, member, surfaces.operations, database_name, engine


def test_history_search_get_and_erasure_visibility(history_workspace: tuple) -> None:
    owner, member, operations, database_name, engine = history_workspace
    record_id = uuid7()
    at = datetime.now(UTC)
    with UnitOfWork(engine, database_name) as uow:
        uow.connection.execute(
            insert(history_record).values(
                id=record_id,
                source_namespace="synthetic",
                external_source_key=opaque_history_source_key("conversation:1"),
                source_reference="conversation:1",
                kind="conversation",
                status="turn",
                title="Garden conversation",
                body="The cedar beds need mulch.",
                occurred_at=at,
                imported_at=at,
                session_key="session-1",
                chat_key="chat-1",
                source_role="user",
                source_category=None,
                confirmed_at=None,
                superseded_by_source_key=None,
                erased_at=None,
            )
        )
        uow.commit()

    def call(ctx: WorkspaceContext, operation: str, payload: dict[str, object]):
        return dispatch(
            ctx, operation, payload, registry=operations, consumers=ConsumerRegistry()
        )

    result = call(owner, HISTORY_SEARCH, {"query": "cedar"})
    assert result.ok, result
    assert isinstance(result.result, HistorySearchResult)
    assert len(result.result.items) == 1
    hit = result.result.items[0]
    assert hit.id == record_id
    assert hit.kind == "conversation" and hit.status == "turn"
    assert hit.body == "The cedar beds need mulch."

    detail = call(owner, HISTORY_GET, {"id": str(record_id)})
    assert detail.ok and isinstance(detail.result, HistoryItem)
    assert detail.result.id == record_id

    for operation, payload in (
        (HISTORY_SEARCH, {"query": "cedar"}),
        (HISTORY_GET, {"id": str(record_id)}),
        (HISTORY_ERASE, {"id": str(record_id)}),
    ):
        denied = call(member, operation, payload)
        assert not denied.ok and denied.result is None
        assert denied.approval_id is None

    pending = call(owner, HISTORY_ERASE, {"id": str(record_id)})
    assert pending.state == "approval_required"
    assert pending.approval_id is not None
    still_present = call(owner, HISTORY_GET, {"id": str(record_id)})
    assert still_present.ok
    erased = call(owner, APPROVAL_APPROVE, {"approval_id": str(pending.approval_id)})
    assert erased.ok, erased
    held_record = call(
        owner, OPERATION_GET, {"operation_id": str(pending.operation_id)}
    )
    assert erased.result.state == "executed", (
        held_record.result.error_code,
        held_record.result.error_text,
    )
    after_search = call(owner, HISTORY_SEARCH, {"query": "cedar"})
    assert after_search.ok and isinstance(after_search.result, HistorySearchResult)
    assert after_search.result.items == ()
    after_get = call(owner, HISTORY_GET, {"id": str(record_id)})
    assert after_get.state == "not_found" and after_get.result is None
    with UnitOfWork(engine, database_name) as uow:
        tombstone = uow.connection.execute(
            select(history_record).where(history_record.c.id == record_id)
        ).one()
    assert tombstone.erased_at is not None
    assert tombstone.title == tombstone.body == ""
    assert tombstone.session_key is None
    assert tombstone.chat_key is None
    assert tombstone.source_role is None
    assert tombstone.source_reference is None


def test_history_import_replay_does_not_duplicate_or_resurrect(
    history_workspace: tuple,
) -> None:
    _owner, _member, _operations, database_name, engine = history_workspace
    at = datetime.now(UTC)
    unit = HistoryExtractUnit(
        external_source_key="store.procedural_note:7",
        kind="procedural_note",
        status="unconfirmed",
        title="Procedural note",
        body="A synthetic, unconfirmed planting method.",
        occurred_at=at,
        session_key="synthetic-session",
    )
    extract = parse_history_extract(
        dump_history_extract([unit], source_sha256="a" * 64, unresolved_count=0)
    )
    with UnitOfWork(engine, database_name) as uow:
        first = import_history(uow.connection, extract, imported_at=at)
        uow.commit()
    assert (first.inserted, first.identical, first.erased) == (1, 0, 0)

    with UnitOfWork(engine, database_name) as uow:
        second = import_history(uow.connection, extract, imported_at=at)
        uow.commit()
    assert (second.inserted, second.identical, second.erased) == (0, 1, 0)

    with UnitOfWork(engine, database_name) as uow:
        uow.connection.execute(
            update(history_record)
            .where(
                history_record.c.external_source_key
                == opaque_history_source_key(unit.external_source_key)
            )
            .values(
                title="",
                body="",
                source_reference=None,
                session_key=None,
                erased_at=datetime.now(UTC),
            )
        )
        uow.commit()
    with UnitOfWork(engine, database_name) as uow:
        third = import_history(uow.connection, extract, imported_at=at)
        uow.commit()
    assert (third.inserted, third.identical, third.erased) == (0, 0, 1)


def test_curated_import_uses_migrated_origin_and_stable_receipt(
    history_workspace: tuple,
) -> None:
    owner, _member, _operations, database_name, engine = history_workspace
    at = datetime.now(UTC)
    candidate = ExtractUnit(
        external_source_key="store.memory_item:synthetic-1",
        kind="fact",
        title="Garden note",
        body="The cedar beds need mulch.",
        confidence=0.8,
        recorded_at=at,
        mentions=(),
    )
    extract = parse_extract(dump_extract([candidate], unresolved_count=0))
    authority_id = uuid7()
    with UnitOfWork(engine, database_name) as uow:
        first = import_curated(
            owner,
            uow,
            extract,
            authority_id=authority_id,
            owner_id=owner.actor.id,
            consumers=ConsumerRegistry(),
            imported_at=at,
        )
        uow.commit()
    assert (first.active, first.unavailable) == (1, 0)
    with UnitOfWork(engine, database_name) as uow:
        second = import_curated(
            owner,
            uow,
            extract,
            authority_id=authority_id,
            owner_id=owner.actor.id,
            consumers=ConsumerRegistry(),
            imported_at=at,
        )
        rows = uow.connection.execute(select(memory)).all()
        uow.commit()
    assert (second.active, second.unavailable) == (1, 0)
    assert len(rows) == 1
    assert rows[0].origin == "migrated"
    assert rows[0].audience_kind == "member"
    assert rows[0].audience_id == owner.actor.id
