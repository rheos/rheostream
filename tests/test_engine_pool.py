"""The engine pool's two bounds: idle close, and the count cap behind it.

Seam: ``EnginePool``. Almost none of this needs a cluster — SQLAlchemy's
``create_engine`` is lazy, so these build engines and never connect. The one exception
is ``test_an_engine_with_a_connection_checked_out_survives_the_idle_sweep``, which has
to hold a **real** connection open to have anything to skip, and says so in its own
docstring. The point of each test is the property the bound exists for, not the
mechanics of ``OrderedDict``.

Issue #12: the count cap alone is adversarial to a caller that walks every workspace
in turn, because under a pure LRU the least-recently-used engine is always the one the
walk is about to reach next. Idle close is the bound that normally binds, so the walk
reclaims what nobody wanted instead of evicting what it is about to need.

Two of the assertions here are about a *shape* rather than a value, and both exist
because the obvious version of the test passes either way:

* ``test_a_hot_engine_survives_the_idle_sweep`` holds engine **identity**. An
  ``is not None`` assertion is satisfied forever by a pool that silently rebuilds the
  engine on every call, which is the exact failure the idle bound would be hiding.
* ``test_dispose_is_never_called_under_the_lock`` reads ``pools.py`` itself, because
  disposal under the lock is a latency defect, not a wrong answer: every functional
  assertion in this file passes with the call in the wrong place.
"""

import ast
import threading
import time
from pathlib import Path

import pytest
from conftest import ClusterSession
from rheo_core.storage import pools as pools_module
from rheo_core.storage.pools import EnginePool, check_database_name
from sqlalchemy import text
from sqlalchemy.engine import make_url

CLUSTER = make_url("postgresql+psycopg://rheo:secret@localhost:5432/postgres")
MAINTENANCE_DATABASE = "postgres"
"""A second real database for the in-use test's other sweep entry point."""


