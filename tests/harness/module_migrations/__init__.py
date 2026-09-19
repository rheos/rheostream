"""Alembic chains for the fixture modules ``tests/harness/modules.py`` declares.

One subpackage per fixture module, named for its ``module_id``, each holding the
``env.py`` and ``versions/`` directory Alembic needs. A subpackage rather than one
shared chain, because ``script_location`` is resolved per chain from the manifest's
``storage.migrations_path`` and the version table's name is derived from the module
id: two modules sharing a script directory would share one ``env.py``, which can name
only one version table, and the second module's history would be recorded under the
first one's name.

**Every ``env.py`` here configures both version-table keywords, and that is the point
of the fixtures as much as the extensions are.** A module chain whose ``env.py`` is
left on Alembic's defaults migrates perfectly cleanly while writing **zero**
``core.module_schema_version`` rows and raising nothing — the orchestrator reads a
table that never fills, so the ``schema_ahead`` guard is vacuous too. The pairing is
stated at
:data:`~rheo_core.migrations.orchestrator.MODULE_VERSION_TABLE_PREFIX` and in
``docs/architecture/module-contract.md`` § Storage.

These fixtures **derive** the pair from that public constant and their own directory
name, where a shipped module writes it as a literal and asserts it in its own
contract tests. The difference is deliberate: a real module's ``env.py`` is what the
contract test is checking, so it must be able to be wrong; a fixture's job is to be
correct by construction so the install path under test is the only thing that can
fail.
"""

from pathlib import Path
from typing import Final

from rheo_core.migrations.orchestrator import MODULE_VERSION_TABLE_PREFIX

PROBE_TABLE: Final = "probe_row"
"""The one table every fixture revision creates, inside its own module's schema.

An observable artifact rather than an empty revision: "the chain ran" and "the chain
did not run" are both asserted by ``tests/postgres/test_module_install.py``, and a
revision that created nothing would leave the version table as the only evidence.
"""


def module_id_of(env_file: str) -> str:
    """The module id a fixture chain belongs to: its own directory's name.

    Derived rather than repeated, so a chain directory cannot be named for one module
    while its ``env.py`` records another's history.
    """
    return Path(env_file).resolve().parent.name


def configure_module_chain(module_id: str) -> None:
    """Configure and run one fixture module's chain on the orchestrator's connection.

    Refuses without a connection for the same reason every chain in this tree does:
    the chain runs only through ``rheo_core.migrations.orchestrator``, which has
    already asserted which database it is connected to. ``alembic`` invoked by hand
    has neither a URL nor a connection, and would otherwise migrate nothing while
    appearing to succeed.
    """
    from alembic import context

    connection = context.config.attributes.get("connection")
    if connection is None:
        raise RuntimeError(
            f"the {module_id} fixture migration chain runs only through "
            "rheo_core.migrations.orchestrator: no connection was provided"
        )
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table=f"{MODULE_VERSION_TABLE_PREFIX}{module_id}",
        version_table_schema=module_id,
    )
    with context.begin_transaction():
        context.run_migrations()
