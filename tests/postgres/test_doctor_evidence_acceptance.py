"""``rheo doctor``'s evidence-acceptance check against a real workspace database
(#221).

A drain that contains an acceptance fault defers the unit and succeeds, so a unit's
fifth-attempt ``gap/acceptance_failed`` settlement is otherwise silent. The check
counts those settlements per active workspace over the last 24 hours.

Seam: ``_check_workspace_acceptance`` on a freshly provisioned workspace, with rows
written straight into ``core.evidence_unit``. Each test's workspace is its own, so the
counts are exact; the session's other workspaces are only reached through
``_check_evidence_acceptance``, which is asserted to include this one's line.

- An empty table is ``ok``; a workspace with no table is ``ok`` and not applicable.
- Any failure fails: one among other settlements, a lone lost unit, or many.
- A settlement older than the window, and a ``pending`` row, count for nothing.
- The line carries counts only, never evidence text or a native key.

Every text is synthetic.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import ClusterSession
from rheo_app_cli.commands.doctor import (
    ACCEPTANCE_WINDOW,
    _check_evidence_acceptance,
    _check_workspace_acceptance,
)
from rheo_core.storage.evidence_tables import (
    OUTCOME_ACCEPTANCE_FAILED,
    OUTCOME_ACTIVE,
    OUTCOME_EXTRACTION_FAILED,
    STATE_GAP,
    STATE_PENDING,
    STATE_SETTLED,
    evidence_unit,
)
from sqlalchemy import delete, inspect, text

pytestmark = pytest.mark.postgres

_MARKER = "doctor-acceptance-canary-text"


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture(autouse=True)
def clean_evidence_rows(
    cluster: ClusterSession, workspace: UUID
) -> Iterator[None]:
    """Remove rows from this test's private workspace before the next test runs."""
    yield
    database = cluster.registry_row(workspace).database_name
    engine = cluster.backend.pools.engine_for(database)
    if inspect(engine).has_table("evidence_unit", schema="core"):
        with engine.begin() as connection:
            connection.execute(delete(evidence_unit))


def _insert(
    cluster: ClusterSession,
    workspace: UUID,
    *,
    state: str,
    outcome: str | None,
    settled_at: datetime | None,
    count: int = 1,
) -> None:
    database = cluster.registry_row(workspace).database_name
    recorded = (settled_at or datetime.now(UTC)) - timedelta(minutes=10)
    rows = [
        {
            "id": uuid4(),
            "producer_kind": "rheo_runtime",
            "authority_id": uuid4(),
            "native_key": f"{_MARKER}-{uuid4()}",
            "speaker_account_id": uuid4(),
            "audience_kind": "workspace",
            "audience_id": None,
            "purpose": "respond",
            "source_recorded_at": recorded,
            "source_expires_at": recorded + timedelta(days=3),
            "body": f"{_MARKER} body" if state == STATE_PENDING else None,
            "extraction_attempts": 5 if state == STATE_GAP else 0,
            "retry_after": None,
            "state": state,
            "outcome": outcome,
            "created_at": recorded,
            "settled_at": settled_at,
        }
        for _ in range(count)
    ]
    with cluster.backend.pools.engine_for(database).begin() as connection:
        connection.execute(evidence_unit.insert(), rows)


def _check(cluster: ClusterSession, workspace: UUID, now: datetime) -> tuple[str, str]:
    check = _check_workspace_acceptance(
        cluster.backend, cluster.registry_row(workspace), now=now
    )
    assert check.name.startswith(f"evidence acceptance {workspace} ("), check.name
    return check.level, check.detail


def test_an_empty_evidence_table_is_ok(
    cluster: ClusterSession, workspace: UUID, now: datetime
) -> None:
    level, detail = _check(cluster, workspace, now)
    assert level == "ok", detail
    assert detail.startswith("0 of 0 evidence units settled in the last 24 h"), detail


