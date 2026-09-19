"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Generated through ``rheo_core.migrations.orchestrator.build_config()`` (no Alembic by
hand: see ``rheo_core.migrations``). Every table this chain creates belongs to the
``recallatron`` schema, which ``0001_schema`` already guarantees exists. Downgrade is
not supported in release one.
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401
from alembic import op  # noqa: F401
${imports if imports else ""}
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
