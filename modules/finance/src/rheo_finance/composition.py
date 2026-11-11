"""The production ``FinanceContext``: what a command dispatches with.

``build_context`` is the CLI's: the workspace that ``FINANCE_WORKSPACE_ID`` names,
reached as core's operator, its layout bound for the process, and core sessions
(``wiring.core``). The scheduled tick builds its own context in
``wiring.tick_job``. Building one opens no connection, reads no manual file and starts
no process: ``dispatch`` opens a session per operation.

``build_source_context`` is the one-off read of an old standalone ledger for
``finance state export --source-dsn-file``; it never imports core.

Core is imported inside the functions, through ``rheo_finance.wiring``, so importing
this module (as every command does lazily) loads none of it.
"""

from __future__ import annotations

from pathlib import Path

from rheo_finance import db, operations
from rheo_finance.clock import SystemClock
from rheo_finance.context import FinanceContext, FinancePaths, SnapshotProvider
from rheo_finance.contracts import Role
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.settings import default_settings


def build_context(
    role: Role, *, snapshot_provider: SnapshotProvider = txn_snapshot
) -> FinanceContext:
    from rheo_finance.wiring import core

    return core.build_context(role, snapshot_provider=snapshot_provider)


def bind_workspace_layout() -> db.FinanceLayout:
    """Bind the CLI workspace's layout for this process without checking it, so
    ``finance init-data`` can create its directories."""
    from rheo_finance.wiring import core

    return core.bind_workspace_layout()


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
