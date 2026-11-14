"""The finance context over core: the workspace's layout, its sessions, the CLI's
workspace.

``handler_context`` builds the ``FinanceContext`` with settings from the bound layout's
data directory and the system clock. ``context_over_core`` is that context with
``operations.with_core_sessions`` over ``open_unit_of_work``, so every operation a tick
or a command dispatches runs on a fresh core session as ``finance_owner`` or
``finance_reader``.

The CLI names its workspace with ``FINANCE_WORKSPACE_ID`` (a UUID, not a secret) and
reaches it as the operator; the module must be enabled there and the layout must
exist. Nothing here creates a directory: ``finance init-data`` does.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Final
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.boundary.context import Refusal
from rheo_core.boundary.factories import context_for_operator
from rheo_core.storage.data_root import Purpose, workspace_dir
from rheo_core.storage.routing import open_unit_of_work

from rheo_finance import db, operations
from rheo_finance.clock import SystemClock
from rheo_finance.context import (
    FinanceContext,
    FinancePaths,
    SnapshotProvider,
    no_progress,
)
from rheo_finance.contracts import Role
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.model.client import ModelClient
from rheo_finance.settings import load_settings, settings_path

MODULE_ID: Final = "finance"
WORKSPACE_VARIABLE: Final = "FINANCE_WORKSPACE_ID"


def layout_for(core_ctx: WorkspaceContext) -> db.FinanceLayout:
    """``<data root>/workspaces/<id>/uploads/finance/{data,archive,inbox,keys}``."""
    return db.layout_under(workspace_dir(core_ctx, Purpose.UPLOADS))


def handler_context(
    role: Role,
    *,
    model_client: ModelClient | None = None,
    snapshot_provider: SnapshotProvider = txn_snapshot,
    progress: Callable[[], None] | None = None,
) -> FinanceContext:
    """The finance context under the bound layout, with no databases: what a handler
    is given (``dispatch`` clears them too), so it can neither open a session nor
    dispatch again. The read adapter (``wiring.reads``) runs a handler with it."""
    data = db.data_dir()
    return FinanceContext(
        settings=load_settings(settings_path(data)),
        clock=SystemClock(),
        paths=FinancePaths(data=data),
        role=role,
        snapshot_provider=snapshot_provider,
        model_client=model_client,
        progress=progress if progress is not None else no_progress,
    )


def context_over_core(
    core_ctx: WorkspaceContext,
    role: Role,
    *,
    model_client: ModelClient | None,
    snapshot_provider: SnapshotProvider = txn_snapshot,
    progress: Callable[[], None] | None = None,
) -> FinanceContext:
    """``handler_context`` on ``core_ctx``'s workspace, with its core sessions.

    ``progress`` is the job's checkpoint when a tick builds it; the CLI passes none,
    so its long steps beat a no-op."""
    ctx = handler_context(
        role,
        model_client=model_client,
        snapshot_provider=snapshot_provider,
        progress=progress,
    )
    return operations.with_core_sessions(
        ctx, open_session=lambda: open_unit_of_work(core_ctx)
    )


def workspace_id() -> UUID:
    raw = os.environ.get(WORKSPACE_VARIABLE, "").strip()
    try:
        return UUID(raw)
    except ValueError:
        raise db.LayoutRefused("finance_workspace_not_set") from None


def operator_context() -> WorkspaceContext:
    """The operator's context on the CLI's workspace, with finance enabled there."""
    core_ctx = context_for_operator(workspace_id())
    if isinstance(core_ctx, Refusal):
        raise db.LayoutRefused("finance_workspace_unavailable")
    if MODULE_ID not in core_ctx.enabled_modules:
        raise db.LayoutRefused("finance_module_not_enabled")
    return core_ctx


def bind_workspace_layout() -> db.FinanceLayout:
    """Bind the CLI workspace's layout for this process, unchecked (for init-data)."""
    layout = layout_for(operator_context())
    db.use_layout(layout)
    return layout


def build_context(
    role: Role, *, snapshot_provider: SnapshotProvider = txn_snapshot
) -> FinanceContext:
    """The CLI's context: the workspace's layout bound for the process, checked, and
    core sessions. A CLI process registers no runtime adapters, so no model client.

    Current in process is bound for the process too, with no consumer registry, so
    ``finance sink approve|preview|state`` and ``finance rebuild`` compute the spec
    the tick does; no command syncs, and a reminder session opened here refuses."""
    from rheo_finance.sinks import current
    from rheo_finance.wiring.current import CoreCurrent

    core_ctx = operator_context()
    db.use_layout(db.check_layout(layout_for(core_ctx)))
    current.use_in_process(CoreCurrent(core_ctx, consumers=None))
    return context_over_core(
        core_ctx, role, model_client=None, snapshot_provider=snapshot_provider
    )
