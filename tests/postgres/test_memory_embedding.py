"""Recallatron's embedding pipeline against real workspaces: the providers, the enqueue
gate, the embed job's outcomes, and the one-job rebuild.

Seams under test: ``LocalEmbeddingProvider.embed`` on the real model (AC 4 — 384 wide,
unit length, no socket once loaded); the provider registry's two distinct failures (a
missing extra degrades, a wrong width raises); ``enqueue_embed_job`` at both of its call
sites under the shipped default (no job for ``remember``, ``derive``, ``correct`` or
``supersede``); the embed job's skip, no-op and raise outcomes, driven through a real
worker visit; ``recallatron.embedding.rebuild``'s prune, fill and committed progress
(AC 12), its version prune and its refusal; ``fill_missing_embeddings`` called on its
own; and per-workspace coverage (AC 14).

**One way to select a provider, everywhere.** A test rebinds
``registry.configured_provider_name`` (``monkeypatch`` undoes it). The ``RHEO__``
variable path kills the session at import, and declaring the key in the process-wide
settings registry would leave it declared for every later test.

**The real-provider tests fail, never skip, without the ``local-embeddings`` extra.**
Their failure message names the command that installs it.

**Queued jobs are enqueued directly.** The strategy key offers ``lexical`` alone in
this phase, so the enqueue helper cannot fire yet; these tests write the job row the
helper would have written and drive it through ``visit_workspace``, exactly as the
worker would.
"""

from __future__ import annotations

import math
import socket
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import register_resolver
from rheo_core.storage import work_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.work.jobs import enqueue_job, list_failed_jobs
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_recallatron import MANIFEST
from rheo_recallatron.configuration import (
    EMBED_INPUT_VERSION,
    EMBED_JOB_KIND,
    EMBED_MAX_ATTEMPTS,
    EMBEDDING_DIMENSIONS,
    MEMORY_RECORD_TYPE,
    REBUILD_JOB_KIND,
    STRATEGY_LEXICAL,
)
from rheo_recallatron.contracts import MemoryCorrected, MemorySuperseded, MemoryWritten
from rheo_recallatron.eligibility import memory_reference
from rheo_recallatron.embedding import embed_input, registry
from rheo_recallatron.embedding import rebuild as rebuild_module
from rheo_recallatron.embedding.fake import FakeEmbeddingProvider
from rheo_recallatron.embedding.local import (
    MODEL_ID,
    UNIT_NORM_TOLERANCE,
    LocalEmbeddingProvider,
)
from rheo_recallatron.embedding.operations import (
    EMBEDDING_REBUILD,
    EmbeddingRebuildScheduled,
)
from rheo_recallatron.embedding.protocol import EmbeddingProvider
from rheo_recallatron.embedding.rebuild import fill_batch, fill_missing_embeddings
from rheo_recallatron.operations import (
    MEMORY_CORRECT,
    MEMORY_DERIVE,
    MEMORY_REMEMBER,
    MEMORY_SUPERSEDE,
)
from rheo_recallatron.references import canonical_ref
from rheo_recallatron.refusals import EMBEDDING_PROVIDER_UNAVAILABLE
from rheo_recallatron.resolvers import resolve_memory
from rheo_recallatron.retrieval.dense import DenseStrategy
from rheo_recallatron.retrieval.dispatch import resolve_strategy
from rheo_recallatron.retrieval.protocol import IndexItem
from rheo_recallatron.storage import tables as memory_tables
from rheo_recallatron.storage.repository import (
    EmbeddingCoverage,
    MemoryEmbeddingRow,
    MemoryPurposeRow,
    MemoryRow,
    delete_memory,
    embedding_coverage,
    get_embedding_state,
    get_memory,
    insert_memory,
    insert_memory_embedding,
    insert_memory_purpose,
    invalidate_memory,
    set_embedding_state,
)
from sqlalchemy import ColumnElement, Engine, Row, and_, func, select

pytestmark = pytest.mark.postgres

