"""The caller-initiated door for one module's migration chain.

The other door is ``orchestrator.migrate_workspace``, the unattended startup pass over
``core``; it refuses a module chain name outright. The difference is what happens to a
failure. ``migrate_workspace`` catches everything its ``chains`` loop raises and marks
the *whole workspace* ``unavailable``; this function has no ``try``, no
``MigrationResult`` and no control-plane engine, so a bad module revision reaches the
install operation that asked for it and nothing else (FR 11).
"""

from datetime import UTC, datetime

from alembic.script import ScriptDirectory
from sqlalchemy import Connection

from rheo_core.migrations.orchestrator import (
    build_config,
    recorded_revisions,
    run_chain,
)
from rheo_core.modules.manifest import ModuleManifest
from rheo_core.storage.repositories import insert_module_schema_version


def run_module_chain(
    connection: Connection,
    manifest: ModuleManifest,
    *,
    expected_database: str,
    core_version: str,
) -> None:
    """Apply ``manifest``'s chain on ``connection``, recording every step it applied.

    Runs on the caller's own connection inside the caller's own transaction — no new
    connection and no new engine — so the module's DDL, its version-table row and the
    ``core.module_schema_version`` rows below commit or roll back together.
    ``run_chain`` takes the same ``ADVISORY_LOCK_KEY`` the ``core`` chain takes, and
    creates the module's own schema under it.

    **Why the chain is walked rather than read.** Alembic's version table records only
    the current head, not the history that reached it, so "which revisions did this
    call apply" cannot be read back afterwards. Snapshotting the recorded heads either
    side of ``run_chain`` and walking the script directory between them answers it
    exactly: ``iterate_revisions(after, before)`` yields every revision above the old
    heads up to and including the new ones, and yields *nothing* when the two agree.
    That is what makes a re-run a clean no-op — the second call applies no revision,
    so it writes no row — and it is why the row count tracks applied steps rather than
    calls.
    """
    module_id = manifest.module_id
    before = recorded_revisions(connection, module_id)
    run_chain(connection, module_id, expected_database=expected_database)
    after = recorded_revisions(connection, module_id)
    script = ScriptDirectory.from_config(build_config(module_id))
    # ``"base"`` rather than an empty lower bound: the version table did not exist (or
    # held nothing) before this call, so every revision reaching ``after`` is new.
    lower = tuple(sorted(before)) if before else "base"
    applied = tuple(script.iterate_revisions(tuple(sorted(after)), lower))
    # ``iterate_revisions`` walks downwards, newest first; the rows go in oldest first
    # so the table reads as the order the steps actually ran in.
    for revision in reversed(applied):
        insert_module_schema_version(
            connection,
            module_id=module_id,
            schema_version=revision.revision,
            # Per row rather than one timestamp for the batch: the table's documented
            # reading is that the latest row is the current version, and a multi-step
            # chain sharing one timestamp would leave that unresolvable.
            applied_at=datetime.now(UTC),
            core_version_at_apply=core_version,
        )
