"""The redaction policy a native runtime session was built under.

Revision ID: 0009_runtime_session_policy
Revises: 0008_tool_telemetry

Adds two nullable columns to ``core.runtime_session``: ``purpose`` and
``policy_digest``, the digest of the tier-policy inputs the session's first run was
rendered under (``rheo_core/runtime/operations.py``). A continuation resumes only when
both equal the new run's; a row written before this revision has neither and is never
resumed, so an existing session is replaced by a fresh one rather than trusted. No
backfill: nothing records what an old session was shown. Downgrade is not supported in
release one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from rheo_core.storage.core_tables import CORE_SCHEMA

revision: str = "0009_runtime_session_policy"
down_revision: str | None = "0008_tool_telemetry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runtime_session",
        sa.Column("purpose", sa.Text(), nullable=True),
        schema=CORE_SCHEMA,
    )
    op.add_column(
        "runtime_session",
        sa.Column("policy_digest", sa.LargeBinary(), nullable=True),
        schema=CORE_SCHEMA,
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