_MEMORY_MODULE = MANIFEST.module_id
_OWNER = "memory-embedding-test"
_RESPOND = "respond"
_RETIRED_MODEL = "retired/embedding-model"
_EXTRA_REMEDY = "uv sync --frozen --extra local-embeddings"


# --- the workspace --------------------------------------------------------------------


@dataclass(frozen=True)
class Stored:
    """One stored embedding row as the database hands it back; the vector in text."""

    dimensions: int
    vector: str
    embedded_at: datetime


@dataclass(frozen=True)
class EmbeddingWorkspace:
    """A workspace with Recallatron installed, enabled and migrated to 0003."""

    cluster: ClusterSession
    workspace: UUID
    owner_account_id: UUID
    surfaces: LoadedSurfaces
    database_name: str
    engine: Engine
    consumers: ConsumerRegistry

    def context(self) -> WorkspaceContext:
        ctx = context_for_harness(self.workspace, self.owner_account_id, Role.OWNER)
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

    def call(self, name: str, payload: dict[str, object]) -> OperationOutcome:
        return dispatch(
            self.context(),
            name,
            payload,
            registry=self.surfaces.operations,
            consumers=self.consumers,
        )

    def visit(self, *, at: datetime) -> None:
        """One real worker visit at a fixed instant, with every job kind the module
        declares registered — taken off the manifest, so a kind it stops declaring
        fails here as unknown rather than passing on a registration of this file's."""
        kinds = JobKindRegistry()
        for job in MANIFEST.jobs:
            kinds.register(job.name, job.input_model, job.handler)
        visit_workspace(
            DueWorkspace(workspace_id=self.workspace, observed_due_at=None),
            kinds=kinds,
            consumers=ConsumerRegistry(),
            backend=self.cluster.backend,
            owner=_OWNER,
            clock=lambda: at,
            jitter=None,
        )

    def jobs_of(self, kind: str) -> list[Row[Any]]:
        with self.reading() as uow:
            return list(
                uow.connection.execute(
                    select(work_tables.job)
                    .where(work_tables.job.c.kind == kind)
                    .order_by(work_tables.job.c.created_at)
                )
            )

    def failed_kinds(self) -> tuple[str, ...]:
        """What the core failure list shows, read through its own repository."""
        with self.reading() as uow:
            return tuple(
                row.kind for row in list_failed_jobs(uow.connection, limit=500)
            )

    def operation(self, operation_id: UUID) -> Row[Any]:
        with self.reading() as uow:
            return uow.connection.execute(
                select(work_tables.operation).where(
                    work_tables.operation.c.id == operation_id
                )
            ).one()

    def vectors(self) -> dict[tuple[UUID, str], Stored]:
        """Every stored embedding row, keyed by ``(memory, model)``."""
        with self.reading() as uow:
            rows = uow.connection.execute(select(memory_tables.memory_embedding))
            return {
                (row.memory_id, str(row.model_id)): Stored(
                    dimensions=int(row.dimensions),
                    vector=str(row.vector),
                    embedded_at=row.embedded_at,
                )
                for row in rows
            }

    def live_count(self) -> int:
        """``count(live memories)``, by query — the expectation a fill is held to."""
        with self.reading() as uow:
            return int(
                uow.connection.execute(
                    select(func.count())
                    .select_from(memory_tables.memory)
                    .where(_live())
                ).scalar_one()
            )

    def live_lacking(self, model_id: str) -> int:
        """Live memories with no row for ``model_id``, by query."""
        embedding = memory_tables.memory_embedding
        has_row = (
            select(embedding.c.memory_id)
            .where(
                embedding.c.memory_id == memory_tables.memory.c.id,
                embedding.c.model_id == model_id,
            )
            .exists()
        )
        with self.reading() as uow:
            return int(
                uow.connection.execute(
                    select(func.count())
                    .select_from(memory_tables.memory)
                    .where(_live(), ~has_row)
                ).scalar_one()
            )

    def coverage(self, model_id: str) -> EmbeddingCoverage:
        with self.reading() as uow:
            return embedding_coverage(uow.connection, model_id=model_id)


