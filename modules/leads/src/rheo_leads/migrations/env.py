"""Use only the workspace connection supplied by the orchestrator."""

from alembic import context

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Leads migrations require the orchestrator connection")
context.configure(
    connection=connection,
    target_metadata=None,
    version_table="alembic_version_leads",
    version_table_schema="leads",
)
with context.begin_transaction():
    context.run_migrations()
