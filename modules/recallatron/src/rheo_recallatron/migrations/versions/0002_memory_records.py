"""The six-table memory spine and the ``source_receipt`` seam.

Revision ID: 0002_memory_records
Revises: 0001_schema

Creates exactly the tables, indexes and constraints declared in
``rheo_recallatron.storage.tables``, which carries its own ``MetaData`` so revision
0001 still creates exactly what it created — nothing. Everything lands inside the
``recallatron`` schema: ``tests/postgres/test_memory_records.py`` proves that against a
live ``pg_catalog`` census rather than against this sentence.

**The two extensions are not created here, and that is the contract rather than an
omission.** ``StorageDeclaration.required_extensions`` declares ``vector`` and
``pg_trgm``, and ``rheo_core.modules.operations._create_extensions`` installs them —
under the *core's* code, on the core's connection, before this chain runs (§ Install
step 6). A ``CREATE EXTENSION`` here would put the extension's own types and operators
in ``public``, outside this module's schema, from a statement this module owns: the
ownership census would report the leak, and it would be right to. A caller that drives
this chain directly instead of through install has to install the extensions the way
install does; the test harness has one helper for exactly that.

Downgrade is not supported in release one.
"""

from collections.abc import Sequence

from alembic import op
from rheo_recallatron.storage.tables import memory_metadata

revision: str = "0002_memory_records"
down_revision: str | None = "0001_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``checkfirst=False``: a table already present means this chain is running against
    # a database it does not own, which must fail loudly, never skip.
    memory_metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
