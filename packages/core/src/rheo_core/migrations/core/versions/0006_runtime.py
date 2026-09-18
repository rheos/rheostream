"""Runtime request, context, transcript, and session tables.

Revision ID: 0006_runtime
Revises: 0005_export_records

Creates exactly the four tables on ``rheo_core.storage.runtime_tables``'s own
``MetaData`` so frozen revisions 0001–0005 still create exactly their own sets.
Also inserts one ``core.schedule`` row for ``core.retention_sweep`` (cron stored
for operators only; ``next_run_at`` is migrate-time plus one day). Downgrade is
not supported in release one.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from alembic import op
from rheo_core.refs import uuid7
from rheo_core.storage.runtime_tables import runtime_metadata
from rheo_core.storage.work_tables import schedule
from sqlalchemy import insert

revision: str = "0006_runtime"
down_revision: str | None = "0005_export_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # ``checkfirst=False``: a table already present means this chain is running
    # against a database it does not own, which must fail loudly, never skip.
    runtime_metadata.create_all(bind, checkfirst=False)
    bind.execute(
        insert(schedule).values(
            id=uuid7(),
            module_id="core",
            name="retention_sweep",
            job_kind="core.retention_sweep",
            cron="0 3 * * *",
            enabled=True,
            last_run_at=None,
            next_run_at=datetime.now(UTC) + timedelta(days=1),
        )
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
