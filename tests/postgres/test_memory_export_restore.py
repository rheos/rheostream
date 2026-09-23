"""AC 11: Recallatron's export/import pair, and the restore that validates it.

Architecture § A14's row 11, bullet for bullet. The subject is the **module bridge**:
one source snapshot carrying a real module's rows beside the core categories, the
deterministic deletion-evidence category those bytes are compared on, the two-pass
import with its self-reference and marked-ancestry branches, and the diagnosable
``export_requires_retention_sweep`` refusal.

**Why the round trip is driven through the real operations and a real worker visit.**
Three of the properties here are properties of the *transaction map* rather than of
any function: an export that refuses publishes no artifact, a restore that fails
leaves neither imported memory nor deletion evidence and no active workspace, and a
snapshot that opened before a concurrent commit does not see it. Calling the
collector's pieces directly would leave all three unobserved and every assertion
still green.

**Why the validation branches are driven at the importer's own seam.** The two-pass
importer is a public seam of the module contract — core calls
``MANIFEST.export.importer(uow, rows)`` and then the continuation it returns — and
each validation branch needs an artifact built to be wrong in exactly one way. Going
through a full archive per branch would spend a provision, a drop and a restore to
change one field, and would test the same code through more machinery rather than
more of it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.modules import install_and_enable_module, loaded_probe_modules
from harness.registry import add_member
from rheo_contracts import Role, WorkspaceContext
from rheo_core.audit import AUDIT_LIST
from rheo_core.audit.operations import AuditListInput, audit_list_handler
from rheo_core.boundary import context_for_harness
from rheo_core.deletion import OWNED_DELETIONS, RETENTION_EXPIRY, USER_ERASURE
from rheo_core.deletion.records import (
    DeletionRecordRow,
    insert_deletion_record,
    list_deletion_records,
)
from rheo_core.deletion.tables import deletion_record
from rheo_core.events.consumers import ConsumerRegistry
from rheo_core.exports import (
    EXPORT_JOB_KIND,
    EXPORT_RESOURCE_UNAVAILABLE,
    RESTORE_JOB_KIND,
    WORKSPACE_DIGEST,
    WORKSPACE_EXPORT,
    WORKSPACE_RESTORE,
    ExportJobPayload,
    RestoreJobPayload,
    collect_export_snapshot,
    module_categories,
    module_entry_name,
    read_archive,
    run_export_job,
    run_restore_job,
    serialised_categories,
)
from rheo_core.exports import artifact as artifact_module
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.settings import ValueType
from rheo_core.settings.schema import REGISTRY as SETTINGS_REGISTRY
from rheo_core.storage import control_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import get_workspace
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.pools import EnginePool
from rheo_core.storage.repositories import upsert_workspace_setting
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_recallatron import MANIFEST
from rheo_recallatron.configuration import (
    AUTOMATIC_BOUND_PURPOSE,
    EMBEDDING_DIMENSIONS,
    MEMORY_RECORD_TYPE,
    RETENTION_DAYS_DEFAULT,
    RETENTION_DAYS_SPEC,
    RETENTION_EXPIRE_BY_AGE_KEY,
    RETENTION_EXPIRE_BY_AGE_SPEC,
)
from rheo_recallatron.eligibility import memory_reference
from rheo_recallatron.export import (
    ARTIFACT_INVALID,
    EXPORT_REQUIRES_RETENTION_SWEEP,
    ExportRequiresRetentionSweep,
    _Parsed,
    _successor_of,
    _validate_ancestry,
    export_memory_records,
    import_memory_records,
)
from rheo_recallatron.storage import tables as memory_tables
from rheo_recallatron.storage.repository import (
    MemoryEmbeddingRow,
    MemoryEntityRow,
    MemoryLinkRow,
    MemoryMentionRow,
    MemoryPurposeRow,
    MemoryRow,
    SourceReceiptRow,
    insert_memory,
    insert_memory_embedding,
    insert_memory_entity,
    insert_memory_link,
    insert_memory_mention,
    insert_memory_purpose,
    insert_source_receipt,
    set_memory_successor,
)
from sqlalchemy import Engine, delete, func, select, text

pytestmark = pytest.mark.postgres

_MODULE = MANIFEST.module_id
_CATEGORY = module_entry_name(_MODULE)
_OWNER = "memory-export-restore-test"
_RESPOND = "respond"
_DERIVED_FROM = "derived_from"
_SUPERSEDED = "source_superseded"
_CORRECTED = "source_corrected"
_ERASED = "source_deleted"
_EXPIRED_AGE = timedelta(days=RETENTION_DAYS_DEFAULT + 30)
_DIGEST = bytes(range(32))
_LATE_SETTING = "test.committed_after_snapshot_at"


# --- the two workspaces ---------------------------------------------------------------


@dataclass(frozen=True)
class Deployment:
    """A workspace with Recallatron installed, enabled and reachable."""

    cluster: ClusterSession
    workspace: UUID
    owner_account_id: UUID
    database_name: str
    engine: Engine

    def context(self, *, role: Role = Role.OWNER) -> WorkspaceContext:
        ctx = context_for_harness(self.workspace, self.owner_account_id, role)
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    @contextmanager
    def unit(self) -> Iterator[UnitOfWork]:
        with UnitOfWork(self.engine, self.database_name) as uow:
            yield uow
            uow.commit()

    @contextmanager
    def reading(self) -> Iterator[UnitOfWork]:
        with UnitOfWork(self.engine, self.database_name) as uow:
            yield uow

    def setting(self, key: str, value: str, value_type: ValueType) -> None:
        with self.unit() as uow:
            upsert_workspace_setting(
                uow.connection,
                key=key,
                value=value,
                value_type=value_type,
                updated_by=None,
            )

    def expire_by_age(self, value: str = "true") -> None:
        self.setting(RETENTION_EXPIRE_BY_AGE_KEY, value, ValueType.BOOL)

    def count(self, table: Any) -> int:
        with self.reading() as uow:
            return int(
                uow.connection.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()
            )


@pytest.fixture
def recallatron(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Recallatron loaded for the whole test, export **and** restore.

    One ``with`` around both halves rather than two, because the restore path reads
    ``loaded_manifests()`` three times — to accept the artifact's manifest, to run the
    module's migration chain, and to reach its importer — and a module loaded for the
    export and not the restore would make every round trip below refuse for a reason
    that has nothing to do with what it is testing.

    The two retention keys go into the **process-global** settings registry for the
    reason ``test_memory_retention.py``'s own fixture gives: the harness recipe keeps
    the loader's registry local, and the locked retention recheck resolves through the
    global table. The swap-and-restore keeps the leak that recipe guards against from
    happening anyway.
    """
    monkeypatch.setattr(SETTINGS_REGISTRY, "_specs", dict(SETTINGS_REGISTRY._specs))
    monkeypatch.setattr(SETTINGS_REGISTRY, "_origins", dict(SETTINGS_REGISTRY._origins))
    SETTINGS_REGISTRY.register(RETENTION_DAYS_SPEC, origin=_MODULE)
    SETTINGS_REGISTRY.register(RETENTION_EXPIRE_BY_AGE_SPEC, origin=_MODULE)
    register_core_operations()
    with loaded_probe_modules(monkeypatch, _MODULE, deletions=OWNED_DELETIONS):
        yield


