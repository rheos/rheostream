"""Alembic environment for Recallatron's per-workspace module chain.

Runs only under the orchestrator's connection (to one workspace database, after its
``current_database()`` assertion) and refuses otherwise; the rule and its reasons are
stated once, in ``rheo_core.migrations``. The version table lives inside the module's
own schema, ``recallatron.alembic_version_recallatron`` — one version table per
module, so a module chain's history can never be confused with the ``core`` chain's
or with another module's.
"""

from alembic import context

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError(
        "the recallatron migration chain runs only through "
        "rheo_core.migrations.orchestrator: no connection was provided, so nothing "
        "will be upgraded (alembic invoked by hand has no URL and no connection)"
    )

context.configure(
    connection=connection,
    # No ``MetaData``: this module owns no tables yet, so there is nothing for
    # autogenerate to compare against. The run that adds the memory tables adds
    # their metadata here beside them.
    target_metadata=None,
    version_table="alembic_version_recallatron",
    version_table_schema="recallatron",
)

with context.begin_transaction():
    context.run_migrations()
