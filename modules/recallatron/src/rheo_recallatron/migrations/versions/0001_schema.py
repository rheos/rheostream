"""The ``recallatron`` schema, and nothing inside it.

Revision ID: 0001_schema
Revises: (base)

**No tables.** The memory record types and their tables are a later run's; a table
here would build ahead of a requirement no criterion in this run states. What the
chain needs at release one is a first revision that exists, is idempotent, and leaves
the module's schema in place for the revisions that follow.

**``IF NOT EXISTS`` is load-bearing, not defensive noise, and the bare form breaks
every install.** ``rheo_core.migrations.orchestrator``'s ``run_chain`` takes the
advisory lock (``orchestrator.py:161``) and then executes
``CREATE SCHEMA IF NOT EXISTS <schema>`` itself (``orchestrator.py:162``) *before* it
calls ``command.upgrade`` (``orchestrator.py:170``) — and for a module chain that
schema is the module's own. So by the time this revision runs, ``recallatron`` is
already there, and a bare ``CREATE SCHEMA recallatron`` would raise ``DuplicateSchema``
on a fresh workspace.

Deleting ``orchestrator.py:162`` is not the repair: none of the six ``core``
revisions creates the ``core`` schema (``grep -ri "create schema"`` over that
versions directory returns nothing) precisely because ``:162`` creates it for them,
and ``storage/provisioning.py:214`` says so at the call site. The same line serves the
``control`` chain. That line stays exactly as it is; this statement is the idempotent
backstop that keeps the chain correct if it is ever applied through a bare
``alembic upgrade``, and the one this chain owns. Downgrade is not supported in
release one.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS recallatron")


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
