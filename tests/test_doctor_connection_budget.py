"""``rheo doctor``'s connection-budget check, without a cluster (issue #162).

Seam: ``_check_connection_budget`` over a real ``PostgresBackend`` built from the
shipped defaults. Construction connects to nothing: ``create_engine`` is lazy, and
``max_connections`` (the one catalog read) is replaced per test with the ceiling
under test. ``tests/postgres/test_cli.py`` reads the same line off a live stock
cluster; this file pins the tier boundaries that a single stock cluster cannot.

Two halves:

* **The arithmetic matches the engines.** The figure the check prints is only worth
  reading if it describes the connections the backend can actually open, so the
  control engine's own pool is read back and compared with the reservation.
* **The tiers.** ``ok`` needs the two long-running processes (core and worker) to fit
  inside 80% of ``max_connections``, leaving the rest for a third process: an
  operator's ``rheo`` command, such as the phase-1b migration run.
"""

from typing import Any

import pytest
from rheo_app_cli.commands.doctor import (
    BUDGET_HEADROOM_PERCENT,
    CONFIGURED_PROCESSES,
    _check_connection_budget,
)
from rheo_core.settings.defaults import PACKAGE_DEFAULTS
from rheo_core.storage.postgres import (
    MAINTENANCE_RESERVED,
    PostgresBackend,
    cluster_url_from_dsn,
)
from sqlalchemy.exc import OperationalError

DSN = "postgresql://rheo:not-a-secret@db.example.invalid:5432/postgres"
"""Never dialled: nothing in this file opens a connection."""

STOCK_MAX_CONNECTIONS = 100


def backend(
    *,
    cache_size: int | None = None,
    pool_max_connections: int | None = None,
    ceiling: int = STOCK_MAX_CONNECTIONS,
) -> PostgresBackend:
    built = PostgresBackend(
        cluster_url_from_dsn(DSN),
        control_database="rheo_control",
        template_database="template1",
        pool_cache_size=(
            cache_size
            if cache_size is not None
            else int(PACKAGE_DEFAULTS["storage.pool_cache_size"])  # type: ignore[arg-type]
        ),
        pool_max_connections=(
            pool_max_connections
            if pool_max_connections is not None
            else int(PACKAGE_DEFAULTS["storage.pool_max_connections"])  # type: ignore[arg-type]
        ),
        pool_idle_close_seconds=300,
    )
    built.max_connections = lambda: ceiling  # type: ignore[method-assign]
    return built


def test_shipped_defaults_are_ok_on_a_stock_cluster() -> None:
    """The flagship runs the packaged defaults against a stock ``max_connections``."""
    check = _check_connection_budget(backend())
    assert check.level == "ok", check.line()
    assert "6 * 5 + 6 = 36 per process" in check.detail, check.detail
    assert "2 processes configured = 72" in check.detail, check.detail
    assert "cluster max_connections = 100, target <= 80" in check.detail, check.detail
    assert "effective" not in check.detail, check.detail


def test_the_reservation_is_the_control_engine_plus_one_maintenance_slot() -> None:
    """The printed figure describes the engines the backend really builds.

    ``control_engine`` is lazy and connects to nothing when built, so its pool can be
    read back here. If its size or overflow changes without the reservation following,
    the doctor would print a number the process can exceed.
    """
    built = backend()
    control_pool: Any = built.control_engine.pool
    assert control_pool.size() == built.pool_max_connections
    assert control_pool._max_overflow == 0
    assert (
        built.pools.reserved_connections == control_pool.size() + MAINTENANCE_RESERVED
    )
    assert built.pools.pooled_connections == (
        built.pools.cache_size * built.pools.pool_size
        + built.pools.reserved_connections
    )


def test_the_old_defaults_warn_on_a_stock_cluster() -> None:
    """16 * 5 + 6 = 86 per process, 172 for two: the reading issue #162 reported."""
    check = _check_connection_budget(backend(cache_size=16))
    assert check.level == "warn", check.line()
    assert "2 processes configured = 172" in check.detail, check.detail


@pytest.mark.parametrize(
    ("ceiling", "level"),
    [
        # 72 for the pair: target is 80% of the ceiling, rounded down.
        (90, "ok"),  # target 72: exactly at the line is inside it
        (89, "warn"),  # target 71
        (72, "warn"),  # the pair fits, but leaves nothing for an operator command
        (36, "warn"),  # one process fits exactly
        (35, "FAIL"),  # one process alone does not
    ],
)
def test_tier_boundaries(ceiling: int, level: str) -> None:
    check = _check_connection_budget(backend(ceiling=ceiling))
    assert check.level == level, check.line()


def test_held_connections_above_the_configured_figure_are_what_is_judged() -> None:
    """Busy engines are not evicted (issue #62), so the cache can outgrow its cap.

    Seven engines cached against a cap of six hold 7 * 5 + 6 = 41 per process, 82 for
    two: past the 80 target, so the level follows the held figure, not the
    configured 72.
    """
    built = backend()
    for index in range(built.pools.cache_size + 1):
        built.pools._engines[f"ws_{index}"] = object()  # type: ignore[assignment]
    assert built.pools.held_connections == 41
    check = _check_connection_budget(built)
    assert check.level == "warn", check.line()
    assert "effective 82" in check.detail, check.detail


def test_the_detail_names_every_lever() -> None:
    check = _check_connection_budget(backend())
    for lever in (
        "max_connections",
        "storage.pool_cache_size",
        "storage.pool_max_connections",
    ):
        assert lever in check.detail, (lever, check.detail)
    assert f"{BUDGET_HEADROOM_PERCENT}%" in check.detail
    assert CONFIGURED_PROCESSES == 2


def test_an_unreadable_ceiling_fails() -> None:
    built = backend()

    def refuse() -> int:
        raise OperationalError("SHOW max_connections", {}, Exception("unreachable"))

    built.max_connections = refuse  # type: ignore[method-assign]
    check = _check_connection_budget(built)
    assert check.level == "FAIL", check.line()
    assert "cannot read max_connections" in check.detail
