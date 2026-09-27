"""#191: the harness no longer leaks a ``ws_*`` database per test.

Two halves. Each test's workspace databases are dropped when it finishes, and each
session starts by reaping stale ``ws_*`` databases that killed sessions left behind.
The reaper runs against a cluster other sessions share, so every call here passes
``only=`` with the names this test made. A shifted ``now`` without it would reap
other sessions' databases.
"""

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import (
    CONTROL_TEST_PREFIX,
    DEFAULT_REAP_AFTER_HOURS,
    REAP_AFTER_ENV,
    ClusterSession,
    MakeWorkspace,
    ReapReport,
    reap_after_from_env,
    reap_stale_workspace_databases,
)
from rheo_core.refs import uuid7
from rheo_core.storage.provisioning import database_name_for
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.postgres

SIX_HOURS = timedelta(hours=DEFAULT_REAP_AFTER_HOURS)


def _ws_name_minted_at(at: datetime) -> str:
    """A ``ws_<uuid7 hex>`` name whose UUIDv7 time field says ``at``."""
    low_bits = uuid7().int & ((1 << 80) - 1)
    minted = int(at.timestamp() * 1000) << 80
    return f"ws_{UUID(int=minted | low_bits).hex}"


def _create(cluster: ClusterSession, name: str) -> str:
    """Create a bare database, recorded first so teardown drops it if the reaper
    doesn't."""
    cluster.record(name)
    with cluster.maintenance.connect() as connection:
        quoted = connection.dialect.identifier_preparer.quote(name)
        connection.execute(text(f"CREATE DATABASE {quoted}"))
    return name


def _exists(cluster: ClusterSession, name: str) -> bool:
    with cluster.maintenance.connect() as connection:
        return (
            connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).first()
            is not None
        )


def _reap(cluster: ClusterSession, names: set[str], **kwargs) -> ReapReport:
    return reap_stale_workspace_databases(
        cluster.maintenance, older_than=SIX_HOURS, only=names, **kwargs
    )


@pytest.fixture
def hold_open(cluster: ClusterSession) -> Iterator:
    """Open a connection to a named database for the rest of the test."""
    opened: list[tuple] = []

    def _open(name: str) -> Connection:
        engine = create_engine(
            cluster.maintenance.url.set(database=name), poolclass=NullPool
        )
        connection = engine.connect()
        opened.append((engine, connection))
        return connection

    yield _open
    for engine, connection in opened:
        connection.close()
        engine.dispose()


# --- the reaper -----------------------------------------------------------------------


def test_a_stale_unconnected_database_is_reaped(cluster: ClusterSession) -> None:
    name = _create(cluster, _ws_name_minted_at(datetime.now(UTC) - timedelta(hours=7)))
    report = _reap(cluster, {name})
    assert report.reaped == [name]
    assert not _exists(cluster, name)


def test_a_fresh_database_is_kept(cluster: ClusterSession) -> None:
    name = _create(cluster, _ws_name_minted_at(datetime.now(UTC) - timedelta(hours=5)))
    report = _reap(cluster, {name})
    assert report.fresh == [name]
    assert report.reaped == []
    assert _exists(cluster, name)


def test_a_stale_connected_database_is_kept(cluster: ClusterSession, hold_open) -> None:
    name = _create(cluster, _ws_name_minted_at(datetime.now(UTC) - timedelta(days=2)))
    hold_open(name).execute(text("SELECT 1"))
    report = _reap(cluster, {name})
    assert report.connected == [name]
    assert report.reaped == []
    assert _exists(cluster, name)


def test_names_outside_the_ws_pattern_are_never_touched(
    cluster: ClusterSession,
) -> None:
    """Offered explicitly, a month past the threshold, unconnected: still kept. The
    one real stale ``ws_`` name in the same pass proves the pass ran."""
    untouchable = {
        _create(cluster, f"{CONTROL_TEST_PREFIX}{uuid7().hex[:12]}"),
        _create(cluster, f"ws_scratch_{uuid7().hex[:12]}"),
    }
    stale = _create(cluster, database_name_for(uuid7()))
    report = _reap(
        cluster, untouchable | {stale}, now=datetime.now(UTC) + timedelta(days=30)
    )
    assert report.reaped == [stale]
    for name in untouchable:
        assert _exists(cluster, name), name
        assert name not in report.fresh + report.connected + report.unknown_age


