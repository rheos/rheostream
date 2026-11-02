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

Within a shard (#517) CI spreads the kept tests over xdist workers with `--dist
loadgroup`. Each file is one `xdist_group`, so its tests run on one worker in their own
order, except a file weighing at least :data:`SPLIT_WEIGHT`: its tests go out one by
one, since a single such file (`test_jobfeed_run.py`, 125-160 s) on one worker
outlasted the rest of the shard. Files run heaviest first, so the long ones start
early. Every test still builds its own fixtures; the suite's one module-scoped fixture
lives in a light file, which stays whole.

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
#: Measured on CI (#517, Nov 2026): a postgres test took 0.71 s on average and any
#: other test 0.039 s, about 18 to 1.
POSTGRES_WEIGHT = 18
#: A file at least this heavy (56 postgres tests) is split across a shard's workers.
SPLIT_WEIGHT = 1000


def _file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _weight(path: str, count: int) -> int:
    return count * (POSTGRES_WEIGHT if "/postgres/" in f"/{path}" else 1)


def heaviest_first(nodeids: Iterable[str]) -> list[str]:
    """The files of ``nodeids`` by descending weight, ties on the path."""
    counts = Counter(_file_of(n) for n in nodeids)
    return sorted(counts, key=lambda f: (-_weight(f, counts[f]), f))


def whole_files(nodeids: Iterable[str]) -> set[str]:
    """The files of ``nodeids`` light enough to run whole on one xdist worker."""
    counts = Counter(_file_of(n) for n in nodeids)
    return {f for f, n in counts.items() if _weight(f, n) < SPLIT_WEIGHT}


def partition(nodeids: Iterable[str], shards: int) -> list[set[str]]:
    """The files of each shard, heaviest file first onto the lightest shard."""
    nodeids = list(nodeids)
    counts = Counter(_file_of(n) for n in nodeids)
    files = heaviest_first(nodeids)
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
    rank = {f: i for i, f in enumerate(heaviest_first(item.nodeid for item in keep))}
    keep.sort(key=lambda item: rank[_file_of(item.nodeid)])
    whole = whole_files(item.nodeid for item in keep)
    for item in keep:
        path = _file_of(item.nodeid)
        if path in whole:
            item.add_marker(pytest.mark.xdist_group(name=path))
    dropped = [item for item in items if _file_of(item.nodeid) not in mine]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = keep
