"""``hnsw.ef_search`` for a dense arm, at both ends of its clamp.

The setting's range is ``1..1000``. A ``dense``-only arm asks for 500 rows and four
times that is 2000, which Postgres refuses outright in a backend that has pgvector
loaded, so the ceiling is a hard clamp and not a tuning value. The floor keeps a
small arm's graph search from starting too narrow. Expected values are written out,
not derived from the constants the function reads.
"""

import pytest
from rheo_recallatron.retrieval.dense import hnsw_ef_search


@pytest.mark.parametrize(
    ("limit", "ef_search"),
    [
        (1, 64),
        (16, 64),
        (30, 120),
        (100, 400),
        (250, 1000),
        (500, 1000),
    ],
)
def test_ef_search_scales_with_the_arm_inside_the_setting_s_range(
    limit: int, ef_search: int
) -> None:
    assert hnsw_ef_search(limit) == ef_search