def test_a_workspace_without_the_table_is_not_applicable(
    cluster: ClusterSession, workspace: UUID, now: datetime
) -> None:
    """A workspace not yet at revision 0010. The test workspace is this test's own and
    dropped at session teardown, so dropping its table touches nothing else."""
    database = cluster.registry_row(workspace).database_name
    with cluster.backend.pools.engine_for(database).begin() as connection:
        connection.execute(text("DROP TABLE core.evidence_unit"))
    level, detail = _check(cluster, workspace, now)
    assert level == "ok", detail
    assert "not applicable" in detail


def test_one_failure_among_other_settlements_fails(
    cluster: ClusterSession, workspace: UUID, now: datetime
) -> None:
    _insert(
        cluster,
        workspace,
        state=STATE_SETTLED,
        outcome=OUTCOME_ACTIVE,
        settled_at=now - timedelta(hours=1),
        count=2,
    )
    _insert(
        cluster,
        workspace,
        state=STATE_GAP,
        outcome=OUTCOME_ACCEPTANCE_FAILED,
        settled_at=now - timedelta(hours=2),
    )
    # Neither of these is an acceptance failure, and a pending row has not settled.
    _insert(
        cluster,
        workspace,
        state=STATE_GAP,
        outcome=OUTCOME_EXTRACTION_FAILED,
        settled_at=now - timedelta(hours=3),
    )
    _insert(cluster, workspace, state=STATE_PENDING, outcome=None, settled_at=None)
    level, detail = _check(cluster, workspace, now)
    assert level == "FAIL", detail
    assert detail.startswith("1 of 4 evidence units"), detail
    assert _MARKER not in detail


def test_a_lone_lost_unit_fails(
    cluster: ClusterSession, workspace: UUID, now: datetime
) -> None:
    """The quiet workspace: its one settled unit in the window was lost."""
    _insert(
        cluster,
        workspace,
        state=STATE_GAP,
        outcome=OUTCOME_ACCEPTANCE_FAILED,
        settled_at=now - timedelta(minutes=5),
    )
    level, detail = _check(cluster, workspace, now)
    assert level == "FAIL", detail
    assert detail.startswith("1 of 1 evidence units"), detail
    assert _MARKER not in detail


def test_many_failures_among_other_settlements_fail(
    cluster: ClusterSession, workspace: UUID, now: datetime
) -> None:
    _insert(
        cluster,
        workspace,
        state=STATE_SETTLED,
        outcome=OUTCOME_ACTIVE,
        settled_at=now - timedelta(hours=1),
        count=20,
    )
    _insert(
        cluster,
        workspace,
        state=STATE_GAP,
        outcome=OUTCOME_ACCEPTANCE_FAILED,
        settled_at=now - timedelta(hours=1),
        count=5,
    )
    level, detail = _check(cluster, workspace, now)
    assert level == "FAIL", detail
    assert detail.startswith("5 of 25 evidence units")


def test_a_failure_older_than_the_window_is_not_counted(
    cluster: ClusterSession, workspace: UUID, now: datetime
) -> None:
    _insert(
        cluster,
        workspace,
        state=STATE_GAP,
        outcome=OUTCOME_ACCEPTANCE_FAILED,
        settled_at=now - ACCEPTANCE_WINDOW - timedelta(minutes=1),
    )
    _insert(
        cluster,
        workspace,
        state=STATE_SETTLED,
        outcome=OUTCOME_ACTIVE,
        settled_at=now - timedelta(hours=1),
    )
    level, detail = _check(cluster, workspace, now)
    assert level == "ok", detail
    assert detail.startswith("0 of 1 evidence units"), detail


def test_the_sweep_over_active_workspaces_reports_this_one(
    cluster: ClusterSession, workspace: UUID, now: datetime
) -> None:
    _insert(
        cluster,
        workspace,
        state=STATE_GAP,
        outcome=OUTCOME_ACCEPTANCE_FAILED,
        settled_at=now - timedelta(minutes=5),
    )
    lines = [
        check.line() for check in _check_evidence_acceptance(cluster.backend, now=now)
    ]
    (mine,) = [line for line in lines if f"evidence acceptance {workspace} " in line]
    assert mine.startswith("FAIL "), mine
    assert all(_MARKER not in line for line in lines)
