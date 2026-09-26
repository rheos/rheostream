"""Recallatron's dense and hybrid arms against real workspaces.

Seams under test: ``DenseStrategy.search()`` (its hits, its relevance floor, its arm
counts and its savepoint-guarded degrade), called directly and through ``recall()``;
``HybridStrategy.search()`` (its fusion, its dense arm's over-fetch width and its
degrade to the lexical rows) and ``resolve_strategy``'s dispatch across the three
offered names, both observed through ``recall()``'s items, order and ``provenance``.

**How a test reaches an arm.** Each test names one of three mechanisms:

- **(a)** ``DenseStrategy().search(ctx, uow, request)``, or a registered strategy's
  ``search``, called directly, asserting on its ``SearchResult``;
- **(b)** ``recall()`` end to end with the workspace pinned to ``lexical`` and
  ``STRATEGY_REGISTRY["lexical"]`` swapped for ``DenseStrategy()``, so the configured
  name dispatches the dense implementation and the response reports
  ``strategy="dense"``, the dispatched strategy's own name. The dense tests written
  before the key offered ``dense`` use it. The pin matters: an unconfigured workspace
  resolves ``hybrid``, which never reaches the swapped entry;
- **(c)** ``recall()`` with the strategy stored as the workspace override, the
  ordinary way, now that the key offers all three.

**One way to select a provider.** A test rebinds ``registry.configured_provider_name``
(``monkeypatch`` undoes it), never an ``RHEO__`` variable. **The real-provider tests
fail, never skip,** without the ``local-embeddings`` extra; the failure names the
command that installs it.

**Fixture rows go through the repository**, and vectors through ``index_many``,
``index`` or ``fill_missing_embeddings``. The one exception is the short-arm guard's
population: five thousand synthetic vectors written in one statement by spec evidence
E4's recipe.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from psycopg import errors as pg_errors
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import register_resolver
from rheo_core.settings import ValueType
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.repositories import upsert_workspace_setting
from rheo_recallatron import MANIFEST
from rheo_recallatron.configuration import (
    CANDIDATE_SCAN_LIMIT,
    DENSE_FLOOR_PERCENT_DEFAULT,
    DENSE_FLOOR_PERCENT_KEY,
    EMBEDDING_BATCH_SIZE_DEFAULT,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_PROVIDER_NONE,
    HNSW_EF_SEARCH_MIN,
    HNSW_MAX_SCAN_TUPLES,
    MEMORY_RECORD_TYPE,
    OVERFETCH_MULTIPLIER_DEFAULT,
    OVERFETCH_MULTIPLIER_KEY,
    RECALL_K_DEFAULT,
    RETENTION_EXPIRE_BY_AGE_KEY,
    RETRIEVAL_STRATEGY_KEY,
    STRATEGY_DENSE,
    STRATEGY_HYBRID,
    STRATEGY_LEXICAL,
)
from rheo_recallatron.contracts import (
    ArmCounts,
    MemoryCorrected,
    MemorySuperseded,
    RecallProvenance,
)
from rheo_recallatron.eligibility import (
    MemoryRequest,
    ReadMode,
    begin_request,
    memory_reference,
    row_local_conditions,
)
from rheo_recallatron.embedding import embed_input, registry
from rheo_recallatron.embedding.fake import FAKE_MODEL_ID, FakeEmbeddingProvider
from rheo_recallatron.embedding.local import LocalEmbeddingProvider
from rheo_recallatron.embedding.protocol import EmbeddingProvider
from rheo_recallatron.embedding.rebuild import fill_missing_embeddings
from rheo_recallatron.operations import (
    MEMORY_CORRECT,
    MEMORY_RECALL,
    MEMORY_SUPERSEDE,
    RecallResult,
)
from rheo_recallatron.resolvers import resolve_memory
from rheo_recallatron.retrieval.dense import (
    NO_EMBEDDINGS,
    NO_PROVIDER,
    PROVIDER_RAISED,
    STATEMENT_RAISED,
    DenseStrategy,
    build_dense_statement,
    dense_distance,
    dense_floor,
    dense_floor_percent,
    dense_floor_percent_in,
    dense_similarity,
    hnsw_scan_settings,
)
from rheo_recallatron.retrieval.dispatch import STRATEGY_REGISTRY
from rheo_recallatron.retrieval.hybrid import HybridStrategy
from rheo_recallatron.retrieval.lexical import LexicalStrategy
from rheo_recallatron.retrieval.protocol import (
    ArmProvenance,
    IndexItem,
    RetrievalStrategy,
    SearchRequest,
    SearchResult,
)
from rheo_recallatron.storage import tables as memory_tables
from rheo_recallatron.storage.repository import (
    MemoryLinkRow,
    MemoryPurposeRow,
    MemoryRow,
    analyze_memory,
    get_memory,
    insert_memory,
    insert_memory_link,
    insert_memory_purpose,
    vector_literal,
)
from sqlalchemy import (
    Boolean,
    ColumnElement,
    Engine,
    Select,
    Text,
    cast,
    func,
    literal_column,
    select,
    text,
)

pytestmark = pytest.mark.postgres

_MEMORY_MODULE = MANIFEST.module_id
_RESPOND = "respond"
_EXTRA_REMEDY = "uv sync --frozen --extra local-embeddings"
_DENSE_LOGGER = "rheo_recallatron.retrieval.dense"
_HNSW_INDEX = "memory_embedding_vector_hnsw"

_UNAVAILABLE = SearchResult(
    hits=(), arms=ArmProvenance(lexical=0, dense=0), dense_available=False
)
"""What every dense-only failure answers: no hits, no arm counts, no refusal."""


class FixtureDefect(Exception):
    """A gate on a fixture's shape tripped.

    The fixture cannot test what it claims, so the result would mean nothing either
    way. That is a defect in the fixture, reported as one, and never a reason to loosen
    the gate or the assertion it protects.
    """


def _gate(holds: bool, message: str) -> None:
    if not holds:
        raise FixtureDefect(message)


# --- the workspace --------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalWorkspace:
    """A workspace with Recallatron installed, enabled and migrated."""

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

    def recall(self, **payload: object) -> OperationOutcome:
        return self.call(MEMORY_RECALL, payload)

    def set_strategy(self, name: str) -> None:
        """Mechanism (c): the retrieval strategy as this workspace's stored override."""
        self.setting(RETRIEVAL_STRATEGY_KEY, name, ValueType.STR)

    def setting(self, key: str, value: str, value_type: ValueType) -> None:
        """One workspace override row, written around the settings operation."""
        with self.unit() as uow:
            upsert_workspace_setting(
                uow.connection,
                key=key,
                value=value,
                value_type=value_type,
                updated_by=None,
            )


@pytest.fixture
def retrieval(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[RetrievalWorkspace]:
    """Recallatron loaded through the harness's one recipe, enabled in one workspace.

    The memory resolver goes on the process-wide table as well, where a real
    deployment's loader puts it and where the eligibility walk resolves references.
    """
    register_core_operations()
    register_resolver(
        _MEMORY_MODULE, MEMORY_RECORD_TYPE, resolve_memory, origin=_MEMORY_MODULE
    )
    with loaded_probe_modules(monkeypatch, _MEMORY_MODULE) as surfaces:
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext), bootstrap
        install_and_enable_module(cluster.backend, bootstrap, workspace, _MEMORY_MODULE)
        row = cluster.registry_row(workspace)
        yield RetrievalWorkspace(
            cluster=cluster,
            workspace=workspace,
            owner_account_id=owner_account_id,
            surfaces=surfaces,
            database_name=row.database_name,
            engine=cluster.backend.pools.engine_for(row.database_name),
            consumers=ConsumerRegistry(),
        )


# --- providers ------------------------------------------------------------------------


