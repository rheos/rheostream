"""FR 12 / edge 10: continuation bind uses the stored CLI handle, not the UUID.

Mismatch (including expiry, and since issue #130 a different purpose or redaction
policy, or a row that recorded none) does not resume. A bound match puts
``native_handle`` on ``AdapterSpawn``. Recording doubles only — no live ``claude``.
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
from rheo_contracts import ActorKind, AudienceKind, ContextPurpose
from rheo_core.operations import dispatch
from rheo_core.operations.core_ops import register_core_operations
from rheo_core.redaction.policy import TierPolicy, policy_digest
from rheo_core.refs import uuid7
from rheo_core.runtime import RUNTIME_RUN
from rheo_core.settings import encode_text, resolve, spec_for
from rheo_core.settings.storage_source import PostgresOverrideSource
from rheo_core.storage import runtime_tables
from rheo_core.storage.repositories import upsert_workspace_setting
from sqlalchemy import select

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_harness()


def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_kind", "api_key")


def _current_policy(
    workspace: UUID, purpose: ContextPurpose = ContextPurpose.RESPOND
) -> tuple[str, bytes]:
    """The ``(purpose, policy_digest)`` a run under ``purpose`` would record now."""
    settings = resolve(workspace_id=workspace, source=PostgresOverrideSource())
    policy = TierPolicy.from_settings(purpose, settings)
    return policy.purpose.value, policy_digest(policy)


def _insert_session(
    engine,
    *,
    session_id,
    workspace_owner: UUID,
    native_handle: str,
    now: datetime,
    expires_at: datetime,
    actor_id: UUID | None = None,
    policy: tuple[str | None, bytes | None] | None = None,
    workspace: UUID | None = None,
) -> None:
    """A stored session. ``policy`` defaults to the one a ``respond`` run under the
    workspace's current settings records (so a bound match resumes); pass
    ``(None, None)`` for a row written before revision 0009."""
    actor = workspace_owner if actor_id is None else actor_id
    if policy is None:
        assert workspace is not None, "the default policy is the workspace's"
        policy = _current_policy(workspace)
    stored_purpose, stored_digest = policy
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
                purpose=stored_purpose,
                policy_digest=stored_digest,
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
        workspace=workspace,
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
        workspace=workspace,
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
        workspace=workspace,
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


# --- issue #130 review: a session resumes only under the policy it was built under --


def _run(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    session_id: UUID,
    now: datetime,
    *,
    purpose: str = "respond",
    handle: str = "fresh-handle",
) -> RecordingAdapter:
    ctx = owner_context(cluster, workspace, owner_account_id)
    adapter = RecordingAdapter(native_handle=handle)
    body = payload(continuation=str(session_id), purpose=purpose)
    dispatched = dispatch(ctx, RUNTIME_RUN, body)
    assert dispatched.state == "pending", dispatched
    visit(cluster, workspace, kinds(adapter, frozen_clock(now)))
    return adapter


def _stored(engine, native_handle: str):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(runtime_tables.runtime_session).where(
                    runtime_tables.runtime_session.c.native_handle == native_handle
                )
            )
            .mappings()
            .one()
        )


def test_the_same_policy_resumes_and_a_fresh_run_records_its_policy(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    engine = workspace_engine(cluster, workspace)
    now = datetime.now(UTC)
    session_id = uuid7()
    _insert_session(
        engine,
        session_id=session_id,
        workspace_owner=owner_account_id,
        workspace=workspace,
        native_handle="same-policy-handle",
        now=now,
        expires_at=now + timedelta(hours=72),
    )
    adapter = _run(cluster, workspace, owner_account_id, session_id, now)
    assert adapter.start_calls[0][1].native_handle == "same-policy-handle"

    fresh = _run(
        cluster, workspace, owner_account_id, uuid7(), now, handle="second-handle"
    )
    assert fresh.start_calls[0][1].native_handle is None
    row = _stored(engine, "second-handle")
    assert (row["purpose"], row["policy_digest"]) == _current_policy(workspace)


def test_a_different_purpose_starts_a_fresh_session(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    engine = workspace_engine(cluster, workspace)
    now = datetime.now(UTC)
    session_id = uuid7()
    _insert_session(
        engine,
        session_id=session_id,
        workspace_owner=owner_account_id,
        workspace=workspace,
        native_handle="wider-purpose-handle",
        now=now,
        expires_at=now + timedelta(hours=72),
        policy=_current_policy(workspace, ContextPurpose.INTERNAL_ANALYSIS),
    )
    adapter = _run(
        cluster,
        workspace,
        owner_account_id,
        session_id,
        now,
        purpose="share_with_referral",
    )
    assert len(adapter.start_calls) == 1
    assert adapter.start_calls[0][1].native_handle is None


def test_a_narrowed_redaction_setting_starts_a_fresh_session(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session was built while ``respond`` could see ``internal`` fields; the
    workspace then narrows the list, so resuming would keep what it may no longer
    see."""
    _api_key(monkeypatch)
    engine = workspace_engine(cluster, workspace)
    now = datetime.now(UTC)
    session_id = uuid7()
    _insert_session(
        engine,
        session_id=session_id,
        workspace_owner=owner_account_id,
        workspace=workspace,
        native_handle="before-narrowing-handle",
        now=now,
        expires_at=now + timedelta(hours=72),
    )
    key = "redaction.internal_purposes"
    with engine.begin() as connection:
        upsert_workspace_setting(
            connection,
            key=key,
            value=encode_text(spec_for(key), ("follow_up",)),
            value_type=spec_for(key).type,
            updated_by=None,
        )
    adapter = _run(cluster, workspace, owner_account_id, session_id, now)
    assert adapter.start_calls[0][1].native_handle is None


def test_a_legacy_row_without_a_policy_is_never_resumed(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    engine = workspace_engine(cluster, workspace)
    now = datetime.now(UTC)
    session_id = uuid7()
    _insert_session(
        engine,
        session_id=session_id,
        workspace_owner=owner_account_id,
        native_handle="legacy-handle",
        now=now,
        expires_at=now + timedelta(hours=72),
        policy=(None, None),
    )
    adapter = _run(cluster, workspace, owner_account_id, session_id, now)
    assert adapter.start_calls[0][1].native_handle is None
