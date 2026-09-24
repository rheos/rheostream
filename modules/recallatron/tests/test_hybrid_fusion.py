"""Hybrid fusion and rerank as arithmetic, with no database.

Every expected score is written out as ``1 / (60 + rank)`` by hand, never computed
from the module's constant, so a changed constant reds these tests instead of moving
the expectation with it. The sums are written in the order the arms are fused,
lexical first, so float equality is exact.

``HybridStrategy.search`` itself runs here over stub arms, with its two database
reads (the multiplier and ``recorded_at``) stubbed, to pin the cut it hands on.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from rheo_contracts import WorkspaceContext
from rheo_core.refs.resolver import UnitOfWork
from rheo_recallatron.configuration import (
    CANDIDATE_SCAN_LIMIT,
    OVERFETCH_MULTIPLIER_DEFAULT,
    OVERFETCH_MULTIPLIER_KEY,
)
from rheo_recallatron.eligibility import MemoryRequest, ReadMode
from rheo_recallatron.retrieval import hybrid
from rheo_recallatron.retrieval.hybrid import (
    HybridStrategy,
    fuse,
    overfetch_multiplier_in,
)
from rheo_recallatron.retrieval.protocol import (
    ArmProvenance,
    Hit,
    IndexItem,
    SearchRequest,
    SearchResult,
)

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _id(number: int) -> UUID:
    return UUID(int=number)


def test_fused_scores_are_the_reciprocal_rank_sums() -> None:
    """Two hand-built arms. ``b`` is in both, so it outranks ``a``, which lexical
    ranked first and dense never returned."""
    a, b, c, d = _id(1), _id(2), _id(3), _id(4)
    lexical = [a, b, c]
    dense = [b, d]
    recorded_at = {ref: _NOW for ref in (a, b, c, d)}

    fused = fuse((lexical, dense), recorded_at)

    assert dict(fused) == {
        a: 1 / 61,
        b: 1 / 62 + 1 / 61,
        c: 1 / 63,
        d: 1 / 62,
    }
    assert [ref for ref, _ in fused] == [b, a, d, c]


def test_one_arm_keeps_its_own_order() -> None:
    """The degraded hybrid: an empty dense arm leaves the lexical order exactly, even
    when the lexical tail is newer than its head."""
    x, y, z = _id(9), _id(8), _id(7)
    recorded_at = {x: _NOW, y: _NOW + timedelta(days=1), z: _NOW + timedelta(days=2)}

    fused = fuse(([x, y, z], []), recorded_at)

    assert fused == [(x, 1 / 61), (y, 1 / 62), (z, 1 / 63)]


def test_equal_fused_scores_order_newer_first_then_by_id() -> None:
    """``p`` and ``r`` both score ``1/61``: the newer, ``r``, goes first although ``p``
    has the lower id and came from the first arm. ``q`` and ``s`` both score ``1/62``
    and were recorded together: the lower id, ``s``, goes first although ``q`` came
    from the first arm."""
    p, q, r, s = _id(10), _id(40), _id(20), _id(30)
    recorded_at = {
        p: _NOW,
        r: _NOW + timedelta(seconds=1),
        q: _NOW,
        s: _NOW,
    }

    fused = fuse(([p, q], [r, s]), recorded_at)

    assert [ref for ref, _ in fused] == [r, p, s, q]
    assert dict(fused) == {p: 1 / 61, r: 1 / 61, q: 1 / 62, s: 1 / 62}


def test_a_ranked_id_whose_row_was_not_read_sorts_oldest_within_its_tie() -> None:
    """A row deleted between the arms and the ``recorded_at`` read keeps its rank
    and score, sorts behind its equals, and is left for the walk to drop."""
    gone, kept = _id(1), _id(2)

    fused = fuse(([gone], [kept]), {kept: _NOW})

    assert fused == [(kept, 1 / 61), (gone, 1 / 61)]


@dataclass
class _StubArm:
    """An arm that answers the same ranked ids whatever it is asked, and keeps every
    request it was asked."""

    refs: tuple[UUID, ...]
    name: str = "stub"
    requests: list[SearchRequest] = field(default_factory=list)

    def index(self, ctx: WorkspaceContext, uow: UnitOfWork, item: IndexItem) -> None:
        raise AssertionError("search must not index")

    def invalidate(self, ctx: WorkspaceContext, uow: UnitOfWork, ref: str) -> None:
        raise AssertionError("search must not invalidate")

    def search(
        self, ctx: WorkspaceContext, uow: UnitOfWork, request: SearchRequest
    ) -> SearchResult:
        self.requests.append(request)
        return SearchResult(
            hits=tuple(
                Hit(ref=ref, score=1.0, strategy=self.name) for ref in self.refs
            ),
            arms=ArmProvenance(lexical=0, dense=0),
            dense_available=True,
        )


def test_the_fused_list_handed_on_is_cut_to_the_scan_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub arms answer 500 lexical and 30 dense ids, 530 in all: more than the scan
    bound ``recall()`` asks for. ``search`` hands on exactly the bound, and what it
    drops is lexical's deepest 30, since every dense rank outscores them."""
    monkeypatch.setattr(
        hybrid, "overfetch_multiplier", lambda ctx, uow: OVERFETCH_MULTIPLIER_DEFAULT
    )
    monkeypatch.setattr(hybrid, "_recorded_at", lambda uow, ids: {})
    k = 10
    lexical = _StubArm(tuple(_id(n) for n in range(1, CANDIDATE_SCAN_LIMIT + 1)))
    dense = _StubArm(
        tuple(_id(10_000 + n) for n in range(1, k * OVERFETCH_MULTIPLIER_DEFAULT + 1))
    )
    request = SearchRequest(
        query="apples",
        mode=ReadMode.CURRENT,
        memory=cast(MemoryRequest, None),
        limit=CANDIDATE_SCAN_LIMIT,
        k=k,
    )

    result = HybridStrategy(lexical=lexical, dense=dense).search(
        cast(WorkspaceContext, None), cast(UnitOfWork, None), request
    )

    assert len(lexical.refs) + len(dense.refs) > CANDIDATE_SCAN_LIMIT
    assert len(result.hits) == CANDIDATE_SCAN_LIMIT
    assert result.arms == ArmProvenance(
        lexical=len(lexical.refs), dense=len(dense.refs)
    )
    dropped = set(lexical.refs + dense.refs) - {hit.ref for hit in result.hits}
    assert dropped == set(lexical.refs[-len(dense.refs) :])
    assert [asked.limit for asked in lexical.requests] == [CANDIDATE_SCAN_LIMIT]
    assert [asked.limit for asked in dense.requests] == [
        k * OVERFETCH_MULTIPLIER_DEFAULT
    ]


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, OVERFETCH_MULTIPLIER_DEFAULT),
        ("1", 1),
        ("7", 7),
        ("10", 10),
        ("0", OVERFETCH_MULTIPLIER_DEFAULT),
        ("11", OVERFETCH_MULTIPLIER_DEFAULT),
        ("2.5", OVERFETCH_MULTIPLIER_DEFAULT),
        ("not-a-number", OVERFETCH_MULTIPLIER_DEFAULT),
    ],
)
def test_the_multiplier_read_degrades_to_the_default(
    stored: str | None, expected: int
) -> None:
    """Absent, undecodable or out of range: the default, never a raise."""
    rows = {} if stored is None else {OVERFETCH_MULTIPLIER_KEY: stored}
    assert overfetch_multiplier_in(rows) == expected
