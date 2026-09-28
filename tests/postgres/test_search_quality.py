"""Forward-looking synthetic relevance gates through the actual search operations.

Real MiniLM vectors, the shipped relevance floor and real pgvector retrieval. Missing
runtime/model, degraded dense reads or wrong strategy fail rather than skip or fall
back. These few authored cases are regressions, not production-quality certification.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import install_and_enable_module, loaded_probe_modules
from harness.registry import add_member
from harness.search_quality import score
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.operations.registry import OperationRegistry
from rheo_core.settings import ValueType
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.repositories import upsert_workspace_setting
from rheo_recallatron import MANIFEST
from rheo_recallatron.configuration import RETRIEVAL_STRATEGY_KEY
from rheo_recallatron.contracts import MemoryWritten
from rheo_recallatron.embedding import registry as embedding_registry
from rheo_recallatron.embedding.local import MODEL_ID, LocalEmbeddingProvider
from rheo_recallatron.embedding.rebuild import fill_missing_embeddings
from rheo_recallatron.history import HISTORY_SEARCH, HistorySearchResult
from rheo_recallatron.migration.history_extract import (
    HistoryExtractUnit,
    dump_history_extract,
    parse_history_extract,
)
from rheo_recallatron.migration.history_import import import_history
from rheo_recallatron.operations import MEMORY_RECALL, MEMORY_REMEMBER, RecallResult
from sqlalchemy import Engine

pytestmark = pytest.mark.postgres

_CORPUS = json.loads(
    (Path(__file__).parents[1] / "fixtures/recallatron/search-quality.json").read_text()
)
_RECORDS = _CORPUS["records"]
_CASES = _CORPUS["cases"]
_PASSES = [
    pytest.param(strategy, case, id=f"{strategy}-{case['id']}")
    for case in _CASES
    for strategy in case["strategies"]
]


@dataclass(frozen=True)
class SearchWorkspace:
    owner: WorkspaceContext
    member: WorkspaceContext
    operations: OperationRegistry
    database: str
    engine: Engine

    def call(
        self, name: str, payload: dict[str, object], *, member: bool = False
    ) -> OperationOutcome:
        return dispatch(
            self.member if member else self.owner,
            name,
            payload,
            registry=self.operations,
            consumers=ConsumerRegistry(),
        )


@pytest.fixture
def search_workspace(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[SearchWorkspace]:
    register_core_operations()
    with loaded_probe_modules(monkeypatch, MANIFEST.module_id) as surfaces:
        owner = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(owner, WorkspaceContext)
        install_and_enable_module(cluster.backend, owner, workspace, MANIFEST.module_id)
        member_id = add_member(
            cluster.backend, workspace, Role.MEMBER, display_name="synthetic-reviewer"
        )
        member = context_for_harness(workspace, member_id, Role.MEMBER)
        assert isinstance(member, WorkspaceContext)
        database = cluster.registry_row(workspace).database_name
        yield SearchWorkspace(
            owner,
            member,
            surfaces.operations,
            database,
            cluster.backend.pools.engine_for(database),
        )


@pytest.fixture(scope="session")
def local_provider() -> LocalEmbeddingProvider:
    assert find_spec("fastembed") is not None, (
        "Quality gates require `uv sync --frozen --extra local-embeddings`; "
        "fake vectors are not a quality oracle."
    )
    return LocalEmbeddingProvider()


def _select_strategy(search: SearchWorkspace, strategy: str) -> None:
    with UnitOfWork(search.engine, search.database) as uow:
        upsert_workspace_setting(
            uow.connection,
            key=RETRIEVAL_STRATEGY_KEY,
            value=strategy,
            value_type=ValueType.STR,
            updated_by=None,
        )
        uow.commit()


@pytest.fixture
def curated_corpus(search_workspace: SearchWorkspace) -> dict[str, str]:
    refs = {}
    for record in _RECORDS:
        result = search_workspace.call(
            MEMORY_REMEMBER,
            {"kind": "note", "title": record["title"], "body": record["body"]},
        )
        assert result.ok and isinstance(result.result, MemoryWritten), result
        refs[result.result.ref] = record["id"]
    return refs


@pytest.mark.parametrize(("strategy", "case"), _PASSES)
def test_curated_search_answers_independent_questions(
    search_workspace: SearchWorkspace,
    curated_corpus: dict[str, str],
    local_provider: LocalEmbeddingProvider,
    monkeypatch: pytest.MonkeyPatch,
    strategy: str,
    case: dict,
) -> None:
    # Neither ambient criterion-31 variables nor a configured fake provider may
    # silently turn this matrix into another strategy/provider pass.
    monkeypatch.setattr(embedding_registry, "configured_provider_name", lambda: "local")
    monkeypatch.setitem(embedding_registry.providers(), "local", local_provider)
    assert embedding_registry.resolve_provider() is local_provider
    assert local_provider.model_id == MODEL_ID
    _select_strategy(search_workspace, strategy)
    if strategy != "lexical":
        with UnitOfWork(search_workspace.engine, search_workspace.database) as uow:
            fill_missing_embeddings(uow, provider=local_provider, batch_size=32)
            uow.commit()
    outcome = search_workspace.call(MEMORY_RECALL, {"query": case["query"], "k": 3})
    assert outcome.ok and isinstance(outcome.result, RecallResult), outcome
    result = outcome.result
    assert result.provenance.strategy == strategy
    assert result.provenance.dense_available == (strategy != "lexical")
    ranked = [curated_corpus[item.ref] for item in result.items]
    relevant = set(case["relevant"])
    measured = score(ranked, relevant, k=case["cutoff"])
    explanation = f"{case['id']} / {strategy}: {ranked}; {measured}"
    print(explanation)
    if not relevant:
        assert ranked == [], explanation
        assert measured.recall_at_k is None, explanation
    else:
        assert measured.recall_at_k == 1.0, explanation
        assert measured.precision_at_k >= len(relevant) / case["cutoff"], explanation
        assert measured.reciprocal_rank >= 1 / case["first_relevant_rank_max"], (
            explanation
        )


def test_quality_fixture_is_labelled_independently_of_search() -> None:
    ids = {record["id"] for record in _RECORDS}
    assert _CORPUS["origin"] == "synthetic"
    assert len(ids) == len(_RECORDS)
    assert len({case["id"] for case in _CASES}) == len(_CASES)
    for case in _CASES:
        assert len(case["relevant"]) == len(set(case["relevant"]))
        assert set(case["relevant"]) <= ids
        assert set(case["strategies"]) <= {"lexical", "dense", "hybrid"}
        assert case["query"] not in {record["title"] for record in _RECORDS}
        assert len(case["relevant"]) <= case["cutoff"] == 3
        if case["relevant"]:
            assert 1 <= case["first_relevant_rank_max"] <= case["cutoff"]
        else:
            assert case["first_relevant_rank_max"] is None


@pytest.fixture
def historical_corpus(search_workspace: SearchWorkspace) -> dict[str, str]:
    # One history-only claim on a different topic. It must remain findable as an
    # unconfirmed note, never leak into accepted recall or a member's search.
    units = [
        HistoryExtractUnit(
            external_source_key=f"synthetic:{record['id']}",
            source_reference=f"synthetic:{record['id']}",
            kind="conversation",
            status="turn",
            title=record["title"],
            body=record["body"],
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for record in _RECORDS
    ]
    units.append(
        HistoryExtractUnit(
            external_source_key="synthetic:tentative",
            source_reference="synthetic:tentative",
            kind="procedural_note",
            status="unconfirmed",
            title="Draft method",
            body="A tentative method uses vermiculite for orchid propagation.",
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    extract = parse_history_extract(
        dump_history_extract(
            units,
            source_sha256="a" * 64,
            unresolved_count=0,
        )
    )
    with UnitOfWork(search_workspace.engine, search_workspace.database) as uow:
        report = import_history(uow.connection, extract, imported_at=datetime.now(UTC))
        uow.commit()
    assert report.inserted == len(units)
    return {f"synthetic:{record['id']}": record["id"] for record in _RECORDS}


@pytest.mark.parametrize(
    "case",
    [case for case in _CASES if "lexical" in case["strategies"]],
    ids=lambda case: case["id"],
)
def test_history_search_relevance_stays_on_its_separate_surface(
    search_workspace: SearchWorkspace,
    historical_corpus: dict[str, str],
    case: dict,
) -> None:
    outcome = search_workspace.call(
        HISTORY_SEARCH, {"query": case["query"], "limit": 3}
    )
    assert outcome.ok and isinstance(outcome.result, HistorySearchResult), outcome
    ranked = [historical_corpus[item.source_reference] for item in outcome.result.items]
    measured = score(ranked, set(case["relevant"]), k=case["cutoff"])
    print(f"{case['id']} / history-lexical: {ranked}; {measured}")
    if not case["relevant"]:
        assert ranked == [], (case["id"], ranked, measured)
    else:
        assert measured.recall_at_k == 1.0, (case["id"], ranked, measured)
        assert measured.reciprocal_rank == 1.0, (case["id"], ranked, measured)
    assert all(
        item.kind == "conversation" and item.status == "turn"
        for item in outcome.result.items
    )


def test_history_only_claim_keeps_status_and_owner_boundary(
    search_workspace: SearchWorkspace,
    historical_corpus: dict[str, str],
    curated_corpus: dict[str, str],
) -> None:
    _select_strategy(search_workspace, "lexical")
    query = {"query": "vermiculite"}
    found = search_workspace.call(HISTORY_SEARCH, query)
    assert found.ok and isinstance(found.result, HistorySearchResult), found
    assert len(found.result.items) == 1
    note = found.result.items[0]
    assert (note.kind, note.status, note.source_reference) == (
        "procedural_note",
        "unconfirmed",
        "synthetic:tentative",
    )
    curated = search_workspace.call(MEMORY_RECALL, query)
    assert curated.ok and isinstance(curated.result, RecallResult), curated
    assert curated.result.items == ()
    denied = search_workspace.call(HISTORY_SEARCH, query, member=True)
    assert not denied.ok and denied.result is None
