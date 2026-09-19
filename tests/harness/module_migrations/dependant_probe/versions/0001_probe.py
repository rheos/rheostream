"""The fixture chain's one revision: a table inside the module's own schema.

Revision ID: 0001_probe
Revises: (base)

The schema is read from the Alembic context rather than written as a literal, so the
table this creates and the version table ``env.py`` configures are the same schema by
construction. ``run_chain`` has already created that schema under the advisory lock
before this runs, which is why nothing here creates one.
"""

from collections.abc import Sequence

from alembic import op
from harness.module_migrations import PROBE_TABLE

revision: str = "0001_probe"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    op.execute(f"CREATE TABLE IF NOT EXISTS {schema}.{PROBE_TABLE} (id bigint)")


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