def _select(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """The one mechanism: rebind the name reader, undone by ``monkeypatch``."""
    monkeypatch.setattr(registry, "configured_provider_name", lambda: name)


def _install(
    monkeypatch: pytest.MonkeyPatch, name: str, provider: EmbeddingProvider
) -> None:
    """Register a provider the built-ins cannot produce, removed again at teardown;
    ``register_provider`` applies the width check a real registration gets."""
    monkeypatch.setitem(registry.providers(), name, provider)
    registry.register_provider(name, provider)


def _local(monkeypatch: pytest.MonkeyPatch) -> LocalEmbeddingProvider:
    _select(monkeypatch, registry.LOCAL_PROVIDER)
    provider = registry.resolve_provider()
    assert isinstance(provider, LocalEmbeddingProvider), (
        "the local embedding provider is not registered, so the local-embeddings "
        f"extra is not installed: run `{_EXTRA_REMEDY}`"
    )
    return provider


def _fake(monkeypatch: pytest.MonkeyPatch) -> FakeEmbeddingProvider:
    _select(monkeypatch, registry.FAKE_PROVIDER)
    provider = registry.resolve_provider()
    assert isinstance(provider, FakeEmbeddingProvider), (
        "the fake provider registers only under the test profile"
    )
    return provider


class _RaisingProvider:
    """A configured provider that fails: what a working model cannot do on demand."""

    model_id = "rheo-test/raising"
    dimensions = EMBEDDING_DIMENSIONS

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        raise RuntimeError("the embedding provider is down")


class _WrongWidthProvider:
    """Claims the fake's model and the column's width, answers three numbers.

    Rows exist for its ``model_id`` once the fake has filled the workspace, so the only
    thing wrong is the query vector's width, and the only place that can catch it is
    the database.
    """

    model_id = FAKE_MODEL_ID
    dimensions = EMBEDDING_DIMENSIONS

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [(1.0, 0.0, 0.0) for _ in texts]


@dataclass(frozen=True)
class _RenamedProvider:
    """The fake's vectors under another model's name: rows a different model wrote."""

    model_id: str
    inner: FakeEmbeddingProvider

    @property
    def dimensions(self) -> int:
        return self.inner.dimensions

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return self.inner.embed(texts)


# --- seeding and reading --------------------------------------------------------------


def _recent(offset_seconds: int = 0) -> datetime:
    """Comfortably inside any retention window, ordered by offset."""
    return datetime.now(UTC) - timedelta(days=1) + timedelta(seconds=offset_seconds)


def _long_ago() -> datetime:
    """Older than the 365-day default window, so an age-gated read excludes it."""
    return datetime.now(UTC) - timedelta(days=400)


def _row(title: str, body: str, *, recorded_at: datetime | None = None) -> MemoryRow:
    return MemoryRow(
        id=uuid7(),
        kind="note",
        title=title,
        body=body,
        audience_kind="workspace",
        audience_id=None,
        confidence=None,
        occurred_at=None,
        recorded_at=_recent() if recorded_at is None else recorded_at,
        recorded_by_kind="account",
        recorded_by_id=None,
        origin="told",
        revision=1,
        corrected_at=None,
        superseded_by_id=None,
        invalidated_at=None,
        invalidation_reason=None,
        source_namespace=None,
        external_source_key=None,
    )


def _write(uow: UnitOfWork, row: MemoryRow) -> UUID:
    """One memory and its purpose, through the repository, as ``_write`` does in
    ``test_memory_records.py``."""
    insert_memory(uow.connection, row)
    insert_memory_purpose(
        uow.connection, MemoryPurposeRow(memory_id=row.id, purpose=_RESPOND)
    )
    return row.id


def _seed(ws: RetrievalWorkspace, *texts: tuple[str, str]) -> list[UUID]:
    with ws.unit() as uow:
        return [_write(uow, _row(title, body)) for title, body in texts]


def _fill(ws: RetrievalWorkspace, provider: EmbeddingProvider) -> None:
    with ws.unit() as uow:
        fill_missing_embeddings(
            uow, provider=provider, batch_size=EMBEDDING_BATCH_SIZE_DEFAULT
        )


def _item(ws: RetrievalWorkspace, memory_id: UUID) -> IndexItem:
    with ws.reading() as uow:
        row = get_memory(uow.connection, memory_id)
    assert row is not None
    return IndexItem(row.id, row.title, row.body)


def _request(ctx: WorkspaceContext, uow: UnitOfWork) -> MemoryRequest:
    request = begin_request(ctx, uow)
    assert request is not None
    return request


def _search(ws: RetrievalWorkspace, query: str) -> SearchResult:
    """Mechanism (a) at the ``dense``-only arm width, and proof nothing escaped: one
    more statement on the same connection must still run."""
    ctx = ws.context()
    with ws.reading() as uow:
        result = DenseStrategy().search(
            ctx,
            uow,
            SearchRequest(
                query=query,
                mode=ReadMode.CURRENT,
                memory=_request(ctx, uow),
                limit=CANDIDATE_SCAN_LIMIT,
                k=RECALL_K_DEFAULT,
            ),
        )
        assert uow.connection.execute(select(literal_column("1"))).scalar_one() == 1
    return result


def _recalled(outcome: OperationOutcome) -> RecallResult:
    assert outcome.ok, outcome
    assert isinstance(outcome.result, RecallResult), outcome
    return outcome.result


def _dense_recall(
    ws: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch, query: str
) -> RecallResult:
    """Mechanism (b): the configured name, ``lexical``, dispatches ``DenseStrategy``."""
    ws.set_strategy(STRATEGY_LEXICAL)
    monkeypatch.setitem(STRATEGY_REGISTRY, STRATEGY_LEXICAL, DenseStrategy())
    return _recalled(ws.recall(query=query))


def _similarity(
    uow: UnitOfWork, memory_id: UUID, *, model_id: str, query_vector: Sequence[float]
) -> float:
    """One stored row's similarity to the query, by the arm's own expression and a
    non-index pass: no ``ORDER BY`` on the distance, so HNSW cannot serve it."""
    embedding = memory_tables.memory_embedding
    return float(
        uow.connection.execute(
            select(dense_similarity(dense_distance(query_vector))).where(
                embedding.c.memory_id == memory_id, embedding.c.model_id == model_id
            )
        ).scalar_one()
    )


def _one_dense_line(
    caplog: pytest.LogCaptureFixture, level: int, query: str, *memory_texts: str
) -> logging.LogRecord:
    """The dense arm's one log line, which must be at ``level`` and never carries the
    query's text or a memory's.

    Exactly one line at any level: so when ``level`` is debug, no warning was written.
    """
    records = [record for record in caplog.records if record.name == _DENSE_LOGGER]
    assert len(records) == 1, [(r.levelname, r.getMessage()) for r in records]
    (record,) = records
    assert record.levelno == level, record.levelname
    message = record.getMessage()
    for withheld in (query, *memory_texts):
        assert withheld not in message, withheld
    return record


def _one_warning(caplog: pytest.LogCaptureFixture, query: str) -> logging.LogRecord:
    """A failure that is a fault: one warning, which never carries the query's text."""
    return _one_dense_line(caplog, logging.WARNING, query)


# --- the floor's stored value ---------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, DENSE_FLOOR_PERCENT_DEFAULT),
        ("0", 0),
        ("45", 45),
        ("100", 100),
        ("-1", DENSE_FLOOR_PERCENT_DEFAULT),
        ("101", DENSE_FLOOR_PERCENT_DEFAULT),
        ("0.3", DENSE_FLOOR_PERCENT_DEFAULT),
        ("not-a-number", DENSE_FLOOR_PERCENT_DEFAULT),
    ],
)
def test_the_floor_read_degrades_to_the_default(
    stored: str | None, expected: int
) -> None:
    """Absent, undecodable or out of range: the default, never a raise."""
    rows = {} if stored is None else {DENSE_FLOOR_PERCENT_KEY: stored}
    assert dense_floor_percent_in(rows) == expected


# --- AC 3: a relevant memory that shares no term with its query -----------------------

_AC3_QUERY = "purchase groceries"
_AC3_MEMORY = ("shopping", "buy milk, eggs and bread at the store")
_AC3_MARGIN = 0.15


def _lexemes(value: str) -> ColumnElement[Any]:
    """The text's lexemes as ``text[]``: ``tsvector`` has no ``&&`` of its own."""
    return func.tsvector_to_array(
        func.to_tsvector(literal_column("'english'::regconfig"), value)
    )


