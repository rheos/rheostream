"""``recallatron.memory.dedup_candidates`` against real workspaces and the real model.

Seams under test: the operation's answer through ``dispatch``, for a given fixture and
caller. Three properties, one test each or more: the pairs are bounded and ordered;
a pair either side of which the caller cannot read is dropped **whole**; and with no
provider, or no rows for its model, the answer is empty rather than refused.

**The real provider, for the pairs spec evidence E2 § 4 measured.** A floor-gated
fixture that trips reports a real change in the space, not a synthetic coincidence,
and each one is gated before it is used: the pair's cosine similarity is computed by
``1 - (a.vector <=> b.vector)`` over the two stored rows, in a statement with no
``ORDER BY`` on a distance, so no index answers it. A gate that fails raises
:class:`FixtureDefect`, never a skip. **The real-provider tests fail, never skip,**
without the ``local-embeddings`` extra.

Rows go in through the repository, never through ``remember``, so no embed job is
queued behind a test's back; vectors go in through ``fill_missing_embeddings``.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from harness.registry import add_member
from rheo_contracts import Idempotency, Role, SafetyClass, WorkspaceContext
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
    DEDUP_LIMIT_MAX,
    DEDUP_LIMIT_MIN,
    DEDUP_PAIR_FLOOR,
    EMBEDDING_BATCH_SIZE_DEFAULT,
    EMBEDDING_PROVIDER_NONE,
    MEMORY_RECORD_TYPE,
    RETRIEVAL_STRATEGY_KEY,
    STRATEGY_LEXICAL,
)
from rheo_recallatron.dedup import (
    DEDUP_DECLARATION,
    MEMORY_DEDUP_CANDIDATES,
    DedupCandidates,
    DedupPair,
)
from rheo_recallatron.eligibility import memory_reference
from rheo_recallatron.embedding import registry
from rheo_recallatron.embedding.fake import FakeEmbeddingProvider
from rheo_recallatron.embedding.local import LocalEmbeddingProvider
from rheo_recallatron.embedding.protocol import EmbeddingProvider
from rheo_recallatron.embedding.rebuild import fill_missing_embeddings
from rheo_recallatron.resolvers import resolve_memory
from rheo_recallatron.storage import tables as memory_tables
from rheo_recallatron.storage.repository import (
    MemoryLinkRow,
    MemoryPurposeRow,
    MemoryRow,
    insert_memory,
    insert_memory_link,
    insert_memory_purpose,
)
from sqlalchemy import Engine, Float, literal_column, select, true

pytestmark = pytest.mark.postgres

_MEMORY_MODULE = MANIFEST.module_id
_RESPOND = "respond"
_EXTRA_REMEDY = "uv sync --frozen --extra local-embeddings"

_SAME_BODY = "the body of a memory about apples"
_TWO_TITLES = (("a memory", _SAME_BODY), ("visible", _SAME_BODY))
"""One body under two titles: 0.908 on the shipped provider (E2 § 4)."""

_ONE_TEMPLATE = (("apples", "a note about apples"), ("pears", "a note about pears"))
"""Two distinct notes on one template: 0.707, 0.093 under the floor (E2 § 4)."""

_PARAPHRASE = (
    ("rent due", "pay the landlord 1200 on the first"),
    ("rent", "the landlord expects 1200 by the 1st of the month"),
)
"""The same fact in other words: 0.829 (E2 § 4)."""


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
class DedupWorkspace:
    """A workspace with Recallatron installed, enabled and migrated."""

    cluster: ClusterSession
    workspace: UUID
    owner_account_id: UUID
    surfaces: LoadedSurfaces
    database_name: str
    engine: Engine
    consumers: ConsumerRegistry

    def context(
        self, *, account_id: UUID | None = None, role: Role = Role.OWNER
    ) -> WorkspaceContext:
        ctx = context_for_harness(
            self.workspace,
            self.owner_account_id if account_id is None else account_id,
            role,
        )
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

    def dedup(
        self, ctx: WorkspaceContext | None = None, **payload: object
    ) -> OperationOutcome:
        return dispatch(
            self.context() if ctx is None else ctx,
            MEMORY_DEDUP_CANDIDATES,
            payload,
            registry=self.surfaces.operations,
            consumers=self.consumers,
        )

    def set_strategy(self, name: str) -> None:
        with self.unit() as uow:
            upsert_workspace_setting(
                uow.connection,
                key=RETRIEVAL_STRATEGY_KEY,
                value=name,
                value_type=ValueType.STR,
                updated_by=None,
            )


@pytest.fixture
def dedup(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[DedupWorkspace]:
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
        yield DedupWorkspace(
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


# --- seeding and reading --------------------------------------------------------------


def _row(
    title: str,
    body: str,
    *,
    audience_kind: str = "workspace",
    audience_id: UUID | None = None,
) -> MemoryRow:
    return MemoryRow(
        id=uuid7(),
        kind="note",
        title=title,
        body=body,
        audience_kind=audience_kind,
        audience_id=audience_id,
        confidence=None,
        occurred_at=None,
        recorded_at=datetime.now(UTC) - timedelta(days=1),
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
    insert_memory(uow.connection, row)
    insert_memory_purpose(
        uow.connection, MemoryPurposeRow(memory_id=row.id, purpose=_RESPOND)
    )
    return row.id


def _seed(ws: DedupWorkspace, *texts: tuple[str, str]) -> list[UUID]:
    with ws.unit() as uow:
        return [_write(uow, _row(title, body)) for title, body in texts]


def _fill(ws: DedupWorkspace, provider: EmbeddingProvider) -> None:
    with ws.unit() as uow:
        fill_missing_embeddings(
            uow, provider=provider, batch_size=EMBEDDING_BATCH_SIZE_DEFAULT
        )


def _unresolvable_link(ws: DedupWorkspace, memory_id: UUID) -> None:
    """An ``about`` link to a record nothing in this harness resolves, written as
    ``test_memory_records.py`` writes one: every caller's eligibility walk now denies
    the memory, while its row-local columns and its vector stay exactly as they were."""
    with ws.unit() as uow:
        insert_memory_link(
            uow.connection,
            MemoryLinkRow(
                memory_id=memory_id,
                ref=f"harness.note:{uuid7()}",
                relation="about",
                created_at=datetime.now(UTC),
                supersession_lineage=False,
            ),
        )


def _pair_similarity(
    ws: DedupWorkspace, first: UUID, second: UUID, *, model_id: str
) -> float:
    """``1 - (a.vector <=> b.vector)`` over the two stored rows, and nothing else: no
    ``ORDER BY`` on the distance, so no index can answer it."""
    embedding = memory_tables.memory_embedding
    a = embedding.alias("a")
    b = embedding.alias("b")
    similarity = literal_column("1", Float) - a.c.vector.op("<=>", return_type=Float)(
        b.c.vector
    )
    with ws.reading() as uow:
        return float(
            uow.connection.execute(
                select(similarity)
                .select_from(a.join(b, true()))
                .where(
                    a.c.memory_id == first,
                    a.c.model_id == model_id,
                    b.c.memory_id == second,
                    b.c.model_id == model_id,
                )
            ).scalar_one()
        )


def _clearing(
    ws: DedupWorkspace, memory_ids: Sequence[UUID], *, model_id: str
) -> dict[frozenset[UUID], float]:
    """Every pair of ``memory_ids`` at or above the floor, by the gate's own pass."""
    measured = {
        frozenset(pair): _pair_similarity(ws, *pair, model_id=model_id)
        for pair in itertools.combinations(memory_ids, 2)
    }
    return {
        pair: score for pair, score in measured.items() if score >= DEDUP_PAIR_FLOOR
    }


