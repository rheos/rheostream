"""A pytest plugin for CI sharding (issue #252), loaded only by CI's shard jobs:
`PYTHONPATH=scripts pytest -p ci_pytest_shard`.

`RHEO_TEST_SHARD=i/N` keeps the tests of shard `i` (1-based) out of `N` and deselects
the rest, so CI can run the suite as N parallel jobs. Unset, it does nothing.

Whole files go to one shard, so a module-scoped fixture is built once, in one job.
Files are assigned greedily, heaviest first, to the lightest shard. A file's weight is
its test count, times :data:`POSTGRES_WEIGHT` for files under a `postgres/` directory,
since those tests each provision a workspace database and dominate the run time. Ties
break on the file path, so every job computes the same partition from the same
collection, and the shards together select every collected test exactly once
(`tests/test_shard_partition.py`).

It is a `-p` plugin rather than a conftest on purpose. A root `conftest.py` would
shadow the `from conftest import ...` the test suite relies on, and a plugin named
on the command line is not inherited by the suites' own `pytest` subprocesses
(`--collect-only` for the acceptance matrix, the fail-fast probe), which must see
the whole suite even inside a shard job.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable

import pytest

SHARD_ENV = "RHEO_TEST_SHARD"
POSTGRES_WEIGHT = 5


def _file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _weight(path: str, count: int) -> int:
    return count * (POSTGRES_WEIGHT if "/postgres/" in f"/{path}" else 1)


def partition(nodeids: Iterable[str], shards: int) -> list[set[str]]:
    """The files of each shard, heaviest file first onto the lightest shard."""
    counts = Counter(_file_of(n) for n in nodeids)
    files = sorted(counts, key=lambda f: (-_weight(f, counts[f]), f))
    loads = [0] * shards
    groups: list[set[str]] = [set() for _ in range(shards)]
    for path in files:
        target = min(range(shards), key=lambda i: (loads[i], i))
        groups[target].add(path)
        loads[target] += _weight(path, counts[path])
    return groups


def parse_shard(value: str) -> tuple[int, int]:
    """`i/N` as (i, N), 1 <= i <= N; anything else raises a usage error."""
    try:
        index_text, total_text = value.split("/")
        index, total = int(index_text), int(total_text)
    except ValueError:
        message = f"{SHARD_ENV} must look like 2/5, not {value!r}"
        raise pytest.UsageError(message) from None
    if not 1 <= index <= total:
        raise pytest.UsageError(f"{SHARD_ENV}={value!r}: need 1 <= i <= N")
    return index, total


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    value = os.environ.get(SHARD_ENV, "").strip()
    if not value:
        return
    index, total = parse_shard(value)
    mine = partition((item.nodeid for item in items), total)[index - 1]
    keep = [item for item in items if _file_of(item.nodeid) in mine]
    dropped = [item for item in items if _file_of(item.nodeid) not in mine]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = keep