def _live() -> ColumnElement[bool]:
    memory = memory_tables.memory
    return and_(memory.c.invalidated_at.is_(None), memory.c.superseded_by_id.is_(None))


def _enabled(
    cluster: ClusterSession,
    surfaces: LoadedSurfaces,
    workspace: UUID,
    owner_account_id: UUID,
) -> EmbeddingWorkspace:
    bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(bootstrap, WorkspaceContext), bootstrap
    install_and_enable_module(cluster.backend, bootstrap, workspace, _MEMORY_MODULE)
    row = cluster.registry_row(workspace)
    return EmbeddingWorkspace(
        cluster=cluster,
        workspace=workspace,
        owner_account_id=owner_account_id,
        surfaces=surfaces,
        database_name=row.database_name,
        engine=cluster.backend.pools.engine_for(row.database_name),
        consumers=ConsumerRegistry(),
    )


@dataclass(frozen=True)
class Loaded:
    """The fixture's workspace, plus what enabling a second one needs."""

    first: EmbeddingWorkspace
    surfaces: LoadedSurfaces


@pytest.fixture
def loaded(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[Loaded]:
    """Recallatron loaded through the harness's one recipe, enabled in one workspace.

    The memory resolver goes on the process-wide table as well, which is where a real
    deployment's loader puts it and where the lifecycle paths resolve references.
    """
    register_core_operations()
    register_resolver(
        _MEMORY_MODULE, MEMORY_RECORD_TYPE, resolve_memory, origin=_MEMORY_MODULE
    )
    with loaded_probe_modules(monkeypatch, _MEMORY_MODULE) as surfaces:
        yield Loaded(
            first=_enabled(cluster, surfaces, workspace, owner_account_id),
            surfaces=surfaces,
        )


@pytest.fixture
def embedding(loaded: Loaded) -> EmbeddingWorkspace:
    return loaded.first


# --- seeding and selecting ------------------------------------------------------------


def _select(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """The one mechanism: rebind the name reader, undone by ``monkeypatch``."""
    monkeypatch.setattr(registry, "configured_provider_name", lambda: name)


def _install(
    monkeypatch: pytest.MonkeyPatch, name: str, provider: EmbeddingProvider
) -> None:
    """Register a provider the built-ins cannot produce, removed again at teardown.

    ``setitem`` first so ``monkeypatch`` records that the name was absent and deletes it
    on undo; ``register_provider`` then applies the width check a real registration
    gets.
    """
    monkeypatch.setitem(registry.PROVIDERS, name, provider)
    registry.register_provider(name, provider)


def _fake() -> FakeEmbeddingProvider:
    provider = registry.PROVIDERS.get(registry.FAKE_PROVIDER)
    assert isinstance(provider, FakeEmbeddingProvider), (
        "the fake provider registers only under the test profile"
    )
    return provider


def _row(title: str, *, invalidated: bool = False) -> MemoryRow:
    now = datetime.now(UTC) - timedelta(hours=1)
    return MemoryRow(
        id=uuid7(),
        kind="note",
        title=title,
        body=f"the body of a note about {title}",
        audience_kind="workspace",
        audience_id=None,
        confidence=None,
        occurred_at=None,
        recorded_at=now,
        recorded_by_kind="account",
        recorded_by_id=None,
        origin="told",
        revision=2 if invalidated else 1,
        corrected_at=None,
        superseded_by_id=None,
        invalidated_at=now if invalidated else None,
        invalidation_reason="source_corrected" if invalidated else None,
        source_namespace=None,
        external_source_key=None,
    )


def _seed(
    ws: EmbeddingWorkspace, *titles: str, invalidated: bool = False
) -> list[UUID]:
    """Memories written straight through the repository, as a synthetic fixture does."""
    ids: list[UUID] = []
    with ws.unit() as uow:
        for title in titles:
            row = _row(title, invalidated=invalidated)
            insert_memory(uow.connection, row)
            insert_memory_purpose(
                uow.connection, MemoryPurposeRow(memory_id=row.id, purpose=_RESPOND)
            )
            ids.append(row.id)
    return ids


def _item(ws: EmbeddingWorkspace, memory_id: UUID) -> IndexItem:
    with ws.reading() as uow:
        row = get_memory(uow.connection, memory_id)
    assert row is not None
    return IndexItem(row.id, row.title, row.body)


def _one_hot() -> tuple[float, ...]:
    return (1.0,) + (0.0,) * (EMBEDDING_DIMENSIONS - 1)


def _enqueue_embed(ws: EmbeddingWorkspace, *memory_ids: UUID, now: datetime) -> None:
    """The row the enqueue helper writes, written directly (the module docstring says
    why)."""
    with ws.unit() as uow:
        for memory_id in memory_ids:
            enqueue_job(
                uow.connection,
                kind=EMBED_JOB_KIND,
                payload={"memory_id": str(memory_id)},
                now=now,
                max_attempts=EMBED_MAX_ATTEMPTS,
            )


def _norm(vector: Sequence[float]) -> float:
    return math.sqrt(math.fsum(value * value for value in vector))


class _SocketRefused(Exception):
    pass


def _refuse_sockets(*_args: object, **_kwargs: object) -> socket.socket:
    raise _SocketRefused("a socket was opened")


class _RaisingProvider:
    """A configured provider that fails: what a working model cannot do on demand."""

    model_id = "rheo-test/raising"
    dimensions = EMBEDDING_DIMENSIONS

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        raise RuntimeError("the embedding provider is down")


class _NarrowProvider:
    model_id = "rheo-test/narrow"
    dimensions = 3

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [(1.0, 0.0, 0.0) for _ in texts]


# --- AC 4: the real provider ----------------------------------------------------------


def _local(monkeypatch: pytest.MonkeyPatch) -> LocalEmbeddingProvider:
    _select(monkeypatch, registry.LOCAL_PROVIDER)
    provider = registry.resolve_provider()
    assert isinstance(provider, LocalEmbeddingProvider), (
        "the local embedding provider is not registered, so the local-embeddings "
        f"extra is not installed: run `{_EXTRA_REMEDY}`"
    )
    return provider


def test_the_local_model_identifier_is_one_the_installed_runtime_supports() -> None:
    """The identifier was confirmed against the installed package, and stays so."""
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:  # pragma: no cover - the failure this test reports
        raise AssertionError(
            f"the local-embeddings extra is not installed: run `{_EXTRA_REMEDY}`"
        ) from exc
    supported = {
        str(model["model"]): int(model["dim"])
        for model in TextEmbedding.list_supported_models()
    }
    assert supported.get(MODEL_ID) == EMBEDDING_DIMENSIONS


def test_the_local_provider_embeds_unit_vectors_and_opens_no_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 4. The no-socket guard is armed **after** the lazy load: armed before it, a
    cold cache would trip it on the artifact download, a reason this test does not
    guard."""
    provider = _local(monkeypatch)
    assert provider.model_id == MODEL_ID
    assert provider.dimensions == EMBEDDING_DIMENSIONS

    texts = [embed_input("apples", "a note about apples"), "apples"]
    loaded = provider.embed(texts[:1])

    monkeypatch.setattr(socket, "socket", _refuse_sockets)
    with pytest.raises(_SocketRefused):
        socket.socket()
    vectors = provider.embed(texts)

    assert len(vectors) == len(texts)
    for vector in vectors:
        assert len(vector) == EMBEDDING_DIMENSIONS
        assert all(isinstance(value, float) for value in vector)
        assert abs(_norm(vector) - 1.0) <= UNIT_NORM_TOLERANCE
    # The same text answers the same direction whichever batch it arrives in.
    assert math.fsum(a * b for a, b in zip(loaded[0], vectors[0], strict=True)) > 0.9999


def test_a_missing_extra_registers_no_local_provider_and_a_wrong_width_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two different failures, kept apart: a missing extra is a lexical install and
    degrades to no provider; a provider of the wrong width is a packaging error."""
    # The control: with the extra present, the built-in registration took ``local``.
    assert isinstance(
        registry.PROVIDERS.get(registry.LOCAL_PROVIDER), LocalEmbeddingProvider
    ), f"the local-embeddings extra is not installed: run `{_EXTRA_REMEDY}`"

    monkeypatch.setitem(sys.modules, "fastembed", None)
    monkeypatch.setattr(registry, "PROVIDERS", {})
    registry.register_builtin_providers()
    assert registry.LOCAL_PROVIDER not in registry.PROVIDERS
    # Registration did run: the test profile's fake is there.
    assert isinstance(
        registry.PROVIDERS.get(registry.FAKE_PROVIDER), FakeEmbeddingProvider
    )
    _select(monkeypatch, registry.LOCAL_PROVIDER)
    assert registry.resolve_provider() is None

    with pytest.raises(ValueError, match="3 dimensions"):
        registry.register_provider("narrow", _NarrowProvider())
    assert "narrow" not in registry.PROVIDERS


# --- the shipped default enqueues nothing ---------------------------------------------


def _written(outcome: OperationOutcome) -> MemoryWritten:
    assert outcome.ok, outcome
    assert isinstance(outcome.result, MemoryWritten), outcome
    return outcome.result


def _remember(ws: EmbeddingWorkspace, title: str) -> MemoryWritten:
    return _written(
        ws.call(
            MEMORY_REMEMBER,
            {
                "kind": "note",
                "title": title,
                "body": f"a note about {title}",
                "purposes": [_RESPOND],
            },
        )
    )


def _write_with(ws: EmbeddingWorkspace, writer: str) -> tuple[UUID, str]:
    """Drive one writer; answer the memory it produced and the title it now holds."""
    if writer == "remember":
        written = _remember(ws, "apples")
        return canonical_ref(written.ref).id, "apples"
    if writer == "derive":
        source = _remember(ws, "apples")
        derived = _written(
            ws.call(
                MEMORY_DERIVE,
                {
                    "kind": "summary",
                    "title": "fruit",
                    "body": "a summary of the apples",
                    "sources": [source.ref],
                },
            )
        )
        return canonical_ref(derived.ref).id, "fruit"
    target = _remember(ws, "apples")
    if writer == "correct":
        outcome = ws.call(
            MEMORY_CORRECT,
            {
                "ref": target.ref,
                "expected_revision": target.revision,
                "title": "pears",
                "body": "it was pears all along",
            },
        )
        assert outcome.ok, outcome
        assert isinstance(outcome.result, MemoryCorrected), outcome
        return canonical_ref(outcome.result.ref).id, "pears"
    assert writer == "supersede", writer
    outcome = ws.call(
        MEMORY_SUPERSEDE,
        {
            "ref": target.ref,
            "expected_revision": target.revision,
            "kind": "note",
            "title": "pears",
            "body": "what the record says now",
        },
    )
    assert outcome.ok, outcome
    assert isinstance(outcome.result, MemorySuperseded), outcome
    return canonical_ref(outcome.result.replacement.ref).id, "pears"


@pytest.mark.parametrize("writer", ["remember", "derive", "correct", "supersede"])
def test_the_shipped_default_enqueues_no_embed_job(
    embedding: EmbeddingWorkspace, writer: str
) -> None:
    """No provider resolves under the harness, so no writer queues an embed job.

    ``correct`` is its own case because it has its own call site: it rewrites a row in
    place and never reaches the insert the other three share. The memory the write
    produced is asserted present in the same test, so an absent job row is evidence of
    the gate and not of a write that never happened. The positive control — a job row
    when a provider and a dense-reading strategy are configured — is the next phase's,
    once ``dense`` and ``hybrid`` are offered.
    """
    assert registry.configured_provider_name() == "none"
    with embedding.reading() as uow:
        assert resolve_strategy(embedding.context(), uow).name == STRATEGY_LEXICAL

    memory_id, title = _write_with(embedding, writer)

    with embedding.reading() as uow:
        row = get_memory(uow.connection, memory_id)
    assert row is not None and row.title == title
    if writer == "correct":
        assert row.revision == 2
    assert embedding.jobs_of(EMBED_JOB_KIND) == []


# --- the embed job's outcomes ---------------------------------------------------------


def test_a_provider_gone_before_the_job_runs_succeeds_having_written_nothing(
    embedding: EmbeddingWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    (memory_id,) = _seed(embedding, "apples")
    _select(monkeypatch, registry.FAKE_PROVIDER)
    now = datetime.now(UTC)
    _enqueue_embed(embedding, memory_id, now=now)

    _select(monkeypatch, "none")
    embedding.visit(at=now + timedelta(seconds=1))

    (job,) = embedding.jobs_of(EMBED_JOB_KIND)
    assert job.state == "succeeded", job
    assert embedding.vectors() == {}
    assert EMBED_JOB_KIND not in embedding.failed_kinds()


def test_a_provider_that_raises_exhausts_its_attempts_into_the_failure_list(
    embedding: EmbeddingWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured provider that fails is a real failure: ordinary retry and backoff,
    then the core failure list. Each visit is an hour after the last, past every
    backoff step and well short of the day at which the enabled schedules fall due."""
    (memory_id,) = _seed(embedding, "apples")
    _install(monkeypatch, "raising", _RaisingProvider())
    _select(monkeypatch, "raising")
    start = datetime.now(UTC)
    _enqueue_embed(embedding, memory_id, now=start)

    embedding.visit(at=start + timedelta(seconds=1))
    (job,) = embedding.jobs_of(EMBED_JOB_KIND)
    assert (job.state, job.attempts) == ("queued", 1), job
    for attempt in range(1, EMBED_MAX_ATTEMPTS):
        embedding.visit(at=start + timedelta(hours=attempt, seconds=1))

    (job,) = embedding.jobs_of(EMBED_JOB_KIND)
    assert (job.state, job.attempts) == ("failed", EMBED_MAX_ATTEMPTS), job
    assert "the embedding provider is down" in str(job.last_error)
    assert embedding.failed_kinds() == (EMBED_JOB_KIND,)
    assert embedding.vectors() == {}


def test_a_memory_that_already_has_its_row_is_left_as_it_is(
    embedding: EmbeddingWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rebuild got there first. The queued job runs to a no-op, not a key clash."""
    fake = _fake()
    (memory_id,) = _seed(embedding, "apples")
    with embedding.unit() as uow:
        DenseStrategy().index_many(uow, [_item(embedding, memory_id)], provider=fake)
    before = embedding.vectors()
    _select(monkeypatch, registry.FAKE_PROVIDER)
    now = datetime.now(UTC)
    _enqueue_embed(embedding, memory_id, now=now)

    embedding.visit(at=now + timedelta(seconds=1))

    (job,) = embedding.jobs_of(EMBED_JOB_KIND)
    assert job.state == "succeeded", job
    assert embedding.vectors() == before
    assert set(before) == {(memory_id, fake.model_id)}


@pytest.mark.parametrize("touch", ["invalidated", "deleted"])
def test_a_memory_retired_before_its_job_runs_is_skipped_beside_a_sibling_that_is_not(
    embedding: EmbeddingWorkspace, monkeypatch: pytest.MonkeyPatch, touch: str
) -> None:
    """Two memories, both queued; one is retired before the drain. The untouched one
    gets its row — without it, "no row" could mean a job that never ran — and the
    skipped job ends as a success, not a failure."""
    fake = _fake()
    kept, retired = _seed(embedding, "apples", "pears")
    _select(monkeypatch, registry.FAKE_PROVIDER)
    now = datetime.now(UTC)
    _enqueue_embed(embedding, kept, retired, now=now)
    with embedding.unit() as uow:
        if touch == "invalidated":
            invalidate_memory(
                uow.connection,
                retired,
                reason="source_corrected",
                invalidated_at=now,
                revision=2,
            )
        else:
            assert delete_memory(uow.connection, retired)

    embedding.visit(at=now + timedelta(seconds=1))

    assert [job.state for job in embedding.jobs_of(EMBED_JOB_KIND)] == [
        "succeeded",
        "succeeded",
    ]
    assert set(embedding.vectors()) == {(kept, fake.model_id)}
    assert embedding.failed_kinds() == ()


# --- AC 12: the rebuild ---------------------------------------------------------------


def _rebuild(ws: EmbeddingWorkspace) -> UUID:
    outcome = ws.call(EMBEDDING_REBUILD, {})
    assert outcome.state == "pending", outcome
    assert isinstance(outcome.result, EmbeddingRebuildScheduled), outcome
    assert outcome.operation_id is not None
    assert outcome.result.operation_id == outcome.operation_id
    return outcome.operation_id


def test_the_rebuild_prunes_other_models_fills_every_live_memory_and_reports_progress(
    embedding: EmbeddingWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 12, read after the job's one transaction has committed.

    The workspace is stamped to the current input version first, so the prune under
    test is the model one and not the version wipe. Progress is read off the operation
    record only after the drain: it is written inside the job's transaction and is
    invisible until that commits, by design.
    """
    fake = _fake()
    _select(monkeypatch, registry.FAKE_PROVIDER)
    with embedding.unit() as uow:
        set_embedding_state(uow.connection, embed_input_version=EMBED_INPUT_VERSION)
    covered, *uncovered = _seed(embedding, "apples", "pears", "plums", "figs")
    (invalidated,) = _seed(embedding, "quinces", invalidated=True)
    with embedding.unit() as uow:
        DenseStrategy().index_many(uow, [_item(embedding, covered)], provider=fake)
        for memory_id in (covered, uncovered[0], invalidated):
            insert_memory_embedding(
                uow.connection,
                MemoryEmbeddingRow(
                    memory_id=memory_id,
                    model_id=_RETIRED_MODEL,
                    dimensions=EMBEDDING_DIMENSIONS,
                    vector=_one_hot(),
                    embedded_at=datetime.now(UTC) - timedelta(days=1),
                ),
            )
    kept = embedding.vectors()[(covered, fake.model_id)]

    operation_id = _rebuild(embedding)
    embedding.visit(at=datetime.now(UTC) + timedelta(seconds=1))

    vectors = embedding.vectors()
    assert not [key for key in vectors if key[1] == _RETIRED_MODEL]
    assert vectors[(covered, fake.model_id)] == kept
    assert embedding.live_lacking(fake.model_id) == 0
    assert (
        len([key for key in vectors if key[1] == fake.model_id])
        == embedding.live_count()
    )
    assert (invalidated, fake.model_id) not in vectors

    record = embedding.operation(operation_id)
    assert record.state == "succeeded", record
    assert record.progress_fraction == 1.0
    assert record.progress_text and fake.model_id in record.progress_text
    (job,) = embedding.jobs_of(REBUILD_JOB_KIND)
    assert job.operation_id == operation_id
    assert job.state == "succeeded", job


def test_the_rebuild_refuses_when_no_provider_resolves(
    embedding: EmbeddingWorkspace,
) -> None:
    """An explicit owner request with nothing to rebuild against is a mistake worth
    hearing about, not a silent no-op — and nothing is queued."""
    assert registry.resolve_provider() is None
    outcome = embedding.call(EMBEDDING_REBUILD, {})
    assert outcome.state == EMBEDDING_PROVIDER_UNAVAILABLE, outcome
    assert outcome.error is not None
    assert outcome.error.error_code == EMBEDDING_PROVIDER_UNAVAILABLE
    assert embedding.jobs_of(REBUILD_JOB_KIND) == []


def test_a_bumped_input_version_makes_the_rebuild_replace_every_row_and_restamp(
    embedding: EmbeddingWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every live memory already carries a current-model row, so only the version
    wipe can explain a rewritten one."""
    fake = _fake()
    _select(monkeypatch, registry.FAKE_PROVIDER)
    memory_ids = _seed(embedding, "apples", "pears", "plums")
    old = datetime.now(UTC) - timedelta(days=1)
    with embedding.unit() as uow:
        set_embedding_state(uow.connection, embed_input_version=EMBED_INPUT_VERSION)
        for memory_id in memory_ids:
            item = _item(embedding, memory_id)
            insert_memory_embedding(
                uow.connection,
                MemoryEmbeddingRow(
                    memory_id=memory_id,
                    model_id=fake.model_id,
                    dimensions=fake.dimensions,
                    vector=fake.embed([embed_input(item.title, item.body)])[0],
                    embedded_at=old,
                ),
            )
    assert embedding.live_lacking(fake.model_id) == 0
    bumped = EMBED_INPUT_VERSION + 1
    monkeypatch.setattr(rebuild_module, "EMBED_INPUT_VERSION", bumped)

    _rebuild(embedding)
    embedding.visit(at=datetime.now(UTC) + timedelta(seconds=1))

    vectors = embedding.vectors()
    assert set(vectors) == {(memory_id, fake.model_id) for memory_id in memory_ids}
    assert all(row.embedded_at > old for row in vectors.values())
    with embedding.reading() as uow:
        state = get_embedding_state(uow.connection)
    assert state is not None and state.embed_input_version == bumped


def test_fill_missing_embeddings_on_its_own_fills_every_live_memory(
    embedding: EmbeddingWorkspace,
) -> None:
    """The harness entry: called directly, in a plain unit of work, with a batch
    smaller than the work so the cursor is exercised."""
    fake = _fake()
    first, *_ = _seed(embedding, "apples", "pears", "plums", "figs", "dates")
    (invalidated,) = _seed(embedding, "quinces", invalidated=True)
    with embedding.unit() as uow:
        DenseStrategy().index_many(uow, [_item(embedding, first)], provider=fake)
    lacking = embedding.live_lacking(fake.model_id)
    assert lacking > 2

    with embedding.unit() as uow:
        written = fill_missing_embeddings(uow, provider=fake, batch_size=2)

    assert written == lacking
    assert embedding.live_lacking(fake.model_id) == 0
    assert (invalidated, fake.model_id) not in embedding.vectors()


# --- AC 14: coverage is per workspace -------------------------------------------------


def test_coverage_is_answered_per_workspace(
    loaded: Loaded,
    cluster: ClusterSession,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
) -> None:
    """Two workspaces filled differently answer differently. There is no
    deployment-wide figure, because a workspace is a database."""
    fake = _fake()
    first = loaded.first
    second = _enabled(cluster, loaded.surfaces, make_workspace(), owner_account_id)
    _seed(first, "apples", "pears", "plums")
    _seed(second, "apples", "pears", "plums")
    with first.unit() as uow:
        fill_missing_embeddings(uow, provider=fake, batch_size=2)
    with second.unit() as uow:
        written, _ = fill_batch(uow, provider=fake, after=None, limit=1)
    assert written == 1

    full = first.coverage(fake.model_id)
    partial = second.coverage(fake.model_id)
    assert full.embedded == full.live == first.live_count()
    assert partial.live == second.live_count()
    assert partial.embedded == 1 < partial.live
    assert full != partial


# --- a reference check ----------------------------------------------------------------


def test_dense_invalidate_removes_every_vector_the_memory_holds(
    embedding: EmbeddingWorkspace,
) -> None:
    """The protocol's ``invalidate``, reached through a canonical reference."""
    fake = _fake()
    kept, dropped = _seed(embedding, "apples", "pears")
    with embedding.unit() as uow:
        DenseStrategy().index_many(
            uow, [_item(embedding, kept), _item(embedding, dropped)], provider=fake
        )
        DenseStrategy().invalidate(embedding.context(), uow, memory_reference(dropped))
    assert set(embedding.vectors()) == {(kept, fake.model_id)}
