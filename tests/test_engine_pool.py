"""The engine pool's two bounds: idle close, and the count cap behind it.

Seam: ``EnginePool``. No cluster is needed — SQLAlchemy's ``create_engine`` is lazy,
so these build engines and never connect. The point of each test is the property the
bound exists for, not the mechanics of ``OrderedDict``.

Issue #12: the count cap alone is adversarial to a caller that walks every workspace
in turn, because under a pure LRU the least-recently-used engine is always the one the
walk is about to reach next. Idle close is the bound that normally binds, so the walk
reclaims what nobody wanted instead of evicting what it is about to need.
"""

import time

import pytest
from rheo_core.storage.pools import EnginePool, check_database_name
from sqlalchemy.engine import make_url

CLUSTER = make_url("postgresql+psycopg://rheo:secret@localhost:5432/postgres")


def pool(*, cache_size: int = 4, idle_close_seconds: float = 300.0) -> EnginePool:
    return EnginePool(
        CLUSTER,
        cache_size=cache_size,
        pool_size=5,
        idle_close_seconds=idle_close_seconds,
    )


def test_engine_is_cached_and_reused() -> None:
    p = pool()
    first = p.engine_for("ws_a")
    assert p.engine_for("ws_a") is first
    assert p.cached() == ("ws_a",)


def test_count_cap_is_the_hard_ceiling() -> None:
    """Past the cap the least-recently-used engine goes, so connections stay bounded."""
    p = pool(cache_size=2)
    p.engine_for("ws_a")
    p.engine_for("ws_b")
    p.engine_for("ws_c")
    assert p.cached() == ("ws_b", "ws_c")


def test_idle_close_reclaims_before_the_cap_binds() -> None:
    """The property issue #12 is about.

    A caller walks more workspaces than the cap holds. With the idle window already
    past, each engine is reclaimed because nobody wanted it, and the walk never has to
    evict a live one: after the walk the cache holds only what the walk itself created,
    and no engine was evicted by the cap.
    """
    p = pool(cache_size=2, idle_close_seconds=0.01)
    p.engine_for("ws_a")
    p.engine_for("ws_b")
    time.sleep(0.05)
    # Both are now idle. The next call expires them before it considers the cap.
    p.engine_for("ws_c")
    assert p.cached() == ("ws_c",)


def test_a_hot_engine_survives_the_idle_sweep() -> None:
    """Idle is measured from last use, so a repeatedly-used engine never expires."""
    p = pool(cache_size=4, idle_close_seconds=0.05)
    p.engine_for("ws_hot")
    for _ in range(4):
        time.sleep(0.02)
        assert p.engine_for("ws_hot") is not None
    assert "ws_hot" in p.cached()


def test_close_idle_is_callable_without_asking_for_an_engine() -> None:
    p = pool(idle_close_seconds=0.01)
    p.engine_for("ws_a")
    p.engine_for("ws_b")
    time.sleep(0.05)
    assert p.close_idle() == 2
    assert p.cached() == ()
    assert p.close_idle() == 0


def test_worst_case_connections_is_reportable() -> None:
    """The arithmetic an operator compares with the cluster's own ``max_connections``.

    The shipped default pair is 16 * 5 = 80 per process, and core and worker are
    separate processes each holding their own.
    """
    assert pool(cache_size=16).worst_case_connections == 80


def test_dispose_all_clears_the_idle_bookkeeping_too() -> None:
    p = pool()
    p.engine_for("ws_a")
    p.dispose_all()
    assert p.cached() == ()
    # A stale last-used entry would make the next engine look instantly expired.
    assert p.engine_for("ws_a") is not None
    assert p.cached() == ("ws_a",)


@pytest.mark.parametrize(
    ("cache_size", "pool_size", "idle"),
    [(0, 5, 300.0), (4, 0, 300.0), (4, 5, 0.0), (4, 5, -1.0)],
)
def test_non_positive_bounds_are_refused(
    cache_size: int, pool_size: int, idle: float
) -> None:
    with pytest.raises(ValueError):
        EnginePool(
            CLUSTER, cache_size=cache_size, pool_size=pool_size, idle_close_seconds=idle
        )


def test_database_name_check_still_guards_interpolation() -> None:
    assert check_database_name("ws_018f") == "ws_018f"
    for bad in ("WS_A", "ws-a", "", "a" * 64, 'ws"; DROP DATABASE x'):
        with pytest.raises(ValueError):
            check_database_name(bad)