def _slug(role: str) -> str:
    """A workspace slug nothing else in the session can collide with.

    Slugs are unique across the control plane and this file provisions two workspaces
    per test, so a literal would make every case after the first fail on
    ``slug_taken`` — a failure about fixture naming that looks like a failure about
    export.
    """
    return f"memory-{role}-{uuid7().hex[:12]}"


def _deployment(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> Deployment:
    bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(bootstrap, WorkspaceContext), bootstrap
    install_and_enable_module(cluster.backend, bootstrap, workspace, _MODULE)
    row = cluster.registry_row(workspace)
    return Deployment(
        cluster=cluster,
        workspace=workspace,
        owner_account_id=owner_account_id,
        database_name=row.database_name,
        engine=cluster.backend.pools.engine_for(row.database_name),
    )


@pytest.fixture
def source(
    recallatron: None,
    cluster: ClusterSession,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
) -> Deployment:
    return _deployment(cluster, make_workspace(slug=_slug("source")), owner_account_id)


@pytest.fixture
def host(
    recallatron: None,
    cluster: ClusterSession,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
) -> WorkspaceContext:
    """A second workspace, only ever used to *dispatch* the restore from.

    A restore names its own workspace in the artifact, so the operation has to be
    called from somewhere else once the source is gone.
    """
    workspace = make_workspace(slug=_slug("host"))
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


# --- seeding --------------------------------------------------------------------------


def _memory(
    *,
    age: timedelta = timedelta(days=1),
    title: str = "a memory",
    audience_kind: str = "workspace",
    audience_id: UUID | None = None,
    origin: str = "told",
    invalidation_reason: str | None = None,
    source_key: tuple[str, str] | None = None,
    recorded_by_id: UUID | None = None,
) -> MemoryRow:
    recorded_at = datetime.now(UTC) - age
    namespace, key = (None, None) if source_key is None else source_key
    return MemoryRow(
        id=uuid7(),
        kind="note",
        title=title,
        body=f"the body of {title}",
        audience_kind=audience_kind,
        audience_id=audience_id,
        confidence=None,
        occurred_at=None,
        recorded_at=recorded_at,
        recorded_by_kind="account" if recorded_by_id is not None else "service",
        recorded_by_id=recorded_by_id,
        origin=origin,
        revision=1,
        corrected_at=None,
        superseded_by_id=None,
        invalidated_at=None if invalidation_reason is None else recorded_at,
        invalidation_reason=invalidation_reason,
        source_namespace=namespace,
        external_source_key=key,
    )


def _receipt(
    *,
    state: str,
    namespace: str,
    key: str,
    record_id: UUID | None,
    producer_kind: str = "migration",
    bound_purpose: str | None = None,
    audience_kind: str = "workspace",
    audience_id: UUID | None = None,
    source_recorded_at: datetime | None = None,
    source_expires_at: datetime | None = None,
) -> SourceReceiptRow:
    return SourceReceiptRow(
        representation_type="memory",
        source_namespace=namespace,
        external_source_key=key,
        record_id=record_id,
        payload_digest=_DIGEST,
        state=state,
        producer_kind=producer_kind,
        authority_id=uuid7(),
        principal_account_id=None,
        audience_kind=audience_kind,
        audience_id=audience_id,
        bound_purpose=bound_purpose,
        source_recorded_at=source_recorded_at,
        source_expires_at=source_expires_at,
    )


def _seed_memory(
    deployment: Deployment,
    row: MemoryRow,
    *,
    purposes: tuple[str, ...] = (_RESPOND,),
    links: tuple[tuple[str, str, bool], ...] = (),
) -> MemoryRow:
    with deployment.unit() as uow:
        insert_memory(uow.connection, row)
        for purpose in purposes:
            insert_memory_purpose(
                uow.connection, MemoryPurposeRow(memory_id=row.id, purpose=purpose)
            )
        for ref, relation, lineage in links:
            insert_memory_link(
                uow.connection,
                MemoryLinkRow(
                    memory_id=row.id,
                    ref=ref,
                    relation=relation,
                    created_at=row.recorded_at,
                    supersession_lineage=lineage,
                ),
            )
    return row


def _one_hot_vector() -> tuple[float, ...]:
    """A unit vector as wide as the stored column. Nothing here reads it back."""
    return (1.0,) + (0.0,) * (EMBEDDING_DIMENSIONS - 1)


def _populate(deployment: Deployment, owner_account_id: UUID) -> dict[str, Any]:
    """One workspace holding every shape the export format has to carry.

    Deliberately wide rather than minimal: the round trip's claim is byte equality on
    *every* category, and a fixture that only held one plain memory would make that
    claim true of a format that dropped five of its six record kinds.
    """
    predecessor = _seed_memory(
        deployment, _memory(title="the superseded one", invalidation_reason=_SUPERSEDED)
    )
    corrected = _seed_memory(
        deployment, _memory(title="the corrected one", invalidation_reason=_CORRECTED)
    )
    replacement = _seed_memory(
        deployment,
        _memory(title="the replacement", origin="derived"),
        purposes=(_RESPOND, AUTOMATIC_BOUND_PURPOSE),
        links=((memory_reference(predecessor.id), _DERIVED_FROM, True),),
    )
    # The supersession pointer, written after the replacement exists — which is the
    # ordering the real supersession path has and the reason restore needs a second
    # pass at all. Without it the marked-ancestry edge above has no chain to walk and
    # the artifact is legitimately refused.
    with deployment.unit() as uow:
        set_memory_successor(uow.connection, predecessor.id, replacement.id)
    predecessor = replace(predecessor, superseded_by_id=replacement.id)
    private = _seed_memory(
        deployment,
        _memory(
            title="a private memory",
            audience_kind="member",
            audience_id=owner_account_id,
        ),
    )
    accepted = _seed_memory(
        deployment,
        _memory(title="an accepted source unit", source_key=("probe", "live")),
    )
    entity = MemoryEntityRow(
        id=uuid7(),
        kind="person",
        name="Ada",
        normalized_name="ada",
        ref=None,
        created_at=datetime.now(UTC),
        source_namespace=None,
        external_source_key=None,
    )
    with deployment.unit() as uow:
        connection = uow.connection
        insert_memory_entity(connection, entity)
        insert_memory_mention(
            connection,
            MemoryMentionRow(
                memory_id=replacement.id, entity_id=entity.id, role="author"
            ),
        )
        # Every receipt state, because § A12 names them together and a restore that
        # lost the terminal ones could replay an accepted unit into a second memory.
        insert_source_receipt(
            connection,
            _receipt(
                state="active",
                namespace="probe",
                key="live",
                record_id=accepted.id,
            ),
        )
        for index, state in enumerate(memory_tables.RECEIPT_STATES[1:]):
            insert_source_receipt(
                connection,
                _receipt(
                    state=state,
                    namespace="probe",
                    key=f"terminal-{index}",
                    record_id=None,
                ),
            )
        insert_memory_embedding(
            connection,
            MemoryEmbeddingRow(
                memory_id=replacement.id,
                model_id="probe-model",
                dimensions=EMBEDDING_DIMENSIONS,
                vector=_one_hot_vector(),
                embedded_at=datetime.now(UTC),
            ),
        )
        # The erasure names a memory that is **gone**, which is what an erasure
        # leaves behind. Pointing it at a row still present in the same workspace
        # would be a ledger that contradicts its own tables, and the importer is
        # right to refuse one.
        insert_deletion_record(
            connection,
            ref=_ref(uuid7()),
            actor_kind="account",
            actor_id=owner_account_id,
            approval_id=uuid7(),
            participants=(_MODULE,),
            invalidated_memory_count=2,
            cause=USER_ERASURE,
            retained_successor_ref=None,
            now=datetime.now(UTC),
        )
        insert_deletion_record(
            connection,
            ref=_ref(uuid7()),
            actor_kind="system",
            actor_id=None,
            approval_id=None,
            participants=(_MODULE,),
            invalidated_memory_count=1,
            cause=RETENTION_EXPIRY,
            retained_successor_ref=memory_reference(replacement.id),
            now=datetime.now(UTC),
        )
    return {
        "predecessor": predecessor,
        "corrected": corrected,
        "replacement": replacement,
        "private": private,
        "accepted": accepted,
        "entity": entity,
    }


def _ref(memory_id: UUID) -> Any:
    from rheo_recallatron.references import canonical_ref

    return canonical_ref(memory_reference(memory_id))


# --- driving the operations -----------------------------------------------------------


def _kinds() -> JobKindRegistry:
    kinds = JobKindRegistry()
    kinds.register(EXPORT_JOB_KIND, ExportJobPayload, run_export_job)
    kinds.register(RESTORE_JOB_KIND, RestoreJobPayload, run_restore_job)
    return kinds


def _visit(cluster: ClusterSession, workspace_id: UUID) -> None:
    visit_workspace(
        DueWorkspace(workspace_id=workspace_id, observed_due_at=None),
        kinds=_kinds(),
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        owner=_OWNER,
        clock=lambda: datetime.now(UTC),
        jitter=None,
    )


def _export(deployment: Deployment) -> Path:
    outcome = dispatch(deployment.context(), WORKSPACE_EXPORT, {})
    assert outcome.state == "pending", outcome
    result: Any = outcome.result
    _visit(deployment.cluster, deployment.workspace)
    path = (
        workspace_dir_for(deployment.workspace, Purpose.EXPORTS)
        / str(result.export_id)
        / f"{result.export_id}.tar.zst"
    )
    assert path.is_file(), "the export published no artifact"
    return path


def _digest(ctx: WorkspaceContext) -> dict[str, Any]:
    outcome = dispatch(ctx, WORKSPACE_DIGEST, {})
    assert outcome.ok, outcome
    result: Any = outcome.result
    return result.model_dump(mode="json")["categories"]


def _remove_workspace(cluster: ClusterSession, workspace_id: UUID) -> None:
    row = cluster.registry_row(workspace_id)
    cluster.backend.pools.dispose_all()
    with cluster.backend.control_engine.begin() as control:
        control.execute(
            delete(control_tables.membership).where(
                control_tables.membership.c.workspace_id == workspace_id
            )
        )
        control.execute(
            delete(control_tables.workspace).where(
                control_tables.workspace.c.id == workspace_id
            )
        )
    with cluster.backend.maintenance_connection() as maintenance:
        quoted = maintenance.dialect.identifier_preparer.quote(row.database_name)
        maintenance.exec_driver_sql(f"DROP DATABASE {quoted} WITH (FORCE)")


def _restore(cluster: ClusterSession, host: WorkspaceContext, artifact: Path) -> None:
    outcome = dispatch(host, WORKSPACE_RESTORE, {"artifact_path": str(artifact)})
    assert outcome.state == "pending", outcome
    _visit(cluster, host.workspace_id)


@contextmanager
def _snapshot(deployment: Deployment) -> Iterator[Any]:
    with UnitOfWork(deployment.engine, deployment.database_name) as worker:
        with collect_export_snapshot(
            deployment.workspace, worker_connection=worker.connection
        ) as view:
            yield view


# --- A12: one snapshot, one instant, no drift between categories ----------------------


def test_one_source_transaction_fixes_the_instant_before_module_collection(
    source: Deployment, owner_account_id: UUID
) -> None:
    """A12 bullet one, now with a real module's rows inside the snapshot.

    The module category is read through the same frozen view as the core six, so a
    memory committed after the snapshot opened is absent from it — and ``snapshot_at``
    is the instant the snapshot's own first statement took, not a clock this
    collection sampled again when it reached the module.
    """
    _populate(source, owner_account_id)
    with _snapshot(source) as view:
        first = view.snapshot_at
        before = module_categories(view)[_CATEGORY]
        late = _seed_memory(source, _memory(title="committed after the snapshot"))
        after = module_categories(view)[_CATEGORY]
        assert view.snapshot_at == first
        assert before == after
        assert str(late.id).encode() not in after
    with _snapshot(source) as later:
        assert str(late.id).encode() in module_categories(later)[_CATEGORY]


def test_a_commit_between_categories_does_not_split_the_archive(
    source: Deployment, owner_account_id: UUID
) -> None:
    """A12 bullet two: pause mid-collection, commit a setting *and* a supersession.

    The pause is deliberate and it is between the **core** categories and the
    **module** one, which is the seam a module bridge adds and the one nothing
    previously covered. Every category still describes the state the snapshot opened
    on, so the archive is one valid source state rather than two spliced halves.
    """
    seeded = _populate(source, owner_account_id)
    with _snapshot(source) as view:
        core = serialised_categories(view)
        source.setting(_LATE_SETTING, "committed-late", ValueType.STR)
        with source.unit() as uow:
            uow.connection.execute(
                delete(memory_tables.memory).where(
                    memory_tables.memory.c.id == seeded["corrected"].id
                )
            )
        module = module_categories(view)[_CATEGORY]
        assert serialised_categories(view) == core
        assert _LATE_SETTING.encode() not in core["settings"]
        assert str(seeded["corrected"].id).encode() in module


# --- A12: the round trip --------------------------------------------------------------


def test_a_populated_workspace_restores_equal_on_every_category_and_its_digest(
    source: Deployment,
    host: WorkspaceContext,
    cluster: ClusterSession,
    owner_account_id: UUID,
) -> None:
    """A12's headline claim, by comparison rather than by inspection.

    Every named category — composition, settings, approvals, operations, audit,
    deletions, and the module's own — is compared, digest included, after the source
    workspace has been physically dropped and rebuilt from its archive alone. The
    deletion-evidence digest is what makes the preserved-verbatim rule observable:
    re-minting a ledger id or a ``deleted_at`` changes these bytes and nothing else
    would notice.
    """
    seeded = _populate(source, owner_account_id)
    artifact = _export(source)
    before = _digest(source.context())
    assert set(before) == {
        "composition",
        "settings",
        "approvals",
        "operations",
        "audit",
        "deletions",
        _CATEGORY,
    }
    assert before["deletions"]["count"] == 2
    assert before[_CATEGORY]["count"] > 0
    source_ledger = _ledger(source)

    _remove_workspace(cluster, source.workspace)
    _restore(cluster, host, artifact)
    restored = context_for_harness(source.workspace, owner_account_id, Role.OWNER)
    assert isinstance(restored, WorkspaceContext), restored

    assert _digest(restored) == before
    rebuilt = replace(
        source,
        engine=cluster.backend.pools.engine_for(source.database_name),
    )
    assert _ledger(rebuilt) == source_ledger
    with rebuilt.reading() as uow:
        rows = {
            row.id: row
            for row in uow.connection.execute(select(memory_tables.memory)).all()
        }
    assert set(rows) == {
        seeded["predecessor"].id,
        seeded["corrected"].id,
        seeded["replacement"].id,
        seeded["private"].id,
        seeded["accepted"].id,
    }
    assert rows[seeded["private"].id].audience_kind == "member"


def test_no_embedding_survives_the_round_trip(
    source: Deployment,
    host: WorkspaceContext,
    cluster: ClusterSession,
    owner_account_id: UUID,
) -> None:
    """Vectors are excluded and rebuildable (1a2), so none may come back.

    The source is seeded with one, so this is an omission observed rather than an
    absence that was never populated: the archive carries no embedding line and the
    restored workspace holds no embedding row.
    """
    _populate(source, owner_account_id)
    assert source.count(memory_tables.memory_embedding) == 1
    artifact = _export(source)
    assert b"probe-model" not in read_archive(artifact)[_CATEGORY]

    _remove_workspace(cluster, source.workspace)
    _restore(cluster, host, artifact)
    rebuilt = replace(
        source, engine=cluster.backend.pools.engine_for(source.database_name)
    )
    assert rebuilt.count(memory_tables.memory_embedding) == 0


def test_retained_history_and_every_receipt_state_round_trip(
    source: Deployment,
    host: WorkspaceContext,
    cluster: ClusterSession,
    owner_account_id: UUID,
) -> None:
    """Non-erasure history and all six receipt states come back unchanged.

    ``memory_tables.RECEIPT_STATES`` rather than a written-out list, so a seventh
    state added to the vocabulary reds here instead of being silently untested.
    """
    _populate(source, owner_account_id)
    artifact = _export(source)
    _remove_workspace(cluster, source.workspace)
    _restore(cluster, host, artifact)
    rebuilt = replace(
        source, engine=cluster.backend.pools.engine_for(source.database_name)
    )
    with rebuilt.reading() as uow:
        states = {
            str(row.state)
            for row in uow.connection.execute(
                select(memory_tables.source_receipt.c.state)
            )
        }
        reasons = {
            row.invalidation_reason
            for row in uow.connection.execute(
                select(memory_tables.memory.c.invalidation_reason)
            )
        }
    assert states == set(memory_tables.RECEIPT_STATES)
    assert {_SUPERSEDED, _CORRECTED} <= reasons
    assert _ERASED not in reasons


def _ledger(deployment: Deployment) -> tuple[DeletionRecordRow, ...]:
    with deployment.reading() as uow:
        return list_deletion_records(uow.connection)


# --- A12 steps 1-4: the validation branches, at the importer's own seam ---------------


@dataclass(frozen=True)
class Importing:
    """A transaction the importer runs in and that is always rolled back."""

    deployment: Deployment

    def run(
        self, rows: tuple[dict[str, Any], ...], ledger: tuple[Any, ...] = ()
    ) -> None:
        with UnitOfWork(self.deployment.engine, self.deployment.database_name) as uow:
            for entry in ledger:
                insert_deletion_record(uow.connection, **entry)
            finish = import_memory_records(uow, rows)
            finish()
            # No commit: every branch below is about what validation *refuses*, and a
            # committed success would leak rows into the next case's workspace.


@pytest.fixture
def importing(source: Deployment) -> Importing:
    with source.unit() as uow:
        uow.connection.execute(delete(memory_tables.memory))
        uow.connection.execute(delete(memory_tables.memory_entity))
        uow.connection.execute(delete(memory_tables.source_receipt))
    return Importing(source)


def _line(**fields: Any) -> dict[str, Any]:
    return {key: _encode(value) for key, value in fields.items()}


def _encode(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _memory_line(row: MemoryRow) -> dict[str, Any]:
    fields = {name: getattr(row, name) for name in MemoryRow.__slots__}
    return _line(record="memory", **fields)


def _purpose_line(memory_id: UUID, purpose: str = _RESPOND) -> dict[str, Any]:
    return _line(record="memory_purpose", memory_id=memory_id, purpose=purpose)


def _link_line(
    memory_id: UUID, predecessor: UUID, *, ref: str | None = None
) -> dict[str, Any]:
    """``ref`` overrides the canonical spelling of ``predecessor``, and only that."""
    return _line(
        record="memory_link",
        memory_id=memory_id,
        ref=memory_reference(predecessor) if ref is None else ref,
        relation=_DERIVED_FROM,
        created_at=datetime.now(UTC),
        supersession_lineage=True,
    )


def _expiry(memory_id: UUID, successor: UUID | None) -> dict[str, Any]:
    return {
        "ref": _ref(memory_id),
        "actor_kind": "system",
        "actor_id": None,
        "approval_id": None,
        "participants": (_MODULE,),
        "invalidated_memory_count": 1,
        "cause": RETENTION_EXPIRY,
        "retained_successor_ref": (
            None if successor is None else memory_reference(successor)
        ),
        "now": datetime.now(UTC),
    }


def _erasure(memory_id: UUID) -> dict[str, Any]:
    return {
        "ref": _ref(memory_id),
        "actor_kind": "system",
        "actor_id": None,
        "approval_id": uuid7(),
        "participants": (_MODULE,),
        "invalidated_memory_count": 1,
        "cause": USER_ERASURE,
        "retained_successor_ref": None,
        "now": datetime.now(UTC),
    }


def test_marked_ancestry_over_a_physically_absent_predecessor_is_accepted(
    importing: Importing,
) -> None:
    """A12 step 4's absent branch: A expired naming B, and B is the replacement.

    The predecessor's row is gone and its expiry evidence is what accounts for it.
    Nothing here is reconstructed from that evidence — it carries no content — it is
    only what tells a legitimate absence from a broken chain.
    """
    absent = uuid7()
    replacement = _memory(title="the replacement", origin="derived")
    importing.run(
        (
            _memory_line(replacement),
            _purpose_line(replacement.id),
            _link_line(replacement.id, absent),
        ),
        ledger=(_expiry(absent, replacement.id),),
    )


def test_marked_ancestry_over_an_unaccounted_predecessor_is_refused(
    importing: Importing,
) -> None:
    """The same artifact with the evidence removed. The false-negative control."""
    absent = uuid7()
    replacement = _memory(title="the replacement", origin="derived")
    with pytest.raises(OperationRefused) as refusal:
        importing.run(
            (
                _memory_line(replacement),
                _purpose_line(replacement.id),
                _link_line(replacement.id, absent),
            )
        )
    assert refusal.value.state == ARTIFACT_INVALID
    assert "no expiry evidence" in str(refusal.value)


def test_a_retained_successor_that_resolves_to_nothing_is_legitimate_history(
    importing: Importing,
) -> None:
    """w2, and the false positive the ratified rule exists to prevent.

    A expires naming B; B is later, separately and validly erased. B's own row and
    B's own ledger row are both genuinely gone, so A's ``retained_successor_ref``
    resolves to nothing — and that is history, not corruption. The artifact restores.
    """
    absent = uuid7()
    departed = uuid7()
    survivor = _memory(title="an unrelated survivor")
    importing.run(
        (_memory_line(survivor), _purpose_line(survivor.id)),
        ledger=(_expiry(absent, departed),),
    )


# --- a malformed reference is the artifact's defect, not the caller's -----------------
#
# Every reference inside an artifact is the *artifact's* content, so a malformed one is
# ``recallatron_artifact_invalid`` — what an operator restoring an archive has to see —
# and never ``input_invalid``, which says the caller's own request was wrong. The
# distinction is easy to lose: the module's ``canonical_ref`` helper exists to turn a
# malformed reference into ``input_invalid`` for the read and write paths, and a
# validation site that reached for it would silently report the caller's request as the
# broken thing. These cases pin the state, which is the part that differs.


def test_a_malformed_link_reference_is_an_artifact_defect(
    importing: Importing,
) -> None:
    """A12 step 1, over a link whose ``ref`` is not canonical.

    The undashed spelling of a real id: ``UUID`` accepts it, the reference grammar does
    not, and an artifact that shipped it would restore a link nothing can resolve.
    """
    predecessor = uuid7()
    replacement = _memory(title="the replacement", origin="derived")
    with pytest.raises(OperationRefused) as refusal:
        importing.run(
            (
                _memory_line(replacement),
                _purpose_line(replacement.id),
                _link_line(replacement.id, predecessor, ref=_undashed(predecessor)),
            )
        )
    assert refusal.value.state == ARTIFACT_INVALID
    assert "not a canonical record reference" in str(refusal.value)


def test_a_malformed_retained_successor_is_an_artifact_defect(
    importing: Importing,
) -> None:
    """The ledger's own w2 check, against a reference that is not one at all.

    The case above it — a successor that resolves to nothing — is legitimate history.
    This one is a chain that was never walkable, and the two answers differ.
    """
    absent = uuid7()
    survivor = _memory(title="an unrelated survivor")
    broken = {**_expiry(absent, None), "retained_successor_ref": "not a reference"}
    with pytest.raises(OperationRefused) as refusal:
        importing.run(
            (_memory_line(survivor), _purpose_line(survivor.id)), ledger=(broken,)
        )
    assert refusal.value.state == ARTIFACT_INVALID
    assert "not a record reference" in str(refusal.value)
    assert str(absent) in str(refusal.value)


def test_a_malformed_successor_further_along_the_ledger_chain_is_refused(
    importing: Importing,
) -> None:
    """The same rule one hop in, where the walk rather than the row reads the reference.

    A expires naming B, and B's own evidence names something that is not a reference.
    The ledger is read oldest first, so the walk out of A reaches B's broken reference
    before the loop ever arrives at B's own row: a second reader of the same column,
    and the one a first-row-only check would leave unguarded.
    """
    first = uuid7()
    second = uuid7()
    survivor = _memory(title="an unrelated survivor")
    broken = {**_expiry(second, None), "retained_successor_ref": "not a reference"}
    with pytest.raises(OperationRefused) as refusal:
        importing.run(
            (_memory_line(survivor), _purpose_line(survivor.id)),
            ledger=(_expiry(first, second), broken),
        )
    assert refusal.value.state == ARTIFACT_INVALID
    assert "not a record reference" in str(refusal.value)
    assert str(second) in str(refusal.value)


# The last two references an artifact can carry are guarded in depth rather than at the
# seam: ``_validate_structure`` parses every link before ancestry sees one, and
# ``_validate_ledger`` parses every retained successor before the expiry hop reads one,
# so no artifact can deliver a malformed reference to either. They are still wrong to
# answer ``input_invalid``, and a later reordering of those checks would make them
# reachable, so each is driven directly.


def test_the_expiry_hop_refuses_a_malformed_successor_as_an_artifact_defect() -> None:
    """``_successor_of``'s absent branch, which reads the ledger rather than a row."""
    absent = uuid7()
    evidence = _deletion_evidence(absent, "not a reference")
    with pytest.raises(OperationRefused) as refusal:
        _successor_of(absent, {}, {absent: evidence})
    assert refusal.value.state == ARTIFACT_INVALID
    assert "malformed successor" in str(refusal.value)


def test_the_ancestry_check_refuses_a_malformed_link_as_an_artifact_defect() -> None:
    """``_validate_ancestry``'s own read of what ``_validate_structure`` parsed."""
    predecessor = uuid7()
    replacement = _memory(title="the replacement", origin="derived")
    link = MemoryLinkRow(
        memory_id=replacement.id,
        ref=_undashed(predecessor),
        relation=_DERIVED_FROM,
        created_at=datetime.now(UTC),
        supersession_lineage=True,
    )
    parsed = _Parsed(
        memories=(replacement,),
        purposes=(),
        entities=(),
        mentions=(),
        links=(link,),
        receipts=(),
    )
    with pytest.raises(OperationRefused) as refusal:
        _validate_ancestry(parsed, {replacement.id: {_RESPOND}}, ())
    assert refusal.value.state == ARTIFACT_INVALID
    assert "not a canonical record reference" in str(refusal.value)


def _undashed(record_id: UUID) -> str:
    """A real memory reference in the one spelling ``RecordRef.parse`` refuses."""
    return memory_reference(record_id).replace("-", "")


def _deletion_evidence(memory_id: UUID, successor_ref: str) -> DeletionRecordRow:
    """One expiry row as the ledger hands it back, built without a database."""
    return DeletionRecordRow(
        id=uuid7(),
        deleted_at=datetime.now(UTC),
        record_type=f"{_MODULE}.{MEMORY_RECORD_TYPE}",
        record_id=memory_id,
        actor_kind="system",
        actor_id=None,
        approval_id=None,
        participants=(_MODULE,),
        cancelled_job_count=0,
        cancelled_action_count=0,
        removed_export_count=0,
        invalidated_memory_count=1,
        cause=RETENTION_EXPIRY,
        retained_successor_ref=successor_ref,
    )


def test_a_superseded_by_pointer_must_resolve_when_it_is_present(
    importing: Importing,
) -> None:
    """A12 step 3's other side, and the branch the case above must not weaken.

    A ``superseded_by_id`` on an imported row names a *present* successor by
    definition — there is no ledger hop for it — so an unresolved one is a broken
    artifact rather than history.
    """
    missing = uuid7()
    predecessor = _memory(title="a predecessor", invalidation_reason=_SUPERSEDED)
    broken = replace(predecessor, superseded_by_id=missing)
    with pytest.raises(OperationRefused) as refusal:
        importing.run((_memory_line(broken), _purpose_line(broken.id)))
    assert refusal.value.state == ARTIFACT_INVALID
    assert "absent successor" in str(refusal.value)


def test_marked_ancestry_descending_from_an_erasure_aborts(
    importing: Importing,
) -> None:
    """Erasure evidence aborts the ancestry check, in either branch (A12 step 4)."""
    erased = uuid7()
    replacement = _memory(title="the replacement", origin="derived")
    with pytest.raises(OperationRefused) as refusal:
        importing.run(
            (
                _memory_line(replacement),
                _purpose_line(replacement.id),
                _link_line(replacement.id, erased),
            ),
            ledger=(_erasure(erased),),
        )
    assert refusal.value.state == ARTIFACT_INVALID
    assert "descends from an erasure" in str(refusal.value)


def test_no_restore_path_revives_erased_content(importing: Importing) -> None:
    """A12 step 1: a memory marked ``source_deleted`` may not carry content.

    The only invalidation reason an artifact may not represent at all. A restore that
    accepted it would be the erasure-revival path every other rule in this file is
    arranged to make unreachable.
    """
    erased = _memory(title="erased content", invalidation_reason=_ERASED)
    with pytest.raises(OperationRefused) as refusal:
        importing.run((_memory_line(erased), _purpose_line(erased.id)))
    assert refusal.value.state == ARTIFACT_INVALID
    assert "reconstructs erased content" in str(refusal.value)


def test_a_memory_with_no_purpose_is_refused(importing: Importing) -> None:
    """A12 step 1's nonempty purpose set, which nothing else in the artifact implies."""
    orphan = _memory(title="a memory with no purpose")
    with pytest.raises(OperationRefused) as refusal:
        importing.run((_memory_line(orphan),))
    assert refusal.value.state == ARTIFACT_INVALID
    assert "carry no purpose" in str(refusal.value)


def test_a_terminal_receipt_beside_a_live_representation_is_refused(
    importing: Importing,
) -> None:
    """A12 step 2: a refused source and an accepted one cannot be the same key."""
    live = _memory(title="a live representation", source_key=("probe", "one"))
    receipt = _receipt(state="denied", namespace="probe", key="one", record_id=None)
    with pytest.raises(OperationRefused) as refusal:
        importing.run(
            (
                _memory_line(live),
                _purpose_line(live.id),
                _line(record="source_receipt", **_receipt_fields(receipt)),
            )
        )
    assert refusal.value.state == ARTIFACT_INVALID
    assert "accompanies a live representation" in str(refusal.value)


def test_an_automatic_active_receipt_may_not_widen_what_it_accepted(
    importing: Importing,
) -> None:
    """§ A5's automatic shape, rechecked on the way in (A12 step 2).

    The memory is ``told`` where an automatically accepted one must be ``derived``,
    so the artifact claims provenance the acceptance path would never have written.
    """
    recorded_at = datetime.now(UTC) - timedelta(hours=1)
    accepted = replace(
        _memory(title="an automatic memory", source_key=("probe", "auto")),
        recorded_at=recorded_at,
    )
    receipt = _receipt(
        state="active",
        namespace="probe",
        key="auto",
        record_id=accepted.id,
        producer_kind="rheo_runtime",
        bound_purpose=AUTOMATIC_BOUND_PURPOSE,
        source_recorded_at=recorded_at,
        source_expires_at=recorded_at + timedelta(days=1),
    )
    with pytest.raises(OperationRefused) as refusal:
        importing.run(
            (
                _memory_line(accepted),
                _purpose_line(accepted.id, AUTOMATIC_BOUND_PURPOSE),
                _line(record="source_receipt", **_receipt_fields(receipt)),
            )
        )
    assert refusal.value.state == ARTIFACT_INVALID
    assert "claims origin" in str(refusal.value)


def _receipt_fields(row: SourceReceiptRow) -> dict[str, Any]:
    return {name: getattr(row, name) for name in SourceReceiptRow.__slots__}


# --- A12 step 5: atomicity ------------------------------------------------------------


def test_a_failure_in_the_second_pass_imports_nothing_and_activates_nothing(
    source: Deployment,
    host: WorkspaceContext,
    cluster: ClusterSession,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A12 step 5: parents are in, the ledger is in, then the second pass raises.

    The failure is injected at the point the ordering exists for, which is the point
    at which the most is already written: memory parents, entities, receipts and the
    whole deletion ledger are in the transaction when it raises. Nothing survives, and
    the workspace stays ``restoring`` rather than being activated over a half import.
    """
    _populate(source, owner_account_id)
    artifact = _export(source)
    _remove_workspace(cluster, source.workspace)

    real = artifact_module._import_modules

    def failing(uow: Any, entries: Any, manifest: Any) -> Any:
        continuations = real(uow, entries, manifest)
        assert continuations, "the artifact carried no module importer to fail"

        def explode() -> None:
            raise RuntimeError("second pass failed")

        return [*continuations, explode]

    monkeypatch.setattr(artifact_module, "_import_modules", failing)
    outcome = dispatch(host, WORKSPACE_RESTORE, {"artifact_path": str(artifact)})
    assert outcome.state == "pending", outcome
    _visit(cluster, host.workspace_id)

    with cluster.backend.control_engine.connect() as control:
        row = get_workspace(control, source.workspace)
    assert row is not None
    assert row.state is WorkspaceState.RESTORING
    rebuilt = replace(
        source, engine=cluster.backend.pools.engine_for(row.database_name)
    )
    assert rebuilt.count(deletion_record) == 0
    # The module's tables are not empty — they are **absent**. Restore builds the
    # module schema inside the same transaction as its rows, so a second-pass failure
    # takes the DDL down with the data, which is a stronger reading of "leaves no
    # imported memory" than an empty table would be.
    with rebuilt.reading() as uow:
        relation = uow.connection.execute(
            text("SELECT to_regclass('recallatron.memory')")
        ).scalar_one_or_none()
    assert relation is None


# --- A12: the diagnosable refusal -----------------------------------------------------


def test_export_requires_retention_sweep_is_diagnosable_and_not_per_caller(
    source: Deployment, owner_account_id: UUID
) -> None:
    """The refusal carries a bounded count and the sweep schedule's own state.

    Three claims in one place because they are one behaviour. The count is of
    **workspace-audience** roots past the snapshot's horizon — the equally old
    member-audience row beside them is outside the mechanism entirely and is not
    counted. The schedule fields name the row and the job an operator would otherwise
    correlate by hand. And the figure is not a derivative of any caller: the exporter
    takes no context, and the number the dispatched operation refuses with is the same
    number the exporter produces on its own.
    """
    _seed_memory(source, _memory(age=_EXPIRED_AGE, title="an old workspace memory"))
    _seed_memory(source, _memory(age=_EXPIRED_AGE, title="another old one"))
    _seed_memory(
        source,
        _memory(
            age=_EXPIRED_AGE,
            title="an old private memory",
            audience_kind="member",
            audience_id=owner_account_id,
        ),
    )
    source.expire_by_age()

    with _snapshot(source) as view:
        with pytest.raises(ExportRequiresRetentionSweep) as refusal:
            export_memory_records(view)
    diagnostic = refusal.value.diagnostic
    assert refusal.value.state == EXPORT_REQUIRES_RETENTION_SWEEP
    assert diagnostic.expired_root_count == 2
    assert diagnostic.count_is_bounded is False
    assert diagnostic.schedule.schedule_id is not None
    assert diagnostic.schedule.enabled is True
    assert diagnostic.schedule.next_run_at is not None

    outcome = dispatch(source.context(), WORKSPACE_DIGEST, {})
    assert outcome.state == EXPORT_REQUIRES_RETENTION_SWEEP, outcome
    assert outcome.error is not None
    reported = outcome.error.error_text
    assert "2 workspace-audience memories" in reported
    assert str(diagnostic.schedule.schedule_id) in reported


def test_the_refusal_is_unreachable_while_the_gate_is_off(
    source: Deployment, owner_account_id: UUID
) -> None:
    """The default posture, and the control for the case above.

    A workspace holding memories older than the package window exports cleanly while
    ``expire_by_age`` is off, because off means there is no horizon at all rather
    than a horizon nothing enforces.
    """
    _seed_memory(source, _memory(age=_EXPIRED_AGE, title="an old workspace memory"))
    with _snapshot(source) as view:
        assert module_categories(view)[_CATEGORY]


def test_the_pool_capacity_refusal_precedes_a_real_module_export(
    source: Deployment,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P12's three-connection floor, now with a module category in the export path.

    Re-exercised here rather than taken on trust from P12: the reason the floor is
    three is the worker transaction, the snapshot and a heartbeat, and a module
    exporter that opened a connection of its own would make it four without anything
    else noticing. **Both sides are asserted**, one below the floor and one exactly on
    it: the refusal lands before the pool is asked for the workspace's engine at all,
    and a collection that includes the module's rows then completes at exactly
    ``MINIMUM_SNAPSHOT_CONNECTIONS``. Asserting only the refusal would leave the
    number itself untested and a module that quietly took a fourth connection would
    surface as an exhausted pool in production rather than here.
    """
    _populate(source, owner_account_id)
    for pool_size in (
        artifact_module.MINIMUM_SNAPSHOT_CONNECTIONS - 1,
        artifact_module.MINIMUM_SNAPSHOT_CONNECTIONS,
    ):
        with UnitOfWork(source.engine, source.database_name) as worker:
            small = EnginePool(
                source.cluster.backend.pools.cluster_url,
                cache_size=2,
                pool_size=pool_size,
                idle_close_seconds=300.0,
                reserved_connections=0,
            )
            monkeypatch.setattr(source.cluster.backend, "pools", small)
            try:
                if pool_size < artifact_module.MINIMUM_SNAPSHOT_CONNECTIONS:
                    with pytest.raises(OperationRefused) as refusal:
                        with collect_export_snapshot(
                            source.workspace, worker_connection=worker.connection
                        ):
                            pass
                    assert refusal.value.state == EXPORT_RESOURCE_UNAVAILABLE
                    assert small.cached() == ()
                    continue
                with collect_export_snapshot(
                    source.workspace, worker_connection=worker.connection
                ) as view:
                    assert module_categories(view)[_CATEGORY]
                    assert serialised_categories(view)["deletions"]
            finally:
                small.dispose_all()


# --- § A8: the owner/operator-only deletions collection on core.audit.list ------------


def test_the_audit_read_publishes_deletion_evidence_only_to_owner_and_operator(
    source: Deployment, owner_account_id: UUID
) -> None:
    """§ A8's opt-in ``deletions`` collection, and its gate at the point of disclosure.

    The exact counters live on the ledger and in this collection and nowhere else —
    a caller of the deletion operation receives only ``{deletion_ref}`` — so this is
    the one surface that publishes closure sizes, and it publishes them to an owner or
    an operator who asked. ``None`` rather than ``[]`` for a read that did not
    collect: an empty list would tell a member that nothing has ever been deleted.
    """
    _populate(source, owner_account_id)
    silent: Any = dispatch(source.context(), AUDIT_LIST, {"limit": 50}).result
    assert silent.deletions is None

    opted: Any = dispatch(
        source.context(), AUDIT_LIST, {"limit": 50, "include_deletions": True}
    ).result
    assert opted.deletions is not None
    assert len(opted.deletions) == 2
    published = {row.cause: row for row in opted.deletions}
    assert set(published) == {USER_ERASURE, RETENTION_EXPIRY}
    assert published[USER_ERASURE].invalidated_memory_count == 2
    assert published[USER_ERASURE].record_ref.startswith(f"{_MODULE}.memory:")
    assert published[RETENTION_EXPIRY].retained_successor_ref is not None

    # And the gate at the point of disclosure, reached directly, so the operation's
    # own owner/operator roles are not the only thing standing between a member and
    # the ledger's exact counters.
    member_id = add_member(
        source.cluster.backend,
        source.workspace,
        Role.MEMBER,
        display_name="a member who may not read deletion evidence",
    )
    member = context_for_harness(source.workspace, member_id, Role.MEMBER)
    assert isinstance(member, WorkspaceContext), member
    refused = dispatch(member, AUDIT_LIST, {"limit": 50, "include_deletions": True})
    assert refused.state == "role_not_permitted", refused
    with source.reading() as uow:
        published = audit_list_handler(
            member, uow, AuditListInput(limit=50, include_deletions=True)
        )
    assert published.deletions is None


def test_the_deletion_evidence_category_is_deterministic(
    source: Deployment, owner_account_id: UUID
) -> None:
    """The bytes the round-trip digest rests on, read twice and compared.

    Two collections over one unchanged workspace produce identical bytes, and the
    order is the ledger's own ascending id rather than whatever the planner returns —
    which is the property that makes the digest comparable at all.
    """
    _populate(source, owner_account_id)
    with _snapshot(source) as first:
        once = serialised_categories(first)["deletions"]
    with _snapshot(source) as second:
        twice = serialised_categories(second)["deletions"]
    assert once == twice
    assert once.count(b"\n") == 2
    ordered = [row.id for row in _ledger(source)]
    assert ordered == sorted(ordered)
    assert str(ordered[0]).encode() in once.splitlines()[0]