def _refs(first: UUID, second: UUID) -> tuple[str, str]:
    """A pair as the operation spells it: the two references, in sorted order."""
    ref_a, ref_b = sorted((memory_reference(first), memory_reference(second)))
    return ref_a, ref_b


def _pairs(outcome: OperationOutcome) -> tuple[DedupPair, ...]:
    """A successful answer's pairs. A refusal fails here, which is how every test
    below also proves the call was not refused."""
    assert outcome.ok, outcome
    assert isinstance(outcome.result, DedupCandidates), outcome
    return outcome.result.pairs


def _named(pairs: Sequence[DedupPair]) -> set[str]:
    return {pair.ref_a for pair in pairs} | {pair.ref_b for pair in pairs}


# --- the declaration ------------------------------------------------------------------


def test_dedup_candidates_is_declared_a_read_for_the_three_read_roles() -> None:
    """AC 17's declaration half: ``READ``, owner/member/service, no audit, on the
    manifest. Whether a tool reaches it is AC 18's, pinned in ``test_memory_mcp.py``."""
    assert DEDUP_DECLARATION.name == "recallatron.memory.dedup_candidates"
    assert DEDUP_DECLARATION.safety_class is SafetyClass.READ
    assert DEDUP_DECLARATION.roles == frozenset({Role.OWNER, Role.MEMBER, Role.SERVICE})
    assert DEDUP_DECLARATION.audit is None
    assert DEDUP_DECLARATION.idempotency is Idempotency.NONE
    assert MEMORY_DEDUP_CANDIDATES in {
        declaration.name for declaration, _ in MANIFEST.operations
    }