def test_a_non_v7_name_is_aged_from_disk_or_kept(cluster: ClusterSession) -> None:
    """A ``ws_<uuid4 hex>`` name carries no time. Its age comes from ``PG_VERSION``'s
    mtime when the role may read it; otherwise it is kept as unknown."""
    with cluster.maintenance.connect() as connection:
        may_stat = connection.execute(
            text(
                "SELECT rolsuper OR pg_has_role(current_user, 'pg_read_server_files', "
                "'member') FROM pg_roles WHERE rolname = current_user"
            )
        ).scalar_one()
    name = _create(cluster, database_name_for(uuid4()))
    assert _reap(cluster, {name}).fresh == ([name] if may_stat else [])
    later = _reap(cluster, {name}, now=datetime.now(UTC) + timedelta(days=1))
    if may_stat:
        assert later.reaped == [name]
    else:
        assert later.unknown_age == [name]
        assert _exists(cluster, name)


def test_concurrent_reapers_drop_a_database_once_and_never_raise(
    cluster: ClusterSession,
) -> None:
    name = _create(cluster, _ws_name_minted_at(datetime.now(UTC) - timedelta(days=1)))
    start = threading.Barrier(6)
    reports: list[ReapReport] = []
    errors: list[BaseException] = []

    def reaper() -> None:
        start.wait()
        try:
            reports.append(_reap(cluster, {name}))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=reaper) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert sum(len(report.reaped) for report in reports) == 1
    for report in reports:
        assert set(report.reaped + report.gone) <= {name}
        assert report.connected == report.fresh == report.unknown_age == []
    assert not _exists(cluster, name)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, SIX_HOURS),
        ("", SIX_HOURS),
        ("12", timedelta(hours=12)),
        ("0.5", timedelta(minutes=30)),
        ("0", None),
        ("off", None),
        ("OFF", None),
    ],
)
def test_the_threshold_reads_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: timedelta | None
) -> None:
    if raw is None:
        monkeypatch.delenv(REAP_AFTER_ENV, raising=False)
    else:
        monkeypatch.setenv(REAP_AFTER_ENV, raw)
    assert reap_after_from_env() == expected


@pytest.mark.parametrize("raw", ["-1", "six", "nan"])
def test_a_bad_threshold_refuses_to_run(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(REAP_AFTER_ENV, raw)
    with pytest.raises(RuntimeError, match=REAP_AFTER_ENV):
        reap_after_from_env()


def test_the_session_ran_the_reaper(cluster: ClusterSession) -> None:
    """Unless the environment turned it off, the session fixture reaped at start."""
    if reap_after_from_env() is None:
        assert cluster.reap_report is None
    else:
        assert cluster.reap_report is not None
        assert cluster.reap_report.older_than == reap_after_from_env()


# --- the per-test drop ----------------------------------------------------------------

#: Written by the first test of the pair, read by the second. They run in file order.
_left_by_previous_test: list[UUID] = []


def test_per_test_drop_1_a_test_provisions_a_workspace(
    cluster: ClusterSession, make_workspace: MakeWorkspace
) -> None:
    workspace_id = make_workspace()
    name = database_name_for(workspace_id)
    assert _exists(cluster, name)
    assert cluster.registry_row_or_none(workspace_id) is not None
    assert cluster.test_databases == [name]
    _left_by_previous_test.append(workspace_id)


def test_per_test_drop_2_that_database_is_gone_once_the_test_finished(
    cluster: ClusterSession,
) -> None:
    assert _left_by_previous_test, (
        "run together with test_per_test_drop_1_a_test_provisions_a_workspace"
    )
    for workspace_id in _left_by_previous_test:
        name = database_name_for(workspace_id)
        assert not _exists(cluster, name), name
        # The registry row went first, so nothing that walks the registry meets it.
        assert cluster.registry_row_or_none(workspace_id) is None
        # Still tracked for the session teardown backstop, which uses IF EXISTS.
        assert name in cluster.created_databases
    assert cluster.test_databases == []
