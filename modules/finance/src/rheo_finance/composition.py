"""The production ``FinanceContext``: what a command or a job dispatches with.

Settings from ``$FINANCE_DATA_DIR/settings.toml`` (defaults when it is missing), the
system clock, and the owner and reader DSNs from ``db.env``. Building one opens no
connection, reads no manual file and starts no process: ``dispatch`` connects per
operation.

No model client is set: the direct ``claude -p`` client was not imported into
rheoStream, whose modules reach a model only through the runtime contract. Until that
client lands, a model step finds no client and the ledger keeps working on what is
already classified.
"""

from __future__ import annotations

from pathlib import Path

from rheo_finance import db, operations
from rheo_finance.clock import SystemClock
from rheo_finance.context import FinanceContext, FinancePaths, SnapshotProvider
from rheo_finance.contracts import Role
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.settings import default_settings, load_settings, settings_path


def build_context(
    role: Role, *, snapshot_provider: SnapshotProvider = txn_snapshot
) -> FinanceContext:
    data = db.data_dir()
    settings = load_settings(settings_path(data))
    ctx = FinanceContext(
        settings=settings,
        clock=SystemClock(),
        paths=FinancePaths(data=data),
        role=role,
        snapshot_provider=snapshot_provider,
    )
    return operations.with_databases(
        ctx, owner_url=db.owner_url(), reader_url=db.reader_url()
    )


def build_source_context(dsn_file: Path) -> FinanceContext:
    """An owner context over an old standalone ledger, for the one-off state export.

    ``dsn_file`` is a private env file (mode 0600, not a symlink) holding
    ``FINANCE_OWNER_DSN``; both connection names use that DSN. Nothing of core is
    imported: the source is read from a checkout, outside any workspace.
    """
    values = db.parse_env_text(db.read_private_file(dsn_file))
    dsn = values.get(db.OWNER_DSN_KEY, "")
    if not dsn:
        raise db.DbError(f"the DSN file has no value for {db.OWNER_DSN_KEY}")
    url = db.to_sqlalchemy_url(dsn)
    ctx = FinanceContext(
        settings=default_settings(),
        clock=SystemClock(),
        paths=FinancePaths(data=db.data_dir()),
        role=Role.OWNER,
        snapshot_provider=txn_snapshot,
    )
    return operations.with_databases(ctx, owner_url=url, reader_url=url)