# --- AC 17: a pair either side of which the caller cannot read is dropped whole -------


@pytest.mark.parametrize("unreadable", ["first", "second"])
def test_a_pair_whose_one_side_has_an_unresolvable_link_is_dropped_whole(
    dedup: DedupWorkspace, monkeypatch: pytest.MonkeyPatch, unreadable: str
) -> None:
    """AC 17 and edge case 9, through the permission walk.

    The same two rows answer as a pair first, which is the positive control: without
    it, "excluded" would be indistinguishable from "never a candidate". Then one side
    gains a link the caller cannot resolve. The row-local SQL cannot see a link, so
    the pair still reaches the walk, and the walk must drop all of it: the readable
    side is not returned alone, and nothing is returned blank. Parametrized over which
    side sorts first, because the walk checks the two in order and must check both.
    """
    provider = _local(monkeypatch)
    first, second = sorted(_seed(dedup, *_TWO_TITLES))
    _fill(dedup, provider)
    similarity = _pair_similarity(dedup, first, second, model_id=provider.model_id)
    _gate(
        similarity >= DEDUP_PAIR_FLOOR,
        f"one body under two titles scores {similarity:.4f}, under the pair floor",
    )

    readable = _pairs(dedup.dedup())
    assert [(pair.ref_a, pair.ref_b) for pair in readable] == [_refs(first, second)]
    assert readable[0].score == pytest.approx(similarity, abs=1e-9)

    _unresolvable_link(dedup, first if unreadable == "first" else second)

    dropped = _pairs(dedup.dedup())
    assert dropped == ()
    assert _named(dropped).isdisjoint(_refs(first, second))


