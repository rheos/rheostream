"""AC 1: install then enable Recallatron through the two public operations alone.

Seam: ``core.module.install`` dispatched through the real registry and carried to
completion by the real worker loop, then ``core.module.enable`` dispatched the same
way, then the result read back through ``core.workspace.status`` — the same three calls
an operator makes. Nothing here reaches into a handler, seeds a ``core.module_state``
row, or asserts against a table directly: the point of this file is that a workspace
gets from ``absent`` to a status response reporting a real version and a real schema
version without anything else being touched, and a test that wrote a row itself would
have removed the only interesting step.

``tests/postgres/test_module_install.py`` and ``tests/postgres/test_module_enable.py``
are where each operation's own refusals and steps are pinned, with fixture modules.
This file uses Recallatron — the one real ``rheo.modules`` distribution in the
checkout — and asserts only what the two together produce.

**AC 1's other clause is not proved here, and cannot be.** "No file under
``packages/core/`` or ``packages/contracts/`` is edited to make this pass" is a
statement about the *import graph and the diff*, not about anything observable from
inside a running workspace, and no assertion in this file could distinguish a core that
had been edited for Recallatron's benefit from one that had not. It is proved
separately, by Prompt 8's ``tests/test_module_import_graph.py``, which reads the
dependency direction itself. A reader who takes this file as the whole of AC 1 is
reading a smaller claim than it makes.
"""

import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import chain_head, loaded_probe_modules
from rheo_contracts import WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.events import ConsumerRegistry
from rheo_core.modules.operations import (
    ENABLED_STATE,
    MODULE_ENABLE,
    MODULE_INSTALL,
    ModuleInstallPayload,
    run_module_install_job,
)
from rheo_core.operations import (
    OPERATION_GET,
    WORKSPACE_STATUS,
    WorkspaceStatus,
    dispatch,
    register_core_operations,
)
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import HEARTBEAT_SECONDS, visit_workspace
from rheo_recallatron import MANIFEST as RECALLATRON_MANIFEST

pytestmark = pytest.mark.postgres

OWNER = "module-lifecycle-under-test"
RECALLATRON_ID = RECALLATRON_MANIFEST.module_id


@pytest.fixture(autouse=True)
def registrations() -> None:
    """The shipped core operations, which is where both module operations live."""
    register_core_operations()


@pytest.fixture
def operator(workspace: UUID) -> WorkspaceContext:
    """An operator context: both operations are ``owner, operator``."""
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def advancing_clock() -> Callable[[], datetime]:
    """A clock that moves more than one heartbeat on every read.

    The install handler's cancellation checkpoints throttle against the clock they are
    given, so a fixed clock silences every one after the first. Nothing here requests a
    cancellation, but the visit still has to begin *after* the job the dispatch above
    enqueued: a clock pinned to an instant captured before that dispatch would put the
    visit in the job's past and ``acquire_lease`` would match nothing.
    """
    base = datetime.now(UTC) + timedelta(seconds=1)
    reads = itertools.count()
    return lambda: base + timedelta(seconds=(HEARTBEAT_SECONDS + 1) * next(reads))


def run_the_worker(cluster: ClusterSession, workspace_id: UUID) -> None:
    """One real worker visit, with a registry holding only the install job kind.

    A local :class:`JobKindRegistry` rather than ``apps/worker``'s module-level
    ``JOB_KINDS``: ``tests/test_worker_job_kinds.py`` asserts that instance's exact
    contents, so writing into it from here would make that file's answer depend on
    collection order. What this registry holds is the same three arguments the
    composition root registers.
    """
    kinds = JobKindRegistry()
    kinds.register(MODULE_INSTALL, ModuleInstallPayload, run_module_install_job)
    visit_workspace(
        DueWorkspace(workspace_id=workspace_id, observed_due_at=None),
        kinds=kinds,
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        owner=OWNER,
        clock=advancing_clock(),
        jitter=None,
    )


def record_of(ctx: WorkspaceContext, operation_id: UUID | None) -> Any:
    """The operation record, through the supported read rather than a query."""
    assert operation_id is not None
    outcome = dispatch(ctx, OPERATION_GET, {"operation_id": str(operation_id)})
    assert outcome.ok, outcome
    assert outcome.result is not None
    return outcome.result


def status_of(ctx: WorkspaceContext) -> WorkspaceStatus:
    outcome = dispatch(ctx, WORKSPACE_STATUS, {})
    assert outcome.ok, outcome
    assert isinstance(outcome.result, WorkspaceStatus), outcome.result
    return outcome.result


def test_install_then_enable_makes_workspace_status_report_recallatron(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    operator: WorkspaceContext,
) -> None:
    """AC 1 end to end: two operations in, one status response out.

    The status is read **before** anything happens as well as after, so "the module is
    reported" cannot be true of a workspace that was already carrying it — a fresh
    workspace reports no modules at all, and that is the baseline the final assertion
    is a change from.

    The two operations are dispatched against **separate contexts**, both built through
    ``context_for_operator``. The one that dispatched the install was built before the
    module had a row of any kind, and the one that reads the final status is built
    after the enable committed: no context here is reused across the state change it
    caused, which is what makes the last read a real "next request" rather than a stale
    object that happens to agree.

    Each of the four fields the contract names is asserted for its own reason.
    ``package_version`` is the installed distribution's own metadata, so a status that
    reported a hardcoded string would fail here. ``state`` is ``enabled`` and not
    ``installed``, so the second operation is doing work the first did not.
    ``schema_version`` is the revision Recallatron's chain applied, which is null unless
    its ``env.py`` configured both version-table keywords — the silent breach the
    fixture chains exist to make loud. And ``modules`` holds exactly one entry, because
    a status that listed everything the deployment loaded rather than what this
    workspace installed would also have matched a laxer assertion.
    """
    assert status_of(operator).modules == []

    with loaded_probe_modules(monkeypatch, RECALLATRON_ID):
        # The head its chain applies, read off the scripts while the module is loaded
        # rather than pinned as a literal that the next revision would quietly falsify.
        revision = chain_head(RECALLATRON_MANIFEST)
        installing = dispatch(operator, MODULE_INSTALL, {"module_id": RECALLATRON_ID})
        assert installing.state == "pending", installing
        run_the_worker(cluster, workspace)
        assert record_of(operator, installing.operation_id).state == "succeeded"

        # A context built after the install and before the enable: the module has a
        # row now, and it is not an enabled one.
        between = context_for_operator(workspace)
        assert isinstance(between, WorkspaceContext), between
        assert RECALLATRON_ID not in between.enabled_modules

        enabling = dispatch(between, MODULE_ENABLE, {"module_id": RECALLATRON_ID})
        assert enabling.ok, enabling

    after = context_for_operator(workspace)
    assert isinstance(after, WorkspaceContext), after
    assert RECALLATRON_ID in after.enabled_modules

    status = status_of(after)
    assert [module.model_dump() for module in status.modules] == [
        {
            "module_id": RECALLATRON_ID,
            "package_version": RECALLATRON_MANIFEST.package_version,
            "state": ENABLED_STATE,
            "schema_version": revision,
        }
    ]
    assert status.modules[0].package_version, "the reported version must not be empty"
