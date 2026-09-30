"""The CI shard partition in `scripts/ci_pytest_shard.py` (issue #252).

Pure checks on :func:`partition` and :func:`parse_shard`: no cluster and no
subprocess. What matters is that the shards cover every collected file exactly once,
that the answer does not depend on collection order, and that postgres files are
weighted so no shard gets all of them.
"""

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "ci_pytest_shard", ROOT / "scripts" / "ci_pytest_shard.py"
)
assert _spec and _spec.loader
shard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shard)

NODEIDS = [
    *(f"tests/postgres/test_big.py::test_{i}" for i in range(40)),
    *(f"tests/postgres/test_mid.py::test_{i}" for i in range(12)),
    *(f"tests/test_unit_a.py::test_{i}" for i in range(90)),
    *(f"tests/test_unit_b.py::test_{i}" for i in range(15)),
    "tests/test_unit_c.py::test_only",
    *(f"modules/example/tests/test_mod.py::test_{i}" for i in range(7)),
]


@pytest.mark.parametrize("shards", [1, 2, 3, 5, 8])
def test_every_file_lands_in_exactly_one_shard(shards: int) -> None:
    groups = shard.partition(NODEIDS, shards)
    files = {n.split("::")[0] for n in NODEIDS}
    assert len(groups) == shards
    assert set().union(*groups) == files
    assert sum(len(g) for g in groups) == len(files)


def test_the_partition_ignores_collection_order() -> None:
    shuffled = NODEIDS[:]
    random.Random(7).shuffle(shuffled)
    assert shard.partition(shuffled, 4) == shard.partition(NODEIDS, 4)


def test_postgres_files_are_spread_by_weight() -> None:
    """40 postgres tests weigh 200, more than 90 plain ones, so the two postgres files
    do not share a shard with each other when two shards are available."""
    first, second = shard.partition(NODEIDS, 2)
    assert "tests/postgres/test_big.py" in first
    assert "tests/postgres/test_mid.py" in second


@pytest.mark.parametrize(
    ("value", "expected"), [("1/1", (1, 1)), ("3/5", (3, 5)), (" 2/4 ".strip(), (2, 4))]
)
def test_parse_shard_accepts_i_of_n(value: str, expected: tuple[int, int]) -> None:
    assert shard.parse_shard(value) == expected


@pytest.mark.parametrize("value", ["0/3", "4/3", "2", "a/b", "1/2/3", "-1/2"])
def test_parse_shard_refuses_anything_else(value: str) -> None:
    with pytest.raises(pytest.UsageError):
        shard.parse_shard(value)