def test_a_pair_with_another_members_memory_is_dropped_whole(
    dedup: DedupWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 17 through the audience: one side is member-private to a second member.

    That member reads both sides and gets the pair, which is the positive control and
    also the member role calling the operation. The owner cannot read the private side,
    so the row-local audience condition keeps it out of the owner's candidate set, and
    the workspace memory beside it is not proposed with anything.
    """
    provider = _local(monkeypatch)
    member = add_member(
        dedup.cluster.backend, dedup.workspace, Role.MEMBER, display_name="member-two"
    )
    shared_text, private_text = _TWO_TITLES
    with dedup.unit() as uow:
        shared = _write(uow, _row(*shared_text))
        private = _write(
            uow, _row(*private_text, audience_kind="member", audience_id=member)
        )
    _fill(dedup, provider)
    similarity = _pair_similarity(dedup, shared, private, model_id=provider.model_id)
    _gate(
        similarity >= DEDUP_PAIR_FLOOR,
        f"one body under two titles scores {similarity:.4f}, under the pair floor",
    )

    theirs = _pairs(dedup.dedup(dedup.context(account_id=member, role=Role.MEMBER)))
    assert [(pair.ref_a, pair.ref_b) for pair in theirs] == [_refs(shared, private)]

    owners = _pairs(dedup.dedup())
    assert owners == ()
    assert _named(owners).isdisjoint(_refs(shared, private))


# --- empty, never refused -------------------------------------------------------------


def test_no_provider_or_no_rows_for_its_model_answers_empty_not_refused(
    dedup: DedupWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``lexical`` workspace, first with no provider configured and then with one
    configured whose model has written nothing here: an empty answer both times, and
    never a refusal (``_pairs`` asserts the call succeeded).

    The workspace does hold vectors throughout, the fake model's, so "empty" is not
    "there was nothing to compare". The last step is the positive control: the same
    two memories, once the configured model's rows exist, answer as a pair.
    """
    fake = _fake(monkeypatch)
    first, second = _seed(dedup, *_TWO_TITLES)
    _fill(dedup, fake)
    dedup.set_strategy(STRATEGY_LEXICAL)

    _select(monkeypatch, EMBEDDING_PROVIDER_NONE)
    assert registry.resolve_provider() is None
    assert _pairs(dedup.dedup()) == ()

    local = _local(monkeypatch)
    embedding = memory_tables.memory_embedding
    with dedup.reading() as uow:
        models = set(uow.connection.execute(select(embedding.c.model_id)).scalars())
    _gate(models == {fake.model_id}, f"expected only the fake's rows, found {models}")
    assert _pairs(dedup.dedup()) == ()

    _fill(dedup, local)
    assert [(pair.ref_a, pair.ref_b) for pair in _pairs(dedup.dedup())] == [
        _refs(first, second)
    ]


# --- edge case 10: zero or one qualifying pair, and no false pairing ------------------


def test_template_notes_pair_with_nothing_and_a_real_duplicate_beside_them_is_alone(
    dedup: DedupWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edge case 10. ``apples`` and ``pears`` are each other's nearest neighbour and
    sit under the floor, so they answer no pair at all. With the two-title pair added
    beside them, the answer is exactly that pair: the notes are not paired with it or
    with each other.

    Each step is gated by the non-index pass over **every** pair of the memories
    present, so the expected answer is the floor's verdict on the whole set rather than
    on the one pair the test happens to name.
    """
    provider = _local(monkeypatch)
    apples, pears = _seed(dedup, *_ONE_TEMPLATE)
    _fill(dedup, provider)
    clearing = _clearing(dedup, (apples, pears), model_id=provider.model_id)
    _gate(clearing == {}, f"a template pair clears the floor: {clearing}")

    assert _pairs(dedup.dedup()) == ()

    first, second = _seed(dedup, *_TWO_TITLES)
    _fill(dedup, provider)
    everything = (apples, pears, first, second)
    clearing = _clearing(dedup, everything, model_id=provider.model_id)
    _gate(
        set(clearing) == {frozenset((first, second))},
        f"expected only the two-title pair to clear the floor, got {clearing}",
    )

    pairs = _pairs(dedup.dedup())
    assert [(pair.ref_a, pair.ref_b) for pair in pairs] == [_refs(first, second)]
    assert _named(pairs).isdisjoint({memory_reference(apples), memory_reference(pears)})


# --- bounded: by score, then cut at the limit -----------------------------------------


def test_pairs_run_by_score_and_stop_at_the_limit(
    dedup: DedupWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two real duplicate pairs, 0.908 and 0.829: both by default, higher first, and
    the higher alone at ``limit=1``. A limit outside 1-50 is ``input_invalid``."""
    provider = _local(monkeypatch)
    near = sorted(_seed(dedup, *_TWO_TITLES))
    paraphrase = sorted(_seed(dedup, *_PARAPHRASE))
    _fill(dedup, provider)
    clearing = _clearing(dedup, (*near, *paraphrase), model_id=provider.model_id)
    _gate(
        set(clearing) == {frozenset(near), frozenset(paraphrase)},
        f"expected exactly the two duplicate pairs to clear the floor, got {clearing}",
    )
    _gate(
        clearing[frozenset(near)] > clearing[frozenset(paraphrase)],
        f"the two-title pair does not outscore the paraphrase: {clearing}",
    )

    both = _pairs(dedup.dedup())
    assert [(pair.ref_a, pair.ref_b) for pair in both] == [
        _refs(*near),
        _refs(*paraphrase),
    ]
    assert both[0].score > both[1].score

    (top,) = _pairs(dedup.dedup(limit=1))
    assert (top.ref_a, top.ref_b) == _refs(*near)

    for limit in (DEDUP_LIMIT_MIN - 1, DEDUP_LIMIT_MAX + 1):
        refused = dedup.dedup(limit=limit)
        assert refused.state == "input_invalid", refused
        assert refused.result is None, refused
