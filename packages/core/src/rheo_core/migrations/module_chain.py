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


def newly_applied(
    chain: str, *, before: frozenset[str], after: frozenset[str]
) -> tuple[str, ...]:
    """Every revision id ``chain`` gained between two recorded-head snapshots, oldest
    first.

    **A set difference is not the answer, and the difference is invisible on a
    one-revision chain.** Alembic's version table records only the current head, not
    the history that reached it, so a chain upgraded from base to its sixth revision
    records exactly one id — and ``after - before`` reports one applied step where six
    ran. The walk down the script directory is what recovers the six.
    ``iterate_revisions(after, lower)`` yields every revision above ``lower`` up to and
    including ``after``, newest first, and yields nothing when the two snapshots agree.

    ``"base"`` for an empty ``before``: the version table did not exist, or held
    nothing, so every revision reaching ``after`` is newly applied.
    """
    script = ScriptDirectory.from_config(build_config(chain))
    lower = tuple(sorted(before)) if before else "base"
    walked = tuple(script.iterate_revisions(tuple(sorted(after)), lower))
    # Reversed, because the rows go in oldest first so the table reads as the order
    # the steps actually ran in.
    return tuple(revision.revision for revision in reversed(walked))


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

    One row per **newly applied** revision, which :func:`newly_applied` is what
    computes: a second call applies nothing, so it writes nothing, and a re-run is a
    clean no-op rather than a duplicate.
    """
    module_id = manifest.module_id
    before = recorded_revisions(connection, module_id)
    run_chain(connection, module_id, expected_database=expected_database)
    after = recorded_revisions(connection, module_id)
    for revision in newly_applied(module_id, before=before, after=after):
        insert_module_schema_version(
            connection,
            module_id=module_id,
            schema_version=revision,
            # Per row rather than one timestamp for the batch: the table's documented
            # reading is that the latest row is the current version, and a multi-step
            # chain sharing one timestamp would leave that unresolvable.
            applied_at=datetime.now(UTC),
            core_version_at_apply=core_version,
        )
