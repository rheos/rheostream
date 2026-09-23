"""Hybrid fusion and rerank as arithmetic, with no database.

Every expected score is written out as ``1 / (60 + rank)`` by hand, never computed
from the module's constant, so a changed constant reds these tests instead of moving
the expectation with it. The sums are written in the order the arms are fused,
lexical first, so float equality is exact.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from rheo_recallatron.configuration import (
    OVERFETCH_MULTIPLIER_DEFAULT,
    OVERFETCH_MULTIPLIER_KEY,
)
from rheo_recallatron.retrieval.hybrid import fuse, overfetch_multiplier_in

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