def test_dense_recall_finds_a_memory_that_shares_no_term_with_its_query(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 3, on the real provider. Mechanism (b) for the dense half.

    The premise is asserted, not assumed: a shared stem would let lexical find the
    memory and make the lexical half pass for the wrong reason.
    """
    provider = _local(monkeypatch)
    (shopping,) = _seed(retrieval, _AC3_MEMORY)
    _fill(retrieval, provider)
    ctx = retrieval.context()
    (query_vector,) = provider.embed([_AC3_QUERY])

    with retrieval.reading() as uow:
        memory_lexemes, query_lexemes, shared = uow.connection.execute(
            select(
                _lexemes(embed_input(*_AC3_MEMORY)),
                _lexemes(_AC3_QUERY),
                _lexemes(embed_input(*_AC3_MEMORY)).op("&&", return_type=Boolean)(
                    _lexemes(_AC3_QUERY)
                ),
            )
        ).one()
        floor = dense_floor_percent(ctx, uow)
        similarity = _similarity(
            uow, shopping, model_id=provider.model_id, query_vector=query_vector
        )
    # {bread,buy,egg,milk,shop,store} against {groceri,purchas} on the cluster.
    assert memory_lexemes and query_lexemes, (memory_lexemes, query_lexemes)
    assert shared is False, (memory_lexemes, query_lexemes)
    assert floor == DENSE_FLOOR_PERCENT_DEFAULT
    # Measured 0.613 on the shipped provider (spec evidence E2 § 3), 0.313 above the
    # floor. A margin, not an exact score: ONNX CPU inference is deterministic for one
    # model and runtime, not bit-identical across runtime versions or instruction sets.
    assert similarity >= floor / 100.0 + _AC3_MARGIN, similarity

    retrieval.set_strategy(STRATEGY_LEXICAL)
    lexical = _recalled(retrieval.recall(query=_AC3_QUERY))
    assert lexical.provenance.strategy == STRATEGY_LEXICAL
    assert memory_reference(shopping) not in [item.ref for item in lexical.items]

    dense = _dense_recall(retrieval, monkeypatch, _AC3_QUERY)
    assert [item.ref for item in dense.items] == [memory_reference(shopping)]
    assert [item.strategy for item in dense.items] == [STRATEGY_DENSE]
    assert dense.provenance == RecallProvenance(
        strategy=STRATEGY_DENSE,
        arms=ArmCounts(lexical=0, dense=1),
        dense_available=True,
    )


# --- AC 6, the dense-only half: every failure is an empty answer, never a refusal -----


def test_dense_with_no_provider_answers_nothing(
    retrieval: RetrievalWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 6 (a), no provider configured. Mechanism (a).

    No provider is the shipped default, which every recall under ``hybrid`` passes
    through, so it is logged at debug and never as a warning. The line still names
    the cause.
    """
    _seed(retrieval, ("apples", "a note about apples"))
    _select(monkeypatch, EMBEDDING_PROVIDER_NONE)

    with caplog.at_level(logging.DEBUG, logger=_DENSE_LOGGER):
        assert _search(retrieval, "apples") == _UNAVAILABLE

    assert not [
        record
        for record in caplog.records
        if record.name == _DENSE_LOGGER and record.levelno >= logging.WARNING
    ]
    record = _one_dense_line(caplog, logging.DEBUG, "apples", "a note about apples")
    assert record.dense_cause == NO_PROVIDER
    assert record.provider_model_id is None
    assert record.provider_dimensions is None
    assert record.error_class is None


def test_dense_with_a_failing_provider_answers_nothing(
    retrieval: RetrievalWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 6 (b), the provider's ``embed()`` raises. Mechanism (a)."""
    _seed(retrieval, ("apples", "a note about apples"))
    _install(monkeypatch, "raising", _RaisingProvider())
    _select(monkeypatch, "raising")

    with caplog.at_level(logging.WARNING, logger=_DENSE_LOGGER):
        assert _search(retrieval, "apples") == _UNAVAILABLE

    record = _one_warning(caplog, "apples")
    assert record.dense_cause == PROVIDER_RAISED
    assert record.provider_model_id == _RaisingProvider.model_id
    assert record.provider_dimensions == EMBEDDING_DIMENSIONS
    assert record.error_class == "RuntimeError"


def test_dense_with_no_rows_for_its_model_answers_nothing(
    retrieval: RetrievalWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 6 (c), a provider resolved and no row written by its model. Mechanism (a).

    Rows written by **another** model are present, so an availability check that
    counted any vector at all, rather than this model's, answers ``True`` here.
    """
    (apples,) = _seed(retrieval, ("apples", "a note about apples"))
    fake = _fake(monkeypatch)
    retired = _RenamedProvider(model_id="rheo-test/retired-model", inner=fake)
    with retrieval.unit() as uow:
        DenseStrategy().index_many(uow, [_item(retrieval, apples)], provider=retired)
    with retrieval.reading() as uow:
        per_model = dict(
            uow.connection.execute(
                select(
                    memory_tables.memory_embedding.c.model_id, func.count()
                ).group_by(memory_tables.memory_embedding.c.model_id)
            ).all()
        )
    _gate(
        per_model == {retired.model_id: 1},
        f"expected one row, by another model only: {per_model}",
    )

    with caplog.at_level(logging.WARNING, logger=_DENSE_LOGGER):
        assert _search(retrieval, "apples") == _UNAVAILABLE

    record = _one_warning(caplog, "apples")
    assert record.dense_cause == NO_EMBEDDINGS
    assert record.provider_model_id == FAKE_MODEL_ID
    assert record.error_class is None


def test_a_wrong_width_query_vector_fails_in_sql_and_the_transaction_survives(
    retrieval: RetrievalWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 6's mandated SQL-level case. Mechanism (a).

    The fake fills valid 384-wide rows under its model; then a provider claiming the
    same model and width answers a 3-wide vector. Rows exist for the resolved model,
    so the only failure left is ``different vector dimensions 384 and 3``, raised
    inside the savepoint. The class must be the driver's ``DataException``, not a
    ``ValueError``: a Python width check would have stopped it before SQL, and this
    case exists to be the one that reaches it.
    """
    fake = _fake(monkeypatch)
    _seed(retrieval, ("apples", "a note about apples"))
    _fill(retrieval, fake)
    _install(monkeypatch, "wrong-width", _WrongWidthProvider())
    _select(monkeypatch, "wrong-width")
    ctx = retrieval.context()

    with (
        retrieval.reading() as uow,
        caplog.at_level(logging.WARNING, logger=_DENSE_LOGGER),
    ):
        result = DenseStrategy().search(
            ctx,
            uow,
            SearchRequest(
                query="apples",
                mode=ReadMode.CURRENT,
                memory=_request(ctx, uow),
                limit=CANDIDATE_SCAN_LIMIT,
                k=RECALL_K_DEFAULT,
            ),
        )
        # The outer transaction survived the failed statement: after an abort, every
        # further statement on this connection would fail instead.
        survivors = uow.connection.execute(
            select(func.count())
            .select_from(memory_tables.memory_embedding)
            .where(memory_tables.memory_embedding.c.model_id == FAKE_MODEL_ID)
        ).scalar_one()

    assert survivors == 1
    assert result == _UNAVAILABLE
    record = _one_warning(caplog, "apples")
    assert record.dense_cause == STATEMENT_RAISED
    assert record.error_class == pg_errors.DataException.__name__
    assert record.error_class != ValueError.__name__
    assert record.provider_model_id == FAKE_MODEL_ID
    assert record.provider_dimensions == EMBEDDING_DIMENSIONS


def test_a_dense_recall_with_no_provider_is_empty_and_never_lexical(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 6 (a) through ``recall()``. Mechanism (b).

    The memory matches the query lexically, and lexical recall finds it. Under
    ``dense`` with no usable arm the answer is empty, labelled ``dense``, and not a
    refusal: a caller who chose ``dense`` is never handed lexical results.
    """
    _seed(retrieval, ("apples", "a note about apples"))
    _select(monkeypatch, EMBEDDING_PROVIDER_NONE)
    retrieval.set_strategy(STRATEGY_LEXICAL)
    lexical = _recalled(retrieval.recall(query="apples"))
    assert [item.title for item in lexical.items] == ["apples"]

    monkeypatch.setitem(STRATEGY_REGISTRY, STRATEGY_LEXICAL, DenseStrategy())
    outcome = retrieval.recall(query="apples")

    assert outcome.ok, outcome
    result = _recalled(outcome)
    assert result.items == ()
    assert result.provenance == RecallProvenance(
        strategy=STRATEGY_DENSE,
        arms=ArmCounts(lexical=0, dense=0),
        dense_available=False,
    )


# --- dense_available's true half ------------------------------------------------------


def test_a_candidate_without_its_own_row_leaves_the_dense_arm_available(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial coverage, dense-only (edge case 3). Mechanism (a).

    One memory's job has not run yet while another's has. That is the normal steady
    state, not a broken arm: the pending memory is simply absent from the arm.
    """
    fake = _fake(monkeypatch)
    covered, pending = _seed(
        retrieval,
        ("apples", "a note about apples"),
        ("apple pie", "a note about apple pie"),
    )
    with retrieval.unit() as uow:
        DenseStrategy().index_many(uow, [_item(retrieval, covered)], provider=fake)
    with retrieval.reading() as uow:
        embedding = memory_tables.memory_embedding
        pending_rows, model_rows = uow.connection.execute(
            select(
                func.count().filter(embedding.c.memory_id == pending),
                func.count().filter(embedding.c.model_id == fake.model_id),
            )
        ).one()
    _gate(pending_rows == 0, f"the pending memory has {pending_rows} rows")
    _gate(model_rows > 0, "the workspace holds no row for the resolved model")

    result = _search(retrieval, "apple pie")

    assert result.dense_available is True
    assert pending not in {hit.ref for hit in result.hits}


def test_an_embedded_workspace_reports_the_dense_arm_available(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 8's ``true`` half at the arm level: available, with a non-zero dense count.
    Mechanism (a)."""
    fake = _fake(monkeypatch)
    apples, _pears = _seed(
        retrieval,
        ("apples", "a note about apples"),
        ("pears", "a note about pears"),
    )
    _fill(retrieval, fake)

    result = _search(retrieval, "apples")

    assert result.dense_available is True
    assert result.arms.dense > 0
    assert result.arms == ArmProvenance(lexical=0, dense=len(result.hits))
    assert result.hits[0].ref == apples
    assert {hit.strategy for hit in result.hits} == {STRATEGY_DENSE}
    assert all(hit.score >= DENSE_FLOOR_PERCENT_DEFAULT / 100.0 for hit in result.hits)


# --- the arm's own limit --------------------------------------------------------------


def test_the_arm_is_bounded_by_the_request_limit_in_its_statement_and_its_scan(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``request.limit`` bounds the arm, and no constant does. Mechanism (a).

    ``recall()`` always asks for ``CANDIDATE_SCAN_LIMIT``, so no test that goes through
    it can tell ``request.limit`` from a hard-coded 500. The hybrid over-fetch asks for
    fewer. Here three memories clear the floor and the arm is asked for one: it returns
    the nearest, counts one, and leaves this transaction's ``ef_search`` at a one-row
    arm's value, not a 500-row arm's.
    """
    fake = _fake(monkeypatch)
    _seed(
        retrieval,
        ("apples", "apples are red"),
        ("apples", "a note about apples"),
        ("apples and pears", "a note about apples and pears"),
    )
    _fill(retrieval, fake)
    ctx = retrieval.context()
    (query_vector,) = fake.embed(["apples"])
    embedding = memory_tables.memory_embedding

    with retrieval.reading() as uow:
        # The arm's own expressions, floor included, by a non-index pass: no ORDER BY.
        similarity = dense_similarity(dense_distance(query_vector))
        above_floor: dict[UUID, float] = {
            memory_id: float(score)
            for memory_id, score in uow.connection.execute(
                select(memory_tables.memory.c.id, similarity)
                .select_from(
                    memory_tables.memory.join(
                        embedding, embedding.c.memory_id == memory_tables.memory.c.id
                    )
                )
                .where(
                    embedding.c.model_id == fake.model_id,
                    dense_floor(similarity, dense_floor_percent(ctx, uow)),
                    *row_local_conditions(_request(ctx, uow), ReadMode.CURRENT),
                )
            )
        }
    ranked = sorted(above_floor.values(), reverse=True)
    _gate(
        len(ranked) > 1,
        f"{len(ranked)} rows clear the floor, so a limit of one would bind nothing",
    )
    _gate(ranked[0] > ranked[1], f"the two nearest rows tie: {ranked[:2]}")
    nearest = max(above_floor, key=above_floor.__getitem__)

    with retrieval.reading() as uow:
        result = DenseStrategy().search(
            ctx,
            uow,
            SearchRequest(
                query="apples",
                mode=ReadMode.CURRENT,
                memory=_request(ctx, uow),
                limit=1,
                k=RECALL_K_DEFAULT,
            ),
        )
        # ``SET LOCAL`` outlives the released savepoint, so this is what the arm set.
        scan_settings = tuple(
            uow.connection.execute(
                select(
                    func.current_setting("hnsw.ef_search"),
                    func.current_setting("hnsw.iterative_scan"),
                    func.current_setting("hnsw.max_scan_tuples"),
                )
            ).one()
        )

    assert result.dense_available is True
    assert len(result.hits) == result.arms.dense == 1
    assert result.hits[0].ref == nearest
    assert scan_settings == (
        str(HNSW_EF_SEARCH_MIN),
        "strict_order",
        str(HNSW_MAX_SCAN_TUPLES),
    )


# --- AC 11: below the floor is nothing, not a weak answer -----------------------------

_AC11_QUERY = "quarterly tax filing deadline"
_AC11_CLEARANCE = 0.20


def test_dense_answers_nothing_when_every_candidate_is_below_the_floor(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 11, on the real provider. Mechanism (b).

    What this proves is the floor **mechanism**: below it, the caller gets zero items
    rather than the least-bad row. It is never evidence that 30 is the right number:
    30 was measured on the shipped provider, but not on a memory corpus (spec
    requirement 9), and run 1b is where that is confirmed.

    A zero must not be able to come from an arm that never ran, so the arm reports
    itself available, rows exist for its model, and the best candidate is measured
    to sit well below the floor.
    """
    provider = _local(monkeypatch)
    (apples,) = _seed(retrieval, ("apples", "a note about apples"))
    _fill(retrieval, provider)
    ctx = retrieval.context()
    (query_vector,) = provider.embed([_AC11_QUERY])
    embedding = memory_tables.memory_embedding

    with retrieval.reading() as uow:
        floor = dense_floor_percent(ctx, uow)
        model_rows = uow.connection.execute(
            select(func.count())
            .select_from(embedding)
            .where(embedding.c.model_id == provider.model_id)
        ).scalar_one()
        best = uow.connection.execute(
            select(func.max(dense_similarity(dense_distance(query_vector))))
            .select_from(
                memory_tables.memory.join(
                    embedding, embedding.c.memory_id == memory_tables.memory.c.id
                )
            )
            .where(
                embedding.c.model_id == provider.model_id,
                *row_local_conditions(_request(ctx, uow), ReadMode.CURRENT),
            )
        ).scalar_one()
    assert floor == DENSE_FLOOR_PERCENT_DEFAULT
    assert model_rows == 1
    # Measured 0.012 on the shipped provider (spec evidence E2 § 3), 0.288 below it.
    assert best is not None and float(best) <= floor / 100.0 - _AC11_CLEARANCE, best

    result = _dense_recall(retrieval, monkeypatch, _AC11_QUERY)

    assert result.items == ()
    assert result.provenance == RecallProvenance(
        strategy=STRATEGY_DENSE,
        arms=ArmCounts(lexical=0, dense=0),
        dense_available=True,
    )
    assert memory_reference(apples) not in [item.ref for item in result.items]


# --- the lexical exact list, under dense ----------------------------------------------

_PEARS_CLEARANCE = 0.10


def test_dense_recall_of_apples_returns_the_nearest_rows_apples_then_pears(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dense counterpart of
    ``test_recall_returns_eligible_rows_marked_with_the_resolved_strategy``:
    the same three rows and the same age gate, on the real provider at the shipped
    floor with no override. Mechanism (b).

    What this proves is requirement 22's sentence, "nearest rows, not relevant ones",
    as a test: ``pears`` is near ``apples`` in the space and comes back under dense.
    The lexical test's exclusion of ``pears`` is the ``@@`` predicate's doing, not a
    dense defect (spec § Technical Risks 8, evidence E9).
    """
    provider = _local(monkeypatch)
    with retrieval.unit() as uow:
        _write(uow, _row("apples", "a note about apples"))
        pears = _write(uow, _row("pears", "a note about pears", recorded_at=_recent(1)))
        expired = _write(
            uow,
            _row("expired apples", "an old note about apples", recorded_at=_long_ago()),
        )
    retrieval.setting(RETENTION_EXPIRE_BY_AGE_KEY, "true", ValueType.BOOL)
    _fill(retrieval, provider)
    ctx = retrieval.context()
    (query_vector,) = provider.embed(["apples"])

    with retrieval.reading() as uow:
        floor = dense_floor_percent(ctx, uow)
        pears_similarity = _similarity(
            uow, pears, model_id=provider.model_id, query_vector=query_vector
        )
        expired_similarity = _similarity(
            uow, expired, model_id=provider.model_id, query_vector=query_vector
        )
    assert floor == DENSE_FLOOR_PERCENT_DEFAULT
    # Measured 0.471 on the shipped provider, 0.171 above the floor.
    _gate(
        pears_similarity >= floor / 100.0 + _PEARS_CLEARANCE,
        f"pears scores {pears_similarity:.3f}, too close to the floor to mean anything",
    )
    # The expired row clears the floor too, so only the age gate in SQL can drop it.
    _gate(
        expired_similarity >= floor / 100.0,
        f"the expired row scores {expired_similarity:.3f}, under the floor already",
    )

    result = _dense_recall(retrieval, monkeypatch, "apples")

    assert [item.title for item in result.items] == ["apples", "pears"]
    assert result.provenance == RecallProvenance(
        strategy=STRATEGY_DENSE,
        arms=ArmCounts(lexical=0, dense=2),
        dense_available=True,
    )


# --- the short-arm guard: the HNSW plan reaches every row the predicate admits --------

_POPULATION = 5_000
_RESOLVED_EVERY = 50
"""Every 50th memory's vector is the resolved model's: 100 of 5,000, the mid-rebuild
shape. No memory here fails a workspace condition, so there is no exclusion pattern
for this one to share a factor with."""

_RESOLVED_MODEL = "rheo-test/e4-recipe"
_OTHER_MODEL = "rheo-test/e4-recipe-previous"
_GROUND_TRUTH_MINIMUM = 30
_RECIPE_QUERY = tuple(
    math.sin(0.35 + i * 0.11) for i in range(1, EMBEDDING_DIMENSIONS + 1)
)
"""A noise-free vector of the recipe's own shape, at a phase no row has exactly."""


class _RecipeQueryProvider:
    """Answers every text with :data:`_RECIPE_QUERY`, under the resolved model."""

    model_id = _RESOLVED_MODEL
    dimensions = EMBEDDING_DIMENSIONS

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [_RECIPE_QUERY for _ in texts]


def _seed_recipe_population(ws: RetrievalWorkspace) -> None:
    """Spec evidence E4's recipe over real memories.

    Each memory ``g`` of 5,000 gets ``sin(g*0.7 + i*0.11) + random()*0.01`` over
    ``i = 1..384``: distinct vectors spread round the whole circle of phases, not
    clustered at the query. Clustered rows would be the first ``ef_search``
    candidates, and a scan with ``iterative_scan`` off would find them all too.
    ``ARRAY(subquery)`` is required: an ``array_agg`` over the outer ``g`` parses as an
    aggregate of the outer query and the insert fails.
    """
    embedding = memory_tables.memory_embedding
    with ws.unit() as uow:
        ids = [
            _write(uow, _row(f"recipe row {g}", f"synthetic row number {g}"))
            for g in range(1, _POPULATION + 1)
        ]
        uow.connection.execute(
            text(
                f"INSERT INTO {embedding.fullname} "
                "(memory_id, model_id, dimensions, vector, embedded_at) "
                "SELECT seeded.memory_id, "
                "CASE WHEN seeded.g % :every = 0 THEN :resolved ELSE :other END, "
                ":dimensions, "
                "(SELECT ARRAY(SELECT (sin(seeded.g * 0.7 + i * 0.11) "
                "+ random() * 0.01)::real "
                "FROM generate_series(1, :dimensions) AS i))::vector, "
                "now() "
                "FROM unnest(CAST(:ids AS uuid[])) WITH ORDINALITY "
                "AS seeded(memory_id, g)"
            ),
            {
                "every": _RESOLVED_EVERY,
                "resolved": _RESOLVED_MODEL,
                "other": _OTHER_MODEL,
                "dimensions": EMBEDDING_DIMENSIONS,
                "ids": ids,
            },
        )
        analyze_memory(uow.connection)
        uow.connection.execute(text(f"ANALYZE {embedding.fullname}"))


def _explain(uow: UnitOfWork, statement: Select[Any]) -> str:
    """``EXPLAIN`` of a built statement, under this transaction's settings."""
    compiled = statement.compile(
        dialect=uow.connection.dialect, compile_kwargs={"render_postcompile": True}
    )
    rows = uow.connection.exec_driver_sql("EXPLAIN " + str(compiled), compiled.params)
    return "\n".join(str(row[0]) for row in rows)


def _load_pgvector(uow: UnitOfWork) -> None:
    """Touch a vector, so this backend has pgvector loaded before the arm runs.

    Until the library loads, ``set_config('hnsw.ef_search', ...)`` writes an
    unvalidated placeholder, and an out-of-range one is quietly reset to the default on
    load rather than refused. Loading it first makes this test behave as a warm
    production backend does, whichever pooled connection it is handed, so an
    unclamped ``ef_search`` fails here as it would there.
    """
    uow.connection.execute(
        select(cast(vector_literal(_RECIPE_QUERY), memory_tables.Vector))
    )


def _forced_hnsw(uow: UnitOfWork) -> None:
    """Take every path but the HNSW scan off the table. Both hints are needed: with
    ``enable_seqscan`` off alone, the planner joins through the primary key and a
    ``Sort``, which is complete whatever ``iterative_scan`` says."""
    uow.connection.execute(text("SET LOCAL enable_seqscan = off"))
    uow.connection.execute(text("SET LOCAL enable_sort = off"))


def test_the_dense_arm_on_the_hnsw_plan_reaches_every_row_the_predicate_admits(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent-short-arm guard, and the proof that the HNSW path is what ran.
    Mechanism (a).

    5,000 memories, 100 of them embedded by the resolved model, the rest by another:
    the mid-rebuild shape, where a filtered HNSW scan without ``iterative_scan`` comes
    back short. The arm, forced onto the HNSW plan, must return exactly what a
    non-index pass over the identical ``WHERE`` returns. Four gates first, each a
    fixture defect when it trips:

    1. every vector is distinct, so the graph is real;
    2. the population fits inside ``hnsw.max_scan_tuples``, so that bound cannot bind
       and equality holds by construction;
    3. the ground truth holds at least 30 rows, at the floor override of 0 read back
       from the workspace (the predicate still drops every negative-cosine row, about
       half the recipe);
    4. after the assertion, the same forced statement with ``iterative_scan`` off
       returns strictly fewer rows: the guarded mechanism is load-bearing here. At
       2,000 memories both settings returned 50 of 50, which is why this is 5,000.
    """
    provider = _RecipeQueryProvider()
    _install(monkeypatch, "e4-recipe", provider)
    _select(monkeypatch, "e4-recipe")
    _seed_recipe_population(retrieval)
    retrieval.setting(DENSE_FLOOR_PERCENT_KEY, "0", ValueType.INT)
    ctx = retrieval.context()
    embedding = memory_tables.memory_embedding

    def request_in(uow: UnitOfWork) -> SearchRequest:
        return SearchRequest(
            query="the query text is ignored by this provider",
            mode=ReadMode.CURRENT,
            memory=_request(ctx, uow),
            limit=CANDIDATE_SCAN_LIMIT,
            k=RECALL_K_DEFAULT,
        )

    with retrieval.reading() as uow:
        total, distinct = uow.connection.execute(
            select(
                func.count(), func.count(func.distinct(cast(embedding.c.vector, Text)))
            )
        ).one()
        floor = dense_floor_percent(ctx, uow)
    _gate(total == distinct, f"{total} vectors but {distinct} distinct")
    _gate(total <= HNSW_MAX_SCAN_TUPLES, f"{total} vectors exceed the scan bound")
    _gate(floor == 0, f"the floor override reads back as {floor}, not 0")

    with retrieval.reading() as uow:
        uow.connection.execute(text("SET LOCAL enable_indexscan = off"))
        uow.connection.execute(text("SET LOCAL enable_bitmapscan = off"))
        truth_statement = build_dense_statement(
            request_in(uow),
            model_id=provider.model_id,
            query_vector=_RECIPE_QUERY,
            floor_percent=floor,
        )
        truth = [row[0] for row in uow.connection.execute(truth_statement)]
        truth_plan = _explain(uow, truth_statement)
    _gate(
        len(truth) >= _GROUND_TRUTH_MINIMUM,
        f"the ground truth holds {len(truth)} rows; the recipe gives about 50",
    )
    _gate(_HNSW_INDEX not in truth_plan, f"the ground truth used HNSW:\n{truth_plan}")

    with retrieval.reading() as uow:
        _load_pgvector(uow)
        _forced_hnsw(uow)
        request = request_in(uow)
        result = DenseStrategy().search(ctx, uow, request)
        plan = _explain(
            uow,
            build_dense_statement(
                request,
                model_id=provider.model_id,
                query_vector=_RECIPE_QUERY,
                floor_percent=dense_floor_percent(ctx, uow),
            ),
        )

    assert result.dense_available is True
    assert result.arms.dense == len(truth)
    assert {hit.ref for hit in result.hits} == set(truth)
    assert f"Index Scan using {_HNSW_INDEX}" in plan, plan

    with retrieval.reading() as uow:
        _forced_hnsw(uow)
        uow.connection.execute(hnsw_scan_settings(CANDIDATE_SCAN_LIMIT))
        uow.connection.execute(text("SET LOCAL hnsw.iterative_scan = off"))
        unguarded = uow.connection.execute(
            build_dense_statement(
                request_in(uow),
                model_id=provider.model_id,
                query_vector=_RECIPE_QUERY,
                floor_percent=floor,
            )
        ).all()
    _gate(
        len(unguarded) < len(truth),
        "the guarded mechanism is not load-bearing on this fixture: "
        f"iterative_scan off returned {len(unguarded)} of {len(truth)}",
    )


# --- hybrid, and the setting that picks a strategy ------------------------------------


def _recall_under(
    ws: RetrievalWorkspace, strategy: str, **payload: object
) -> RecallResult:
    """Mechanism (c): store the strategy as the workspace override, then recall."""
    ws.set_strategy(strategy)
    return _recalled(ws.recall(**payload))


def _arms_under(
    ws: RetrievalWorkspace, strategy: str, *, query: str, k: int
) -> ArmProvenance:
    """The strategy's own pre-walk arm lengths, read the way ``recall()`` would ask.

    Internal diagnostics: ``recall()``'s response counts only the returned items
    (#121), so a test about an arm's width reads the strategy directly."""
    ctx = ws.context()
    with ws.reading() as uow:
        return (
            STRATEGY_REGISTRY[strategy]
            .search(
                ctx,
                uow,
                SearchRequest(
                    query=query,
                    mode=ReadMode.CURRENT,
                    memory=_request(ctx, uow),
                    limit=CANDIDATE_SCAN_LIMIT,
                    k=k,
                ),
            )
            .arms
        )


def _seed_ordered(ws: RetrievalWorkspace, *texts: tuple[str, str]) -> list[UUID]:
    """Like ``_seed``, with each row a second newer than the one before it."""
    with ws.unit() as uow:
        return [
            _write(uow, _row(title, body, recorded_at=_recent(offset)))
            for offset, (title, body) in enumerate(texts)
        ]


def _unreadable_source() -> str:
    """A reference nothing in this harness resolves, so the permission walk denies
    every memory linked to it."""
    return f"harness.note:{uuid7()}"


def _write_linked(uow: UnitOfWork, row: MemoryRow, ref: str) -> UUID:
    memory_id = _write(uow, row)
    insert_memory_link(
        uow.connection,
        MemoryLinkRow(
            memory_id=memory_id,
            ref=ref,
            relation="about",
            created_at=row.recorded_at,
            supersession_lineage=False,
        ),
    )
    return memory_id


def _above_floor(
    ws: RetrievalWorkspace, *, model_id: str, query_vector: Sequence[float]
) -> set[UUID]:
    """The memories the dense arm's predicate admits, floor included, by a non-index
    pass (no ``ORDER BY`` on the distance): the arm's ground truth before its limit."""
    ctx = ws.context()
    embedding = memory_tables.memory_embedding
    similarity = dense_similarity(dense_distance(query_vector))
    with ws.reading() as uow:
        return set(
            uow.connection.execute(
                select(memory_tables.memory.c.id)
                .select_from(
                    memory_tables.memory.join(
                        embedding, embedding.c.memory_id == memory_tables.memory.c.id
                    )
                )
                .where(
                    embedding.c.model_id == model_id,
                    dense_floor(similarity, dense_floor_percent(ctx, uow)),
                    *row_local_conditions(_request(ctx, uow), ReadMode.CURRENT),
                )
            ).scalars()
        )


def _vectors(ws: RetrievalWorkspace, model_id: str) -> set[UUID]:
    """The memories holding a row for ``model_id``."""
    embedding = memory_tables.memory_embedding
    with ws.reading() as uow:
        return set(
            uow.connection.execute(
                select(embedding.c.memory_id).where(embedding.c.model_id == model_id)
            ).scalars()
        )


@dataclass
class _Counting:
    """A registered strategy, unchanged, counting its ``search`` calls."""

    inner: RetrievalStrategy
    calls: int = 0

    @property
    def name(self) -> str:
        return self.inner.name

    def index(self, ctx: WorkspaceContext, uow: Any, item: IndexItem) -> None:
        self.inner.index(ctx, uow, item)

    def invalidate(self, ctx: WorkspaceContext, uow: Any, ref: str) -> None:
        self.inner.invalidate(ctx, uow, ref)

    def search(
        self, ctx: WorkspaceContext, uow: Any, request: SearchRequest
    ) -> SearchResult:
        self.calls += 1
        return self.inner.search(ctx, uow, request)


def test_the_stored_setting_picks_which_registered_strategy_runs(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 1, second half. Mechanism (c).

    Every registered strategy is wrapped in a counting spy and each name is stored in
    turn: only the chosen one's ``search`` runs. Under ``hybrid`` the lexical and dense
    spies stay at zero as well, because hybrid runs arms of its own rather than the
    registry's, so a test that swaps a registry entry sees only what the setting chose.
    """
    _seed(retrieval, ("apples", "a note about apples"))
    spies = {
        name: _Counting(STRATEGY_REGISTRY[name])
        for name in (STRATEGY_LEXICAL, STRATEGY_DENSE, STRATEGY_HYBRID)
    }
    for name, spy in spies.items():
        monkeypatch.setitem(STRATEGY_REGISTRY, name, spy)

    for chosen in spies:
        for spy in spies.values():
            spy.calls = 0
        result = _recall_under(retrieval, chosen, query="apples")
        assert result.provenance.strategy == chosen
        assert {name: spy.calls for name, spy in spies.items()} == {
            name: int(name == chosen) for name in spies
        }, chosen


# --- AC 6, the hybrid half: no usable dense arm is the lexical answer, never a refusal

_AC6_ROWS = (
    ("apples", "apples apples apples, and a body"),
    ("one apple", "a single apples mention"),
    ("tie a", "apples in the body"),
    ("tie b", "apples in the body"),
    ("pears", "a body about pears"),
)
"""Four lexical matches at three densities, two of them tied on text so ``lexical``
orders them oldest first, and one row that does not match at all."""

_AC6_CASES = ("no provider", "provider raises", "no rows for its model", "wrong width")


def _dense_arm_unusable(
    ws: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch, case: str
) -> str:
    """Leave the dense arm unable to contribute, the way ``case`` names; answer the
    cause its warning must give."""
    if case == "no provider":
        _select(monkeypatch, EMBEDDING_PROVIDER_NONE)
        return NO_PROVIDER
    if case == "provider raises":
        _install(monkeypatch, "raising", _RaisingProvider())
        _select(monkeypatch, "raising")
        return PROVIDER_RAISED
    fake = _fake(monkeypatch)
    if case == "no rows for its model":
        retired = _RenamedProvider(model_id="rheo-test/retired-model", inner=fake)
        _fill(ws, retired)
        _gate(
            _vectors(ws, fake.model_id) == set()
            and bool(_vectors(ws, retired.model_id)),
            "expected rows by another model only",
        )
        return NO_EMBEDDINGS
    assert case == "wrong width", case
    _fill(ws, fake)
    _install(monkeypatch, "wrong-width", _WrongWidthProvider())
    _select(monkeypatch, "wrong-width")
    return STATEMENT_RAISED


@pytest.mark.parametrize("case", _AC6_CASES)
def test_hybrid_with_no_usable_dense_arm_answers_the_lexical_rows_in_order(
    retrieval: RetrievalWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    """AC 6, the hybrid half. Mechanism (c).

    Whatever stops the dense arm, ``hybrid`` answers what ``lexical`` answers over the
    same rows, in the same order, labelled ``hybrid``, with ``dense_available`` false,
    and ``ok``. The scores are the one-armed fusion's, ``1 / (60 + rank)``, never the
    lexical arm's own. The ``wrong width`` case is the one that fails in SQL: the query
    vector reaches the database, the dense statement raises inside its savepoint, and
    the lexical arm's rows still come back in the same transaction. No provider is
    logged at debug, being the shipped default; the other three causes are warnings.
    """
    _seed_ordered(retrieval, *_AC6_ROWS)
    cause = _dense_arm_unusable(retrieval, monkeypatch, case)
    lexical = _recall_under(retrieval, STRATEGY_LEXICAL, query="apples", k=50)
    _gate(
        len(lexical.items) == 4,
        f"{len(lexical.items)} lexical matches, not the fixture's four",
    )
    retrieval.set_strategy(STRATEGY_HYBRID)
    caplog.clear()

    with caplog.at_level(logging.DEBUG, logger=_DENSE_LOGGER):
        outcome = retrieval.recall(query="apples", k=50)

    assert outcome.ok, outcome
    result = _recalled(outcome)
    assert [item.ref for item in result.items] == [item.ref for item in lexical.items]
    assert [item.score for item in result.items] == [1 / 61, 1 / 62, 1 / 63, 1 / 64]
    assert {item.strategy for item in result.items} == {STRATEGY_HYBRID}
    assert result.provenance == RecallProvenance(
        strategy=STRATEGY_HYBRID,
        arms=ArmCounts(lexical=lexical.provenance.arms.lexical, dense=0),
        dense_available=False,
    )
    assert result.provenance.arms.lexical > 0
    record = _one_dense_line(
        caplog,
        logging.DEBUG if cause == NO_PROVIDER else logging.WARNING,
        "apples",
        *(part for row in _AC6_ROWS for part in row),
    )
    assert record.dense_cause == cause
    if case == "wrong width":
        assert record.error_class == pg_errors.DataException.__name__
        assert record.error_class != ValueError.__name__
        assert record.provider_model_id == FAKE_MODEL_ID
        assert record.provider_dimensions == EMBEDDING_DIMENSIONS


def test_hybrid_over_partial_coverage_reports_the_dense_arm_available(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 6's companion under ``hybrid``, through ``recall()``. Mechanism (c).

    One memory is embedded and one is not yet: the normal steady state, so the dense
    arm is available. The memory it cannot see is still answered, by the lexical arm.
    """
    fake = _fake(monkeypatch)
    covered, pending = _seed(
        retrieval,
        ("apples", "a note about apples"),
        ("apple pie", "a note about apple pie"),
    )
    covered_item = _item(retrieval, covered)
    with retrieval.unit() as uow:
        DenseStrategy().index_many(uow, [covered_item], provider=fake)
    vectors = _vectors(retrieval, fake.model_id)
    _gate(pending not in vectors, "the pending memory already has its row")
    _gate(bool(vectors), "the workspace holds no row for the resolved model")

    result = _recall_under(retrieval, STRATEGY_HYBRID, query="apple pie")

    assert result.provenance.strategy == STRATEGY_HYBRID
    assert result.provenance.dense_available is True
    assert memory_reference(pending) in [item.ref for item in result.items]


def test_hybrid_on_the_real_provider_reports_the_dense_arm_available(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 7 under ``hybrid``, on the real provider over AC 3's pair. Mechanism (c).

    With AC 6's hybrid half, both values of ``dense_available`` are exercised through
    ``recall()`` under ``hybrid`` (AC 8). The pair shares no term, so what came back
    came from the dense arm alone.
    """
    provider = _local(monkeypatch)
    (shopping,) = _seed(retrieval, _AC3_MEMORY)
    _fill(retrieval, provider)

    result = _recall_under(retrieval, STRATEGY_HYBRID, query=_AC3_QUERY)

    assert result.provenance.strategy == STRATEGY_HYBRID
    assert result.provenance.dense_available is True
    assert result.provenance.arms.dense > 0
    assert result.provenance.arms.lexical == 0
    assert [item.ref for item in result.items] == [memory_reference(shopping)]


# --- AC 9 and the walk depth: the multiplier widens the dense arm and nothing else ----

_AC9_K = 2
_AC9_MEMORIES = 12


@pytest.mark.parametrize("stored_multiplier", [None, 4])
def test_the_dense_arm_is_k_times_the_multiplier_wide_and_the_lexical_arm_is_not(
    retrieval: RetrievalWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    stored_multiplier: int | None,
) -> None:
    """AC 9. Mechanism (c), at the default multiplier and at a stored one.

    Gated first: more rows clear the floor than ``k * multiplier``, so the arm's
    length measures the multiplier and not how many rows were available; and lexical
    matches more than that, so a multiplied lexical arm would show.
    """
    fake = _fake(monkeypatch)
    _seed(
        retrieval,
        *[
            (f"apples {n}", f"a note about apples, number {n}")
            for n in range(1, _AC9_MEMORIES + 1)
        ],
    )
    _fill(retrieval, fake)
    multiplier = OVERFETCH_MULTIPLIER_DEFAULT
    if stored_multiplier is not None:
        retrieval.setting(
            OVERFETCH_MULTIPLIER_KEY, str(stored_multiplier), ValueType.INT
        )
        multiplier = stored_multiplier
    width = _AC9_K * multiplier
    (query_vector,) = fake.embed(["apples"])
    above_floor = _above_floor(
        retrieval, model_id=fake.model_id, query_vector=query_vector
    )
    _gate(
        len(above_floor) > width,
        f"{len(above_floor)} rows clear the floor: availability, not the "
        f"multiplier, would decide an arm of {width}",
    )
    lexical = _arms_under(retrieval, STRATEGY_LEXICAL, query="apples", k=_AC9_K)
    _gate(
        lexical.lexical > width,
        f"lexical matches {lexical.lexical}, too few to show a "
        f"multiplied arm of {width}",
    )

    arms = _arms_under(retrieval, STRATEGY_HYBRID, query="apples", k=_AC9_K)
    result = _recall_under(retrieval, STRATEGY_HYBRID, query="apples", k=_AC9_K)

    assert arms.dense == width
    assert arms.lexical == lexical.lexical
    assert result.provenance.dense_available is True
    assert len(result.items) == _AC9_K
    # The response counts the two returned items, never the arms' widths (#121).
    assert result.provenance.arms.lexical <= _AC9_K
    assert result.provenance.arms.dense <= _AC9_K


_WALK_K = 2
_WALK_DENIED = 8
_WALK_FILLER = (
    "ledger invoice quarter harbour violin granite meadow lantern orbit saddle "
    "thimble walnut canyon marble pepper tunnel velvet anchor bishop cobalt"
)
"""Twenty words: a readable row's body. It puts ``apples`` low in the lexical order
and, under the fake provider, below the dense floor."""


def test_hybrid_walks_as_far_as_lexical_when_the_top_of_the_order_is_denied(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk-depth invariant. Mechanism (c), with the arms read directly to gate.

    Eight lexical matches at the top of the order are linked to a source this caller
    cannot read; the two readable matches sit below them and below the dense floor,
    so only the lexical arm carries them. ``lexical`` walks down to both. ``hybrid``
    must as well: with its lexical arm cut to ``k * multiplier`` the walk would get
    only denied rows, and the caller fewer than ``k``.
    """
    fake = _fake(monkeypatch)
    width = _WALK_K * OVERFETCH_MULTIPLIER_DEFAULT
    with retrieval.unit() as uow:
        denied = {
            _write_linked(
                uow,
                _row("apples", "apples apples apples", recorded_at=_recent(n)),
                _unreadable_source(),
            )
            for n in range(_WALK_DENIED)
        }
        readable = {
            _write(
                uow,
                _row("apples", f"{_WALK_FILLER} {n}", recorded_at=_recent(100 + n)),
            )
            for n in range(_WALK_K)
        }
    _fill(retrieval, fake)
    (query_vector,) = fake.embed(["apples"])
    above_floor = _above_floor(
        retrieval, model_id=fake.model_id, query_vector=query_vector
    )
    _gate(not above_floor & readable, "a readable row clears the dense floor")
    _gate(len(above_floor) > width, f"{len(above_floor)} rows clear the dense floor")
    ctx = retrieval.context()
    with retrieval.reading() as uow:
        lexical_arm = LexicalStrategy().search(
            ctx,
            uow,
            SearchRequest(
                query="apples",
                mode=ReadMode.CURRENT,
                memory=_request(ctx, uow),
                limit=CANDIDATE_SCAN_LIMIT,
                k=_WALK_K,
            ),
        )
    lexical_order = [hit.ref for hit in lexical_arm.hits]
    _gate(
        set(lexical_order[:_WALK_DENIED]) == denied
        and set(lexical_order[_WALK_DENIED:]) == readable,
        "the denied rows are not the top of the lexical order",
    )
    lexical = _recall_under(retrieval, STRATEGY_LEXICAL, query="apples", k=_WALK_K)
    _gate(
        {item.ref for item in lexical.items} == {memory_reference(r) for r in readable},
        "lexical did not walk down to both readable rows",
    )

    hybrid_arms = _arms_under(retrieval, STRATEGY_HYBRID, query="apples", k=_WALK_K)
    hybrid = _recall_under(retrieval, STRATEGY_HYBRID, query="apples", k=_WALK_K)

    assert hybrid_arms.dense == width
    assert hybrid.provenance.dense_available is True
    # Only the lexical arm carries the readable rows, and the denied ones above them
    # are counted nowhere a caller sees (#121).
    assert hybrid.provenance.arms == ArmCounts(lexical=_WALK_K, dense=0)
    assert len(hybrid.items) == len(lexical.items)
    assert {item.ref for item in hybrid.items} == {item.ref for item in lexical.items}


_NARROW_LIMIT = 3
_WORDY_BODY = "apples apples apples apples zebra yak xenon walrus"
"""Five ``apples`` with the title, so lexical ranks it above a two-``apples`` row; four
other words, so under the fake provider it sits further from the query than one."""


def test_hybrid_is_bounded_by_the_request_limit_in_its_dense_arm_and_its_answer(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanism (a), the only way to ask for fewer than the scan bound: ``recall()``
    always asks for it. Asked for three with ``k`` of ten, the dense arm is three wide,
    not ``k * multiplier``, and three positions are handed on, not the union.

    Gated so neither bound holds by accident: more rows clear the dense floor than the
    limit, and the two arms' top three are different memories, so their union is
    longer than the limit.
    """
    fake = _fake(monkeypatch)
    with retrieval.unit() as uow:
        # The query's own text, so the query's own vector: the dense arm's top three.
        near = {
            _write(uow, _row("apples", "apples", recorded_at=_recent(n)))
            for n in range(_NARROW_LIMIT)
        }
        wordy = {
            _write(uow, _row("apples", _WORDY_BODY, recorded_at=_recent(10 + n)))
            for n in range(_NARROW_LIMIT)
        }
    _fill(retrieval, fake)
    (query_vector,) = fake.embed(["apples"])
    above_floor = _above_floor(
        retrieval, model_id=fake.model_id, query_vector=query_vector
    )
    _gate(
        len(above_floor) > _NARROW_LIMIT,
        f"{len(above_floor)} rows clear the floor",
    )
    _gate(
        RECALL_K_DEFAULT * OVERFETCH_MULTIPLIER_DEFAULT > _NARROW_LIMIT,
        "the over-fetch width would not exceed the limit",
    )
    ctx = retrieval.context()
    with retrieval.reading() as uow:
        request = SearchRequest(
            query="apples",
            mode=ReadMode.CURRENT,
            memory=_request(ctx, uow),
            limit=_NARROW_LIMIT,
            k=RECALL_K_DEFAULT,
        )
        dense_top = {hit.ref for hit in DenseStrategy().search(ctx, uow, request).hits}
        lexical_top = {
            hit.ref for hit in LexicalStrategy().search(ctx, uow, request).hits
        }
        result = HybridStrategy().search(ctx, uow, request)
    _gate(dense_top == near, "the near rows are not the dense arm's top three")
    _gate(lexical_top == wordy, "the wordy rows are not the lexical arm's top three")

    assert result.dense_available is True
    assert result.arms == ArmProvenance(lexical=_NARROW_LIMIT, dense=_NARROW_LIMIT)
    assert len(result.hits) == _NARROW_LIMIT


# --- AC 15 and AC 16: the permission walk and the closure, with a live dense arm ------


@pytest.mark.parametrize("strategy", [STRATEGY_DENSE, STRATEGY_HYBRID])
def test_a_memory_whose_source_is_unreadable_is_reached_and_dropped(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch, strategy: str
) -> None:
    """AC 15: project criterion 27 under ``dense`` and under ``hybrid``. Mechanism (a)
    to show the walk reaches it, (c) to show it is dropped.

    The denied memory's text is the query's one word, twice, so under the fake
    provider its vector is the query's own: the dense arm's top candidate by
    construction. It is in the strategy's ranked list before the walk, so the walk is
    shown to have reached it and dropped it, not to have stopped short of it.
    """
    fake = _fake(monkeypatch)
    with retrieval.unit() as uow:
        readable = _write(uow, _row("apples one", "apples, readable"))
        denied = _write_linked(
            uow, _row("apples", "apples", recorded_at=_recent(1)), _unreadable_source()
        )
    _fill(retrieval, fake)
    ctx = retrieval.context()
    with retrieval.reading() as uow:
        request = SearchRequest(
            query="apples",
            mode=ReadMode.CURRENT,
            memory=_request(ctx, uow),
            limit=CANDIDATE_SCAN_LIMIT,
            k=RECALL_K_DEFAULT,
        )
        dense_arm = DenseStrategy().search(ctx, uow, request)
        ranked = STRATEGY_REGISTRY[strategy].search(ctx, uow, request)
    assert dense_arm.hits[0].ref == denied
    assert denied in [hit.ref for hit in ranked.hits]

    result = _recall_under(retrieval, strategy, query="apples")

    assert result.provenance.strategy == strategy
    assert result.provenance.dense_available is True
    assert [item.ref for item in result.items] == [memory_reference(readable)]
    # #121: the denied memory is the dense arm's top candidate and counts nowhere.
    # Each count is at most the one returned item, whichever arms ranked it.
    assert result.provenance.arms.lexical <= 1
    assert result.provenance.arms.dense <= 1
    assert result.provenance.arms.dense == (
        1 if readable in {hit.ref for hit in dense_arm.hits} else 0
    )


def test_hidden_memories_move_no_hybrid_score_or_position(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#125, under ``hybrid`` with a live dense arm. Mechanism (c).

    Three readable memories answer ``apples``; the baseline is their items, scores,
    order and provenance. Then three hidden ones are added: ``workspace``-audience,
    so the row-local prefilter admits them, and linked to a source this caller cannot
    read, so only full eligibility removes them. Their text is the query's own word,
    so they top both arms, which is gated on the raw arms. The answer must not move by
    one position or one digit of score: fused over the raw arms, each readable item
    would sit three ranks lower and score less.

    Fewer hidden rows than the dense arm is wide, so the documented arm-width
    residual (enough hidden rows pushing a readable one out of that arm) is not what
    this measures.
    """
    fake = _fake(monkeypatch)
    with retrieval.unit() as uow:
        readable = [
            _write(uow, _row("apples", "apples and pears", recorded_at=_recent(10))),
            _write(uow, _row("apple pie", "an apples recipe", recorded_at=_recent(11))),
            _write(
                uow, _row("orchard", "apples in the orchard", recorded_at=_recent(12))
            ),
        ]
    _fill(retrieval, fake)
    before = _recall_under(retrieval, STRATEGY_HYBRID, query="apples")
    _gate(before.provenance.dense_available, "the dense arm is unavailable")
    _gate(
        {item.ref for item in before.items}
        == {memory_reference(memory_id) for memory_id in readable},
        "the baseline is not the three readable memories",
    )
    _gate(
        before.provenance.arms.dense > 0 and before.provenance.arms.lexical > 0,
        f"one arm contributed nothing: {before.provenance.arms}",
    )

    with retrieval.unit() as uow:
        hidden = {
            _write_linked(
                uow,
                _row("apples", "apples apples", recorded_at=_recent(n)),
                _unreadable_source(),
            )
            for n in range(3)
        }
    _fill(retrieval, fake)
    width = RECALL_K_DEFAULT * OVERFETCH_MULTIPLIER_DEFAULT
    ctx = retrieval.context()
    with retrieval.reading() as uow:
        request = SearchRequest(
            query="apples",
            mode=ReadMode.CURRENT,
            memory=_request(ctx, uow),
            limit=CANDIDATE_SCAN_LIMIT,
            k=RECALL_K_DEFAULT,
        )
        raw_lexical = LexicalStrategy().search(ctx, uow, request)
        raw_dense = DenseStrategy().search(ctx, uow, request)
    _gate(len(hidden) + len(readable) <= width, "the dense arm is too narrow")
    _gate(
        {hit.ref for hit in raw_lexical.hits[: len(hidden)]} == hidden,
        "the hidden memories are not the top of the lexical arm",
    )
    _gate(
        {hit.ref for hit in raw_dense.hits[: len(hidden)]} == hidden,
        "the hidden memories are not the top of the dense arm",
    )

    after = _recall_under(retrieval, STRATEGY_HYBRID, query="apples")

    assert [(item.ref, item.score) for item in after.items] == [
        (item.ref, item.score) for item in before.items
    ]
    assert after.provenance == before.provenance


@pytest.mark.parametrize("change", ["correct", "supersede"])
def test_a_vector_written_by_index_goes_with_the_closure(
    retrieval: RetrievalWorkspace, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """AC 16: project criterion 29's closure, over vectors ``DenseStrategy.index()``
    wrote. ``target`` changes, ``dependant`` derives from it, ``bystander`` does not:
    all three hold a real vector before, and only the bystander's survives."""
    fake = _fake(monkeypatch)
    with retrieval.unit() as uow:
        target = _write(uow, _row("apples", "a note about apples"))
        dependant = _write(
            uow, _row("apple summary", "a summary of apples", recorded_at=_recent(1))
        )
        bystander = _write(
            uow, _row("pears", "a note about pears", recorded_at=_recent(2))
        )
        insert_memory_link(
            uow.connection,
            MemoryLinkRow(
                memory_id=dependant,
                ref=memory_reference(target),
                relation="derived_from",
                created_at=_recent(1),
                supersession_lineage=False,
            ),
        )
    items = [
        _item(retrieval, memory_id) for memory_id in (target, dependant, bystander)
    ]
    ctx = retrieval.context()
    with retrieval.unit() as uow:
        for item in items:
            DenseStrategy().index(ctx, uow, item)
    assert _vectors(retrieval, fake.model_id) == {target, dependant, bystander}

    if change == "correct":
        outcome = retrieval.call(
            MEMORY_CORRECT,
            {
                "ref": memory_reference(target),
                "expected_revision": 1,
                "title": "pears",
                "body": "it was pears all along",
            },
        )
        assert outcome.ok, outcome
        assert isinstance(outcome.result, MemoryCorrected), outcome
    else:
        outcome = retrieval.call(
            MEMORY_SUPERSEDE,
            {
                "ref": memory_reference(target),
                "expected_revision": 1,
                "kind": "note",
                "title": "pears",
                "body": "what the record says now",
            },
        )
        assert outcome.ok, outcome
        assert isinstance(outcome.result, MemorySuperseded), outcome

    assert _vectors(retrieval, fake.model_id) == {bystander}
