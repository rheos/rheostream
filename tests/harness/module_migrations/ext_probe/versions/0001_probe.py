"""``ext_probe``'s one revision: a table whose column type the extension provides.

Revision ID: 0001_probe
Revises: (base)

**The ``citext`` column is what pins "extensions before any migration statement", and
without it that ordering is unobservable.** The other fixture chains create a plain
``bigint`` column, so moving the ``CREATE EXTENSION`` step after the chain changes
nothing they can see: the whole job runs in one transaction, so a failure rolls the
DDL back either way and "the migration did not run" and "it ran and was rolled back"
leave the same empty database behind. A revision that *uses* the declared extension
is the one thing that tells the two orderings apart — run the extension step second
and this statement fails with an undefined type, and the install that should have
succeeded reports ``migration_failed`` instead.

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
    op.execute(
        f"CREATE TABLE IF NOT EXISTS {schema}.{PROBE_TABLE} (id bigint, label citext)"
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
