"""The finance Alembic environment: one chain, its version table in schema ``finance``.

Only the connection an orchestrator passes in ``config.attributes["connection"]`` is
used: core's install and upgrade, and the test harness. ``prepare_roles`` makes sure
the two roles exist, then every revision runs as ``finance_owner``, which creates the
version table, and the role is reset before control returns, because core's install
goes on in the same transaction.
"""

from __future__ import annotations

from alembic import context

from rheo_finance import db

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("finance migrations require the orchestrator connection")
db.prepare_roles(connection)
with db.acting_as(connection, db.OWNER_ROLE):
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table=db.VERSION_TABLE,
        version_table_schema=db.SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()
