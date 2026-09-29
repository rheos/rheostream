"""``rheo doctor``'s ``bridge enrollments`` check against a real workspace database
(spec § Architecture → System Components, item 5, "Expiry signal").

Seam: ``_check_workspace_enrollments`` on this test's own provisioned workspace, with
enrollments created through ``core.evidence_enrollment.create`` and their tokens'
``expires_at``/``revoked_at`` then written straight into ``control.access_token``.
``_check_bridge_enrollments`` is asserted to include this workspace's line.

- No enrollment is ``ok``.
- A token 10 days from expiry is ``warn``; 5 days, at expiry, or revoked under an
  active row is ``FAIL``.
- A revoked enrollment is not counted at all.
- The detail is counts only: no enrollment, token or account id.

Every fingerprint is synthetic, and every enrollment row is removed in teardown
(#238's rule).
"""

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from rheo_app_cli.commands.doctor import (
    _check_bridge_enrollments,
    _check_workspace_enrollments,
)
from rheo_contracts import WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.evidence.enrollment import ENROLLMENT_CREATE, ENROLLMENT_REVOKE
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.storage import control_tables
from rheo_core.storage.evidence_enrollment_tables import evidence_enrollment
from rheo_core.storage.evidence_tables import evidence_unit
from sqlalchemy import delete, inspect, update

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture(autouse=True)
def clean_evidence_rows(cluster: ClusterSession, workspace: UUID) -> Iterator[None]:
    """Remove this test's enrollment and evidence rows before the next test runs."""
    yield
    database = cluster.registry_row(workspace).database_name
    engine = cluster.backend.pools.engine_for(database)
    inspector = inspect(engine)
    with engine.begin() as connection:
        if inspector.has_table("evidence_unit", schema="core"):
            connection.execute(delete(evidence_unit))
        if inspector.has_table("evidence_enrollment", schema="core"):
            connection.execute(delete(evidence_enrollment))


def _ctx(workspace: UUID) -> WorkspaceContext:
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _enroll(workspace: UUID, account_id: UUID) -> Any:
    outcome = dispatch(
        _ctx(workspace),
        ENROLLMENT_CREATE,
        {
            "machine_fingerprint": secrets.token_hex(32),
            "project_fingerprint": secrets.token_hex(32),
            "account_id": str(account_id),
        },
    )
    assert outcome.ok, outcome
    return outcome.result


def _force_token(
    cluster: ClusterSession,
    token_id: UUID,
    *,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> None:
    values: dict[str, object] = {}
    if expires_at is not None:
        values["expires_at"] = expires_at
    if revoked_at is not None:
        values["revoked_at"] = revoked_at
    with cluster.backend.control_engine.begin() as connection:
        connection.execute(
            update(control_tables.access_token)
            .where(control_tables.access_token.c.id == token_id)
            .values(**values)
        )


def _check(cluster: ClusterSession, workspace: UUID, now: datetime) -> tuple[str, str]:
    row = cluster.registry_row(workspace)
    check = _check_workspace_enrollments(cluster.backend, row, now=now)
    assert check.name == f"bridge enrollments {row.id} ({row.slug})"
    return check.level, check.detail


def _assert_content_free(detail: str, *created: Any) -> None:
    for result in created:
        assert str(result.enrollment_id) not in detail
        assert str(result.token_id) not in detail
        assert result.value not in detail


def test_no_enrollment_is_ok(
    cluster: ClusterSession, workspace: UUID, now: datetime
) -> None:
    level, detail = _check(cluster, workspace, now)
    assert level == "ok"
    assert detail.startswith("0 active;")


def test_a_fresh_token_is_ok(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID, now: datetime
) -> None:
    created = _enroll(workspace, owner_account_id)
    level, detail = _check(cluster, workspace, now)
    assert level == "ok"
    assert detail.startswith("1 active;")
    _assert_content_free(detail, created)


def test_ten_days_to_expiry_warns(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID, now: datetime
) -> None:
    created = _enroll(workspace, owner_account_id)
    _force_token(cluster, created.token_id, expires_at=now + timedelta(days=10))
    level, detail = _check(cluster, workspace, now)
    assert level == "warn"
    assert "within 14 days 1" in detail
    _assert_content_free(detail, created)
    assert str(owner_account_id) not in detail


@pytest.mark.parametrize(
    ("offset", "counted"),
    [
        (timedelta(days=5), "expiring within 7 days 1"),
        (timedelta(0), "expired 1"),
        (-timedelta(days=1), "expired 1"),
    ],
    ids=["five_days", "at_expiry", "past_expiry"],
)
def test_near_or_past_expiry_fails(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    now: datetime,
    offset: timedelta,
    counted: str,
) -> None:
    created = _enroll(workspace, owner_account_id)
    _force_token(cluster, created.token_id, expires_at=now + offset)
    level, detail = _check(cluster, workspace, now)
    assert level == "FAIL"
    assert counted in detail
    _assert_content_free(detail, created)


def test_a_revoked_token_under_an_active_row_fails(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID, now: datetime
) -> None:
    created = _enroll(workspace, owner_account_id)
    _force_token(cluster, created.token_id, revoked_at=now)
    level, detail = _check(cluster, workspace, now)
    assert level == "FAIL"
    assert "token revoked 1" in detail
    _assert_content_free(detail, created)


def test_a_revoked_enrollment_is_not_counted(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID, now: datetime
) -> None:
    created = _enroll(workspace, owner_account_id)
    outcome = dispatch(
        _ctx(workspace),
        ENROLLMENT_REVOKE,
        {
            "enrollment_id": str(created.enrollment_id),
            "account_id": str(owner_account_id),
        },
    )
    assert outcome.ok, outcome
    level, detail = _check(cluster, workspace, now)
    assert level == "ok"
    assert detail.startswith("0 active;")


def test_the_sweep_over_active_workspaces_reports_this_one(
    cluster: ClusterSession, workspace: UUID, now: datetime
) -> None:
    row = cluster.registry_row(workspace)
    lines = list(_check_bridge_enrollments(cluster.backend, now=now))
    mine = [
        check
        for check in lines
        if check.name.startswith(f"bridge enrollments {row.id}")
    ]
    assert len(mine) == 1
    assert mine[0].level == "ok"
