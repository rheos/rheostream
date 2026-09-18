"""FR 12 / edge 10: continuation bind uses the stored CLI handle, not the UUID.

Mismatch (including expiry) does not resume. A bound match puts ``native_handle``
on ``AdapterSpawn``. Recording doubles only — no live ``claude``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.registry import register_harness
from harness.runtime_matrix import (
    RecordingAdapter,
    frozen_clock,
    kinds,
    owner_context,
    payload,
    visit,
    workspace_engine,
)
from rheo_contracts import ActorKind, AudienceKind
from rheo_core.operations import dispatch
from rheo_core.operations.core_ops import register_core_operations
from rheo_core.refs import uuid7
from rheo_core.runtime import RUNTIME_RUN
from rheo_core.storage import runtime_tables

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_harness()


def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_kind", "api_key")


def _insert_session(
    engine,
    *,
    session_id,
    workspace_owner: UUID,
    native_handle: str,
    now: datetime,
    expires_at: datetime,
    actor_id: UUID | None = None,
) -> None:
    actor = workspace_owner if actor_id is None else actor_id
    with engine.begin() as connection:
        connection.execute(
            runtime_tables.runtime_session.insert().values(
                id=session_id,
                runtime_id="claude_cli",
                credential_scope="api_key",
                actor_kind=ActorKind.ACCOUNT.value,
                actor_id=actor,
                audience_kind=AudienceKind.SESSION.value,
                audience_id=actor,
                native_handle=native_handle,
                created_at=now,
                last_used_at=now,
                expires_at=expires_at,
            )
        )


def test_continuation_actor_mismatch_does_not_resume(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    ctx = owner_context(cluster, workspace, owner_account_id)
    session_id = uuid7()
    engine = workspace_engine(cluster, workspace)
    now = datetime.now(UTC)
    _insert_session(
        engine,
        session_id=session_id,
        workspace_owner=owner_account_id,
        native_handle="foreign-cli-handle",
        now=now,
        expires_at=now + timedelta(hours=72),
        actor_id=uuid7(),
    )
    adapter = RecordingAdapter()
    body = payload(continuation=str(session_id))
    dispatched = dispatch(ctx, RUNTIME_RUN, body)
    assert dispatched.state == "pending", dispatched
    visit(cluster, workspace, kinds(adapter, frozen_clock(now)))
    assert len(adapter.start_calls) == 1
    spawn = adapter.start_calls[0][1]
    assert spawn.native_handle is None
    assert spawn.native_handle != "foreign-cli-handle"
    assert spawn.native_handle != str(session_id)


def test_continuation_bound_match_puts_cli_native_handle_not_uuid(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    ctx = owner_context(cluster, workspace, owner_account_id)
    session_id = uuid7()
    engine = workspace_engine(cluster, workspace)
    now = datetime.now(UTC)
    _insert_session(
        engine,
        session_id=session_id,
        workspace_owner=owner_account_id,
        native_handle="native-cli-handle",
        now=now,
        expires_at=now + timedelta(hours=72),
    )
    adapter = RecordingAdapter(native_handle="rotated-handle")
    body = payload(continuation=str(session_id))
    dispatched = dispatch(ctx, RUNTIME_RUN, body)
    assert dispatched.state == "pending", dispatched
    visit(cluster, workspace, kinds(adapter, frozen_clock(now)))
    spawn = adapter.start_calls[0][1]
    assert spawn.native_handle == "native-cli-handle"
    assert spawn.native_handle != str(session_id)


def test_continuation_expired_session_is_treated_as_mismatch(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    ctx = owner_context(cluster, workspace, owner_account_id)
    session_id = uuid7()
    engine = workspace_engine(cluster, workspace)
    now = datetime.now(UTC)
    _insert_session(
        engine,
        session_id=session_id,
        workspace_owner=owner_account_id,
        native_handle="stale-handle",
        now=now - timedelta(hours=80),
        expires_at=now - timedelta(seconds=1),
    )
    adapter = RecordingAdapter()
    body = payload(continuation=str(session_id))
    dispatched = dispatch(ctx, RUNTIME_RUN, body)
    assert dispatched.state == "pending", dispatched
    visit(cluster, workspace, kinds(adapter, frozen_clock(now)))
    spawn = adapter.start_calls[0][1]
    assert spawn.native_handle is None