def pool(
    *,
    cache_size: int = 4,
    idle_close_seconds: float = 300.0,
    reserved_connections: int = 5,
) -> EnginePool:
    return EnginePool(
        CLUSTER,
        cache_size=cache_size,
        pool_size=5,
        idle_close_seconds=idle_close_seconds,
        reserved_connections=reserved_connections,
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
    """Idle is measured from last use, so a repeatedly-used engine never expires.

    ``is first`` and not ``is not None``: the assertion has to fail for a pool that
    answers every call with a brand-new engine, which is what "never expires" would
    mean if identity were not held. The loop runs longer in total than the idle window
    while every individual gap stays well inside it, so a pool that measured idleness
    from creation rather than from last use fails here too.
    """
    p = pool(cache_size=4, idle_close_seconds=0.3)
    first = p.engine_for("ws_hot")
    for _ in range(8):
        time.sleep(0.05)
        assert p.engine_for("ws_hot") is first
    assert "ws_hot" in p.cached()
    assert p.engine_for("ws_hot") is first


def test_an_untouched_engine_is_gone_after_the_idle_window() -> None:
    """The other half of the pair: idleness is what actually closes an engine.

    Nothing evicts ``ws_cold`` here — the cap is nowhere near — so its absence can
    only be the idle sweep, which any call runs on the way in.
    """
    p = pool(cache_size=4, idle_close_seconds=0.01)
    p.engine_for("ws_cold")
    assert p.cached() == ("ws_cold",)
    time.sleep(0.05)
    p.engine_for("ws_other")
    assert "ws_cold" not in p.cached()
    assert p.cached() == ("ws_other",)


@pytest.mark.postgres
def test_an_engine_with_a_connection_checked_out_survives_the_idle_sweep(
    cluster: ClusterSession,
) -> None:
    """Issue #62's first gap: eviction does not close an in-flight connection.

    ``_last_used`` is stamped at hand-out and never on return, so a caller holding a
    connection for longer than ``idle_close_seconds`` has its engine selected by the
    sweep while it is still working. ``dispose()`` then **detaches** that connection
    rather than closing it — the socket lives on outside every count this class
    reports — so the fix is to skip expiry while the engine is in use.

    **This test holds a real connection open, and it has to.** A test that checks one
    out and returns it immediately leaves ``checkedout()`` at zero, so it passes
    against the guard and against its absence alike and proves nothing; the whole
    property is about the window during which the connection has *not* come back. That
    is also why this is the one case in this file that needs the cluster.

    Both sweep entry points are driven, because they are two call sites of
    ``_expire_idle`` and a guard added to only one would still leave the other
    disposing a live engine.
    """
    held = cluster.control_database
    p = EnginePool(
        cluster.backend.pools.cluster_url,
        cache_size=4,
        pool_size=5,
        idle_close_seconds=0.01,
        reserved_connections=0,
    )
    try:
        engine = p.engine_for(held)
        connection = engine.connect()
        try:
            assert engine.pool.checkedout() == 1, "the connection is not actually held"
            time.sleep(0.05)
            assert p.close_idle() == 0, "close_idle disposed an engine still in use"
            assert held in p.cached()
            # The other entry point: ``engine_for`` sweeps on the way in.
            p.engine_for(MAINTENANCE_DATABASE)
            assert held in p.cached(), "engine_for's sweep disposed an engine in use"
        finally:
            connection.close()
        # Returned, so the ordinary bound applies again. The sleep is for the second
        # engine, created a moment ago and not yet idle; the held one has been past
        # the window throughout and was skipped only because it was in use.
        time.sleep(0.05)
        assert p.close_idle() == 2
        assert p.cached() == ()
    finally:
        p.dispose_all()


@pytest.mark.postgres
def test_the_count_cap_defers_eviction_of_an_in_use_engine(
    cluster: ClusterSession,
) -> None:
    """Issue #62: the count cap must not ``dispose()``-detach a live connection.

    ``idle_close_seconds`` is far away, so nothing here can be expired by idleness and
    the eviction can only be the cap.
    """
    held = cluster.control_database
    p = EnginePool(
        cluster.backend.pools.cluster_url,
        cache_size=1,
        pool_size=5,
        idle_close_seconds=300.0,
        reserved_connections=0,
    )
    try:
        engine = p.engine_for(held)
        connection = engine.connect()
        try:
            assert engine.pool.checkedout() == 1, "the connection is not actually held"
            # The idle sweep protects it — the guard the case above pins.
            assert p.close_idle() == 0
            assert p.cached() == (held,)

            other = p.engine_for(MAINTENANCE_DATABASE)
            assert other is not engine
            assert held in p.cached()
            assert MAINTENANCE_DATABASE in p.cached()
            assert p.held_connections == 10
            assert p.pooled_connections == 5
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
        finally:
            connection.close()
        p.engine_for(MAINTENANCE_DATABASE)
        assert p.cached() == (MAINTENANCE_DATABASE,)
        assert p.held_connections == 5
    finally:
        p.dispose_all()


@pytest.mark.postgres
def test_a_pin_keeps_an_engine_through_idle_and_the_count_cap(
    cluster: ClusterSession,
) -> None:
    """The handout window: zero checkouts, but a caller still holds the engine."""
    held = cluster.control_database
    p = EnginePool(
        cluster.backend.pools.cluster_url,
        cache_size=1,
        pool_size=5,
        idle_close_seconds=0.01,
        reserved_connections=0,
    )
    try:
        with p.acquire(held) as engine:
            time.sleep(0.05)
            assert p.close_idle() == 0
            assert held in p.cached()
            other = p.engine_for(MAINTENANCE_DATABASE)
            assert other is not engine
            assert held in p.cached()
            assert MAINTENANCE_DATABASE in p.cached()
        time.sleep(0.05)
        assert p.close_idle() == 2
        assert p.cached() == ()
    finally:
        p.dispose_all()


def test_close_idle_is_callable_without_asking_for_an_engine() -> None:
    p = pool(idle_close_seconds=0.01)
    p.engine_for("ws_a")
    p.engine_for("ws_b")
    time.sleep(0.05)
    assert p.close_idle() == 2
    assert p.cached() == ()
    assert p.close_idle() == 0


def test_pooled_connections_counts_the_reserved_engine_too() -> None:
    """The arithmetic an operator compares with the cluster's own ``max_connections``.

    The shipped defaults are ``cache_size`` 16 and ``pool_max_connections`` 5. The
    backend reserves 5 for its control engine plus 1 for serialized maintenance:
    16 * 5 + 6 = 86 per process. Core and worker are separate processes each holding
    their own, so a stock ``max_connections`` of 100 carries one and not two.
    """
    p = pool(cache_size=16, reserved_connections=6)
    assert p.cache_size == 16
    assert p.pool_size == 5
    assert p.reserved_connections == 6
    assert p.pooled_connections == 86
    assert p.held_connections == 6
    # The reservation is additive, not decorative: drop it and the figure moves.
    assert pool(cache_size=16, reserved_connections=0).pooled_connections == 80
    # The old name claimed a ceiling the figure was not, and must not come back as an
    # alias beside the new one — two names for one number is how the wrong one
    # survives a rename.
    assert not hasattr(p, "worst_case_connections")


def test_dispose_all_clears_the_idle_bookkeeping_too() -> None:
    p = pool()
    p.engine_for("ws_a")
    p.dispose_all()
    assert p.cached() == ()
    # A stale last-used entry would make the next engine look instantly expired.
    assert p.engine_for("ws_a") is not None
    assert p.cached() == ("ws_a",)


@pytest.mark.parametrize(
    ("cache_size", "pool_size", "idle", "reserved"),
    [
        (0, 5, 300.0, 5),
        (4, 0, 300.0, 5),
        (4, 5, 0.0, 5),
        (4, 5, -1.0, 5),
        (4, 5, 300.0, -1),
    ],
)
def test_non_positive_bounds_are_refused(
    cache_size: int, pool_size: int, idle: float, reserved: int
) -> None:
    with pytest.raises(ValueError):
        EnginePool(
            CLUSTER,
            cache_size=cache_size,
            pool_size=pool_size,
            idle_close_seconds=idle,
            reserved_connections=reserved,
        )


def test_database_name_check_still_guards_interpolation() -> None:
    assert check_database_name("ws_018f") == "ws_018f"
    for bad in ("WS_A", "ws-a", "", "a" * 64, 'ws"; DROP DATABASE x'):
        with pytest.raises(ValueError):
            check_database_name(bad)


# --- the disposal-under-the-lock shape ------------------------------------------------


def _pools_source_tree() -> ast.Module:
    source_path = pools_module.__file__
    assert source_path is not None, "rheo_core.storage.pools has no source file"
    return ast.parse(Path(source_path).read_text(encoding="utf-8"))


def _is_self_lock(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "_lock"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _is_dispose_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "dispose"
    return isinstance(func, ast.Name) and func.id == "dispose"


def test_dispose_is_never_called_under_the_lock() -> None:
    """``dispose()`` closes sockets and can block; the lock serialises every caller.

    Read from the source rather than asserted behaviourally, because a disposal in the
    wrong place returns the right engine and passes every other test in this file. The
    defect it guards is the one PR #22 shipped: the cache-hit branch disposed its
    discarded engines inside ``with self._lock`` while the cache-miss branch correctly
    deferred them to a loop outside it.

    The positive control matters more than usual here: on a renamed attribute or a
    restructured file, "no dispose call inside a lock body" is also what a walk that
    found neither would report.
    """
    tree = _pools_source_tree()
    lock_bodies = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and any(_is_self_lock(item.context_expr) for item in node.items)
    ]
    disposals_in_file = [node for node in ast.walk(tree) if _is_dispose_call(node)]

    # Positive control: the walk actually found both halves of what it is comparing.
    assert lock_bodies, "found no `with self._lock` block — the walk missed the file"
    assert disposals_in_file, "found no dispose() call — the walk missed the file"

    under_lock = [
        node
        for body in lock_bodies
        for statement in body.body
        for node in ast.walk(statement)
        if _is_dispose_call(node)
    ]
    assert under_lock == [], (
        "dispose() is called while self._lock is held, at line(s) "
        f"{sorted(node.lineno for node in under_lock)}"
    )


@pytest.mark.postgres
def test_maintenance_access_is_one_at_a_time(cluster: ClusterSession) -> None:
    """Issue #62: overlapping maintenance calls used to each open a connection."""
    from rheo_core.storage.postgres import MAINTENANCE_RESERVED

    assert cluster.backend.pools.reserved_connections == (
        cluster.backend.pool_max_connections + MAINTENANCE_RESERVED
    )
    entered = threading.Event()
    release = threading.Event()
    overlapping = []

    def hold() -> None:
        with cluster.backend.maintenance_connection():
            entered.set()
            release.wait(timeout=2)

    first = threading.Thread(target=hold)
    first.start()
    assert entered.wait(timeout=2)
    second = threading.Thread(
        target=lambda: overlapping.append(
            cluster.backend._maintenance_lock.acquire(blocking=False)
        )
    )
    second.start()
    second.join(timeout=2)
    assert overlapping == [False]
    release.set()
    first.join(timeout=2)
