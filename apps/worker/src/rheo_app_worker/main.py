"""The worker image's entry point: ``python -m rheo_app_worker.main``.

Mirrors ``apps/core/src/rheo_app_core/serve.py``'s shape — a ``main()``, an
``if __name__ == "__main__":``, nothing else — with the stop wiring pulled out as its
own function so a test can drive it without launching a process.

``get_backend()`` is called **inside** ``main()``, never at module level. A
module-level call would make the bare ``import rheo_app_worker.main`` resolve the
cluster DSN, and that import is exactly what the deploy topology's smoke check and this
module's own signal test run with no environment configured.
``apps/core/src/rheo_app_core/main.py`` places its equivalent call inside ``lifespan``
for the same reason.
"""

import signal
import threading
from datetime import UTC, datetime
from types import FrameType

# First, and on purpose (#142): ``rheo_core.exports`` and ``rheo_core.operations``
# import each other through ``approvals`` and ``core_ops``, and only the order that
# starts at ``rheo_core.operations`` completes. Every other process entry point reaches
# it first by accident; this one did not, so ``python -m rheo_app_worker.main`` died at
# import. ``tests/test_entry_point_imports.py`` imports every entry point in a fresh
# interpreter so the order cannot silently break again.
import rheo_core.operations  # noqa: F401
from rheo_core.events import ConsumerRegistry
from rheo_core.exports import (
    EXPORT_JOB_KIND,
    RESTORE_JOB_KIND,
    ExportJobPayload,
    RestoreJobPayload,
    run_export_job,
    run_restore_job,
)
from rheo_core.modules import load_modules
from rheo_core.modules.operations import (
    MODULE_INSTALL,
    ModuleInstallPayload,
    run_module_install_job,
)
from rheo_core.runtime import (
    RUNTIME_RUN,
    AdapterRegistry,
    RuntimeJobPayload,
    make_run_runtime_job,
)
from rheo_core.settings import resolve
from rheo_core.storage.postgres import get_backend, reset_backend
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import worker_loop
from rheo_core.work.schedules import (
    RETENTION_SWEEP,
    RetentionSweepPayload,
    run_retention_sweep,
)
from rheo_runtimes import ClaudeCliRuntime

JOB_KINDS = JobKindRegistry()
"""The process-wide job-kind registry.

Export, restore, ``core.runtime.run``, ``core.retention_sweep`` and
``core.module.install`` are registered here, at the composition root, not in
``rheo_core.work.kinds`` — that module holds no process-wide instance so a test can
build its own. ``claude_cli`` is registered on the process ``ADAPTERS`` instance in
this same module.
"""
JOB_KINDS.register(EXPORT_JOB_KIND, ExportJobPayload, run_export_job)
JOB_KINDS.register(RESTORE_JOB_KIND, RestoreJobPayload, run_restore_job)
JOB_KINDS.register(RETENTION_SWEEP, RetentionSweepPayload, run_retention_sweep)
JOB_KINDS.register(MODULE_INSTALL, ModuleInstallPayload, run_module_install_job)

ADAPTERS = AdapterRegistry()
"""Process-wide adapter registry. Production registers ``claude_cli`` here."""
ADAPTERS.register("claude_cli", ClaudeCliRuntime())
JOB_KINDS.register(
    RUNTIME_RUN,
    RuntimeJobPayload,
    make_run_runtime_job(ADAPTERS, lambda: datetime.now(UTC)),
)

CONSUMERS = ConsumerRegistry()
"""The process-wide consumer registry, beside ``JOB_KINDS`` and empty for the same
reason: release one ships no consumer, and this is the one obvious place a later run's
consumer gets registered.

Built here rather than in ``rheo_core.events`` deliberately — that package holds no
module-level instance, so a test builds its own registry and there is no global to
reset between cases. This module is the composition root, which is the only place a
process-wide one belongs.
"""


def install_stop_signals(stop: threading.Event) -> None:
    """Make ``SIGTERM`` and ``SIGINT`` set ``stop``.

    Its own function rather than three lines inside :func:`main` so that it can be
    driven directly by a test: with the wiring inlined, nothing short of launching the
    process could exercise it, and a container's stop signal reaching ``worker_loop``
    would be proved by nothing.

    The handlers are installed for the life of the process, which is what a container
    wants. A test that calls this has to save and restore the process's prior handlers
    itself.
    """

    def _request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)


def refuse_misconfigured_login() -> None:
    """Refuse a configured login adapter that names no owning account.

    An empty executable is an unconfigured skeleton and must still start.
    """

    settings = resolve()
    executable = settings.get_str("runtime.claude_cli.executable")
    kind = settings.get_str("runtime.claude_cli.credential_kind")
    account_id = settings.get_str("runtime.claude_cli.credential_account_id")
    if executable and kind == "login" and not account_id.strip():
        raise SystemExit(
            "runtime.claude_cli.credential_account_id is required when "
            "runtime.claude_cli.executable is set and "
            "runtime.claude_cli.credential_kind is login"
        )


def register_modules() -> None:
    """Load the modules ``modules.installed`` names into this process's registries.

    ``JOB_KINDS`` and ``CONSUMERS`` are read from module scope at call time rather
    than taken as parameters, because they are this composition root's own instances
    and there is nowhere else a worker's job kinds and consumers could go. Passing
    them is what gives module-declared job kinds and subscriptions a registry at all:
    neither ``rheo_core.work.kinds`` nor ``rheo_core.events.consumers`` publishes a
    process-wide one, so ``load_modules()`` defaults both to ``None`` and registers
    neither category unless a composition root supplies it.

    Its own function rather than a line inside :func:`main` so a test can drive it
    without a backend, a cluster or a loop — the same reason
    :func:`install_stop_signals` is one. Called **from** :func:`main`, never at module
    level: a module-level call would resolve settings on the bare
    ``import rheo_app_worker.main`` that the deploy smoke check and this module's own
    signal test run with no environment configured, and it would populate
    ``JOB_KINDS`` before ``tests/test_worker_job_kinds.py``'s import-time assertion
    about the production kinds could read it.
    """
    load_modules(kinds=JOB_KINDS, consumers=CONSUMERS)


def main() -> None:
    refuse_misconfigured_login()
    register_modules()
    backend = get_backend()
    stop = threading.Event()
    install_stop_signals(stop)
    try:
        worker_loop(kinds=JOB_KINDS, consumers=CONSUMERS, backend=backend, stop=stop)
    finally:
        # In a ``finally`` so a loop that exits on its stop signal and one that raises
        # both dispose the backend's engines on the way out.
        reset_backend()


if __name__ == "__main__":
    main()
