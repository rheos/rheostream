"""AC 9: ``core.retention_sweep`` ticker enqueue and handler file/transcript policy.

Seams: ``run_due_schedules`` due-row enqueue with
``RetentionSweepPayload(workspace_id)`` (never ``{}``), ``run_retention_sweep``
transcript DELETE, and ``projects/`` subtree unlink only — the login seed at the
Claude CLI config-dir root survives.

Evidence retention (spec FR 10, § Architecture component 10): the sweep ages expired
``pending`` evidence to ``gap/expired_pending`` and deletes ``settled``/``gap`` rows
older than ``runtime.transcript_retention_days``. Two seams: it does so on a workspace
with no CLI config directory (the early return below the purge), and it skips a row
another transaction holds instead of waiting on it. Every text is synthetic.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.evidence import (
    EvidenceWorkspace,
    enable_recording,
    handler_uow,
    probe_registry,
)
from pydantic import ValidationError
from rheo_contracts import ContextPurpose
from rheo_core.events import ConsumerRegistry
from rheo_core.evidence.record import MAX_BYTES_KEY, NewEvidence, record_evidence
from rheo_core.refs import uuid7
from rheo_core.storage import runtime_tables, work_tables
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.evidence_tables import evidence_unit
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.work.jobs import enqueue_job
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_core.work.schedules import (
    RETENTION_SWEEP,
    RetentionSweepPayload,
    run_due_schedules,
    run_retention_sweep,
)
from sqlalchemy import Connection, Engine, func, insert, select, text, update

pytestmark = pytest.mark.postgres

OWNER = "retention-sweep-test"
_OLD = timedelta(days=91)
_RECENT = timedelta(days=1)


def workspace_engine(cluster: ClusterSession, workspace_id: UUID) -> Engine:
    row = cluster.registry_row(workspace_id)
    return cluster.backend.pools.engine_for(row.database_name)


def _now() -> datetime:
    return datetime.now(UTC)


def _config_dir(workspace_id: UUID) -> Path:
    return workspace_dir_for(workspace_id, Purpose.SCRATCH) / "claude-cli"


def _kinds() -> JobKindRegistry:
    kinds = JobKindRegistry()
    kinds.register(RETENTION_SWEEP, RetentionSweepPayload, run_retention_sweep)
    return kinds


def _visit(cluster: ClusterSession, workspace_id: UUID, *, at: datetime) -> None:
    visit_workspace(
        DueWorkspace(workspace_id=workspace_id, observed_due_at=None),
        kinds=_kinds(),
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        owner=OWNER,
        clock=lambda: at,
        jitter=None,
    )


def _write(path: Path, body: str, *, mtime: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    stamp = mtime.timestamp()
    os.utime(path, (stamp, stamp))


def _insert_transcript(
    connection: Connection,
    *,
    body: str,
    retention_until: datetime,
) -> UUID:
    request_id = uuid7()
    recorded = retention_until
    connection.execute(
        insert(runtime_tables.runtime_request).values(
            id=request_id,
            operation_id=uuid7(),
            runtime_id="claude_cli",
            model_id="sonnet",
            purpose="respond",
            context_digest=b"\x00" * 32,
            permitted_tools=[],
            requirements=[],
            deadline_seconds=600,
            max_iterations=40,
            max_output_bytes=1_000_000,
            terminal_event=None,
            failure_kind=None,
            usage_input_tokens=None,
            usage_output_tokens=None,
            usage_cost=None,
            usage_kind=None,
            started_at=recorded,
            ended_at=None,
        )
    )
    transcript_id = uuid7()
    connection.execute(
        insert(runtime_tables.runtime_transcript).values(
            id=transcript_id,
            request_id=request_id,
            ordinal=1,
            kind="model_output",
            body=body,
            byte_length=len(body.encode()),
            recorded_at=recorded,
            retention_until=retention_until,
        )
    )
    return transcript_id


def _make_schedule_due(engine: Engine, *, now: datetime) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(work_tables.schedule)
            .where(work_tables.schedule.c.job_kind == RETENTION_SWEEP)
            .values(next_run_at=now)
        )


def _transcript_ids(engine: Engine) -> set[UUID]:
    with engine.connect() as connection:
        rows = connection.execute(select(runtime_tables.runtime_transcript.c.id)).all()
    return {row.id for row in rows}


def test_due_schedule_enqueues_visit_workspace_id_payload(
    cluster: ClusterSession, workspace: UUID
) -> None:
    engine = workspace_engine(cluster, workspace)
    now = _now()
    with engine.begin() as connection:
        connection.execute(
            update(work_tables.schedule)
            .where(work_tables.schedule.c.job_kind == RETENTION_SWEEP)
            .values(next_run_at=now)
        )
        run_due_schedules(connection, workspace_id=workspace, now=now)
        job = connection.execute(select(work_tables.job)).one()
        row = connection.execute(
            select(work_tables.schedule).where(
                work_tables.schedule.c.job_kind == RETENTION_SWEEP
            )
        ).one()
    payload = RetentionSweepPayload.model_validate(job.input)
    assert payload.workspace_id == workspace
    assert job.kind == RETENTION_SWEEP
    assert job.input != {}
    assert row.last_run_at == now
    assert row.next_run_at == now + timedelta(days=1)


def test_future_schedule_does_not_enqueue(
    cluster: ClusterSession, workspace: UUID
) -> None:
    engine = workspace_engine(cluster, workspace)
    now = _now()
    with engine.begin() as connection:
        run_due_schedules(connection, workspace_id=workspace, now=now)
        count = connection.execute(
            select(func.count()).select_from(work_tables.job)
        ).scalar_one()
    assert count == 0


def test_retention_sweep_removes_expired_transcript_and_projects_files_only(
    cluster: ClusterSession,
    workspace: UUID,
    make_workspace: MakeWorkspace,
) -> None:
    other = make_workspace()
    engine = workspace_engine(cluster, workspace)
    other_engine = workspace_engine(cluster, other)
    now = _now()
    old = now - _OLD
    recent = now - _RECENT

    with engine.begin() as connection:
        expired_id = _insert_transcript(
            connection, body="expired", retention_until=now - timedelta(hours=1)
        )
        kept_id = _insert_transcript(
            connection, body="kept", retention_until=now + timedelta(days=30)
        )
    with other_engine.begin() as connection:
        other_id = _insert_transcript(
            connection, body="other", retention_until=now - timedelta(hours=1)
        )

    config_dir = _config_dir(workspace)
    expired_session = config_dir / "projects" / "nested" / "session.jsonl"
    kept_session = config_dir / "projects" / "nested" / "fresh.jsonl"
    seed = config_dir / "credentials"
    sibling = config_dir / "settings.json"
    outside_target = config_dir / "outside-target"
    escape = config_dir / "projects" / "nested" / "escape"
    _write(expired_session, "old-session", mtime=old)
    _write(kept_session, "fresh-session", mtime=recent)
    _write(seed, "login-seed", mtime=old)
    _write(sibling, "not-a-session", mtime=old)
    _write(outside_target, "must-remain", mtime=old)
    escape.symlink_to(outside_target)

    other_session = _config_dir(other) / "projects" / "nested" / "session.jsonl"
    _write(other_session, "other-session", mtime=old)

    _make_schedule_due(engine, now=now)
    _visit(cluster, workspace, at=now)

    assert not expired_session.exists()
    assert kept_session.is_file()
    assert seed.is_file()
    assert seed.read_text(encoding="utf-8") == "login-seed"
    assert sibling.is_file()
    assert outside_target.is_file()
    assert escape.is_symlink()
    assert other_session.is_file()
    remaining = _transcript_ids(engine)
    assert expired_id not in remaining
    assert kept_id in remaining
    assert other_id in _transcript_ids(other_engine)


def test_empty_payload_fails_validation_before_unlink(
    cluster: ClusterSession, workspace: UUID
) -> None:
    with pytest.raises(ValidationError):
        RetentionSweepPayload.model_validate({})

    engine = workspace_engine(cluster, workspace)
    now = _now()
    old = now - _OLD
    config_dir = _config_dir(workspace)
    session = config_dir / "projects" / "nested" / "session.jsonl"
    seed = config_dir / "credentials"
    _write(session, "must-remain", mtime=old)
    _write(seed, "login-seed", mtime=old)

    with engine.begin() as connection:
        enqueue_job(
            connection,
            kind=RETENTION_SWEEP,
            payload={},
            now=now,
            max_attempts=3,
        )
    _visit(cluster, workspace, at=now)

    assert session.is_file()
    assert seed.is_file()
    with engine.connect() as connection:
        row = connection.execute(select(work_tables.job)).one()
    assert row.state == "failed"


def test_missing_projects_does_not_create_paths(
    cluster: ClusterSession, workspace: UUID
) -> None:
    engine = workspace_engine(cluster, workspace)
    now = _now()
    config_dir = _config_dir(workspace)
    config_dir.mkdir(parents=True, exist_ok=True)
    seed = config_dir / "credentials"
    _write(seed, "login-seed", mtime=now - _OLD)
    session_root = config_dir / "projects"
    assert not session_root.exists()

    _make_schedule_due(engine, now=now)
    _visit(cluster, workspace, at=now)

    assert not session_root.exists()
    assert seed.is_file()


def test_visit_folds_enabled_schedule_into_next_due_at(
    cluster: ClusterSession, workspace: UUID
) -> None:
    engine = workspace_engine(cluster, workspace)
    now = _now()
    result = visit_workspace(
        DueWorkspace(workspace_id=workspace, observed_due_at=None),
        kinds=_kinds(),
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        owner=OWNER,
        clock=lambda: now,
        jitter=None,
    )
    with engine.connect() as connection:
        scheduled = connection.execute(
            select(work_tables.schedule.c.next_run_at).where(
                work_tables.schedule.c.job_kind == RETENTION_SWEEP
            )
        ).scalar_one()
    assert result.jobs_acquired == 0
    assert result.next_due_at == scheduled


# --- evidence retention ------------------------------------------------------------

TURN = "Please move the Thursday planning session to the small meeting room."
_LOCK_TIMEOUT = "2s"
_LOCK_TIMEOUT_SECONDS = 2.0


@pytest.fixture
def recording(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> EvidenceWorkspace:
    """The shared recording fixture on ``workspace``, which has no CLI config dir."""
    ev = EvidenceWorkspace(cluster, workspace, owner_account_id)
    enable_recording(monkeypatch, ev)
    return ev


class _NeverCancelled:
    """A cancellation token that never fires; the sweep only calls ``checkpoint``."""

    def checkpoint(self) -> None:
        return None


def _evidence(
    ev: EvidenceWorkspace, *, recorded_at: datetime, raw_text: str = TURN
) -> NewEvidence:
    authority_id = uuid7()
    return NewEvidence(
        producer_kind="rheo_runtime",
        authority_id=authority_id,
        native_key=f"turn:{authority_id}",
        speaker_account_id=ev.owner_account_id,
        audience_kind="member",
        audience_id=ev.owner_account_id,
        purpose=ContextPurpose.RESPOND,
        recorded_at=recorded_at,
        raw_text=raw_text,
    )


def _record(ev: EvidenceWorkspace, record: NewEvidence, *, now: datetime) -> str:
    """Record one turn through ``record_evidence``; its native key."""
    with ev.handler(probe_registry()) as uow:
        record_evidence(ev.context(), uow, [record], now=now)
    return record.native_key


def _settle(ev: EvidenceWorkspace, native_key: str, *, settled_at: datetime) -> None:
    """Stand in for the drain: settle a recorded ``pending`` row at ``settled_at``."""
    with ev.unit_of_work() as uow:
        uow.connection.execute(
            update(evidence_unit)
            .where(evidence_unit.c.native_key == native_key)
            .values(state="settled", outcome="active", body=None, settled_at=settled_at)
        )
        uow.commit()


def _units(ev: EvidenceWorkspace) -> dict[str, Any]:
    with ev.unit_of_work() as uow:
        rows = uow.connection.execute(select(evidence_unit)).all()
    return {row.native_key: row for row in rows}


def _sweep(ev: EvidenceWorkspace) -> None:
    """One ``run_retention_sweep`` call, bounded so a lock wait fails instead of hangs.

    ``lock_timeout`` turns a blocking ``FOR UPDATE`` into an error after two seconds,
    and ``statement_timeout`` caps any statement, so a sweep that waits on a held row
    reds the test rather than stalling the suite.
    """
    with ev.unit_of_work() as uow:
        uow.connection.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
        uow.connection.execute(text("SET LOCAL statement_timeout = '10s'"))
        run_retention_sweep(
            handler_uow(uow, None),
            RetentionSweepPayload(workspace_id=ev.workspace_id),
            _NeverCancelled(),
        )
        uow.commit()


def test_the_sweep_ages_expired_pending_evidence_on_a_workspace_with_no_cli_dir(
    recording: EvidenceWorkspace,
) -> None:
    """The regression this purge's placement exists to prevent.

    The workspace has no CLI config directory, so the sweep returns before its
    session-file walk. The evidence purge sits above that return and still runs.
    """
    now = _now()
    assert not _config_dir(recording.workspace_id).exists()
    # Past the 24 h pending window, and still inside it.
    expired = _record(
        recording, _evidence(recording, recorded_at=now - timedelta(hours=25)), now=now
    )
    fresh = _record(recording, _evidence(recording, recorded_at=now), now=now)

    _sweep(recording)

    assert not _config_dir(recording.workspace_id).exists()
    units = _units(recording)
    aged = units[expired]
    assert (aged.state, aged.outcome, aged.body) == ("gap", "expired_pending", None)
    assert aged.settled_at is not None and aged.settled_at >= now
    kept = units[fresh]
    assert (kept.state, kept.outcome, kept.body) == ("pending", None, TURN)
    assert kept.settled_at is None


def test_the_sweep_deletes_old_settled_and_gap_evidence_and_keeps_recent(
    recording: EvidenceWorkspace,
) -> None:
    now = _now()
    old_settled = _record(recording, _evidence(recording, recorded_at=now), now=now)
    recent_settled = _record(recording, _evidence(recording, recorded_at=now), now=now)
    _settle(recording, old_settled, settled_at=now - _OLD)
    _settle(recording, recent_settled, settled_at=now - _RECENT)
    # A gap/expired_pending row the sweep itself aged long ago.
    old_gap = _record(
        recording,
        _evidence(recording, recorded_at=now - _OLD - timedelta(days=2)),
        now=now - _OLD - timedelta(days=2),
    )
    with recording.unit_of_work() as uow:
        uow.connection.execute(
            update(evidence_unit)
            .where(evidence_unit.c.native_key == old_gap)
            .values(
                state="gap",
                outcome="expired_pending",
                body=None,
                settled_at=now - _OLD,
            )
        )
        uow.commit()

    _sweep(recording)

    units = _units(recording)
    assert old_settled not in units
    assert old_gap not in units
    assert units[recent_settled].state == "settled"


def test_an_old_oversize_gap_is_deleted_like_any_settled_row(
    recording: EvidenceWorkspace,
) -> None:
    """``record_evidence`` stamps ``settled_at`` on a ``gap/oversize`` row at insert."""
    recording.set_workspace(MAX_BYTES_KEY, 100)
    now = _now()
    old_at = now - _OLD
    old_oversize = _record(
        recording,
        _evidence(recording, recorded_at=old_at, raw_text="e" * 150),
        now=old_at,
    )
    recent_oversize = _record(
        recording,
        _evidence(recording, recorded_at=now, raw_text="f" * 150),
        now=now - _RECENT,
    )
    before = _units(recording)
    assert (before[old_oversize].state, before[old_oversize].outcome) == (
        "gap",
        "oversize",
    )
    assert before[old_oversize].settled_at == old_at

    _sweep(recording)

    units = _units(recording)
    assert old_oversize not in units
    assert units[recent_oversize].outcome == "oversize"


def test_the_sweep_skips_a_pending_row_another_transaction_holds(
    recording: EvidenceWorkspace,
) -> None:
    """A drain may hold a row's lock across a model call; the sweep must not wait.

    Both rows are past their window. One is held ``FOR UPDATE`` by another open
    transaction; the sweep ages the other, leaves the held one ``pending``, and
    returns well inside its ``lock_timeout``. A sweep that waited would raise
    ``LockNotAvailable`` after two seconds instead.
    """
    now = _now()
    held = _record(
        recording, _evidence(recording, recorded_at=now - timedelta(hours=25)), now=now
    )
    free = _record(
        recording, _evidence(recording, recorded_at=now - timedelta(hours=25)), now=now
    )
    engine = recording.cluster.backend.pools.engine_for(recording.database_name)
    with engine.connect() as holder:
        holder.begin()
        holder.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
        holder.execute(
            select(evidence_unit.c.id)
            .where(evidence_unit.c.native_key == held)
            .with_for_update()
        ).one()
        try:
            started = time.monotonic()
            _sweep(recording)
            elapsed = time.monotonic() - started
        finally:
            holder.rollback()

    assert elapsed < _LOCK_TIMEOUT_SECONDS
    units = _units(recording)
    assert units[held].state == "pending"
    assert units[held].body == TURN
    assert (units[free].state, units[free].outcome) == ("gap", "expired_pending")
