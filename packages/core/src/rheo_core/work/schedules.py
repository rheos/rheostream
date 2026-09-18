"""Schedule ticker and the ``core.retention_sweep`` job.

``run_due_schedules`` is the one-function insertion ``loop.visit_workspace`` calls
after it acquires a workspace engine. It does not parse stored cron expressions:
release-one cadence is ``next_run_at + 1 day``. Every enqueue carries
``RetentionSweepPayload.workspace_id`` from the visit, never an empty dict.

``run_retention_sweep`` reads that id from the payload (``HandlerUnitOfWork`` has
no workspace slot), deletes expired ``core.runtime_transcript`` rows, and unlinks
regular files under that workspace's Claude CLI ``projects/`` subtree only. The
once-copied login seed at the configuration-directory root is not a session file.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Connection, delete, func, select, update

from rheo_core.settings import resolve
from rheo_core.settings.storage_source import PostgresOverrideSource
from rheo_core.storage import work_tables as t
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.runtime_tables import runtime_transcript
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.jobs import enqueue_job

RETENTION_SWEEP: Final = "core.retention_sweep"
_CLI_CONFIG_DIR: Final = "claude-cli"
_SESSION_SUBDIR: Final = "projects"
_DAY: Final = timedelta(days=1)


class RetentionSweepPayload(BaseModel):
    """The ``core.retention_sweep`` job's input. ``workspace_id`` is always present."""

    workspace_id: UUID


def earliest_schedule_due_at(conn: Connection) -> datetime | None:
    """The soonest enabled ``core.schedule.next_run_at``, or ``None``.

    Folded into ``visit_workspace``'s remaining-instant write beside job and
    delivery instants. Disabled rows are not a reason to wake the worker.
    """
    value = conn.execute(
        select(func.min(t.schedule.c.next_run_at)).where(t.schedule.c.enabled.is_(True))
    ).scalar_one_or_none()
    return value if isinstance(value, datetime) else None


def run_due_schedules(conn: Connection, *, workspace_id: UUID, now: datetime) -> None:
    """Enqueue each due enabled schedule and advance it by one day. Does not commit.

    ``workspace_id`` is keyword-only beside ``now`` and is dumped onto every job
    as ``str(workspace_id)``. Stored ``cron`` is ignored. Does not enqueue ``{}``.
    """
    budget = resolve().get_int("work.max_attempts")
    rows = conn.execute(
        select(t.schedule).where(
            t.schedule.c.enabled.is_(True),
            t.schedule.c.next_run_at <= now,
        )
    ).all()
    for row in rows:
        enqueue_job(
            conn,
            kind=str(row.job_kind),
            payload={"workspace_id": str(workspace_id)},
            now=now,
            max_attempts=budget,
        )
        conn.execute(
            update(t.schedule)
            .where(t.schedule.c.id == row.id)
            .values(last_run_at=now, next_run_at=now + _DAY)
        )


def run_retention_sweep(
    uow: HandlerUnitOfWork,
    payload: BaseModel,
    token: CancellationToken,
) -> None:
    """Delete expired transcripts and unlink expired session files for ``payload``."""
    assert isinstance(payload, RetentionSweepPayload)
    token.checkpoint()
    now = datetime.now(UTC)
    uow.connection.execute(
        delete(runtime_transcript).where(runtime_transcript.c.retention_until < now)
    )
    settings = resolve(
        workspace_id=payload.workspace_id, source=PostgresOverrideSource()
    )
    retention_days = settings.get_int("runtime.transcript_retention_days")
    horizon = now - timedelta(days=retention_days)
    config_dir = (
        workspace_dir_for(payload.workspace_id, Purpose.SCRATCH) / _CLI_CONFIG_DIR
    )
    session_root = config_dir / _SESSION_SUBDIR
    if (
        not config_dir.is_dir()
        or session_root.is_symlink()
        or not session_root.is_dir()
    ):
        return
    _unlink_expired_session_files(session_root, horizon=horizon, token=token)


def _unlink_expired_session_files(
    session_root: Path,
    *,
    horizon: datetime,
    token: CancellationToken,
) -> None:
    """Unlink regular files under ``session_root`` older than ``horizon``.

    Recurses only under ``session_root``. Does not follow directory symlinks.
    Does not unlink through a file symlink, so a link whose target sits outside
    ``session_root`` is left alone. Does not delete directories.
    """
    for dirpath, dirnames, filenames in os.walk(session_root, followlinks=False):
        token.checkpoint()
        dirnames[:] = [
            name for name in dirnames if not (Path(dirpath) / name).is_symlink()
        ]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if mtime < horizon:
                path.unlink()
