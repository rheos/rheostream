"""Production ``JOB_KINDS`` lives on the worker composition root, and
``register_modules()`` is what a module's job kinds and subscriptions reach it
through.

``register_modules()`` is called from ``main()``, never at import, which is what
keeps the first assertion below — the production kinds and nothing else — true of a
bare ``import rheo_app_worker.main``.
"""

from collections.abc import Callable, Iterator
from importlib.metadata import EntryPoint

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from rheo_app_worker import main as worker_main
from rheo_app_worker.main import ADAPTERS, CONSUMERS, JOB_KINDS, register_modules
from rheo_core.audit import reset_sinks
from rheo_core.events.consumers import ConsumerSubscription
from rheo_core.exports import (
    EXPORT_JOB_KIND,
    RESTORE_JOB_KIND,
    ExportJobPayload,
    RestoreJobPayload,
    run_export_job,
    run_restore_job,
)
from rheo_core.modules import ENTRY_POINT_GROUP, JobKind, reset_surfaces
from rheo_core.modules import loader as loader_module
from rheo_core.modules.loader import ALLOWLIST_KEY
from rheo_core.runtime import RUNTIME_RUN, AdapterRegistry, RuntimeJobPayload
from rheo_core.settings import env_variable_names
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.kinds import JobKindUnknown
from rheo_core.work.schedules import (
    RETENTION_SWEEP,
    RetentionSweepPayload,
    run_retention_sweep,
)
from rheo_runtimes import ClaudeCliRuntime
from test_module_loader import _manifest

MODULES_VARIABLE = env_variable_names(ALLOWLIST_KEY)[0]

WORKER_MODULE_ID = "worker_probe"
WORKER_JOB_KIND = f"{WORKER_MODULE_ID}.sweep"
WORKER_CONSUMER_ID = f"{WORKER_MODULE_ID}.note.indexer"
WORKER_EVENT_TYPE = f"{WORKER_MODULE_ID}.note.created"


class _WorkerProbePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    since: int


def _worker_job_handler(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    """Never run: this file registers a job kind, it does not dispatch one."""
    return None


def _worker_consumer_handler(uow: HandlerUnitOfWork, envelope: object) -> None:
    """Never run: this file registers a subscription, it does not deliver one."""
    return None


WORKER_JOB = JobKind(
    name=WORKER_JOB_KIND,
    input_model=_WorkerProbePayload,
    handler=_worker_job_handler,
    max_attempts=2,
    cancellable=False,
)
WORKER_SUBSCRIPTION = ConsumerSubscription(
    consumer_id=WORKER_CONSUMER_ID,
    event_type=WORKER_EVENT_TYPE,
    module_id=WORKER_MODULE_ID,
    replay_safe=True,
    handler=_worker_consumer_handler,
)
WORKER_MANIFEST = _manifest(
    WORKER_MODULE_ID, jobs=(WORKER_JOB,), subscriptions=(WORKER_SUBSCRIPTION,)
)
"""A fixture module declaring only a job kind and a subscription.

No operation, tool or settings key, deliberately: ``register_modules()`` passes the
process-wide operation, resolver and tool registries, and this file has no business
writing into tables the rest of the session reads."""

WORKER_ENTRY_POINT = EntryPoint(
    name=WORKER_MODULE_ID,
    value=f"{__name__}:WORKER_MANIFEST",
    group=ENTRY_POINT_GROUP,
)


def test_production_job_kinds_include_core_runtime_run_and_retention_sweep() -> None:
    assert JOB_KINDS.names() == frozenset(
        {EXPORT_JOB_KIND, RESTORE_JOB_KIND, RUNTIME_RUN, RETENTION_SWEEP}
    )

    export_model, export_handler = JOB_KINDS.lookup(EXPORT_JOB_KIND)
    assert export_model is ExportJobPayload
    assert export_handler is run_export_job

    restore_model, restore_handler = JOB_KINDS.lookup(RESTORE_JOB_KIND)
    assert restore_model is RestoreJobPayload
    assert restore_handler is run_restore_job

    runtime_model, runtime_handler = JOB_KINDS.lookup(RUNTIME_RUN)
    assert runtime_model is RuntimeJobPayload
    assert runtime_handler is not None
    assert isinstance(ADAPTERS, AdapterRegistry)
    assert ADAPTERS.names() == frozenset({"claude_cli"})
    assert isinstance(ADAPTERS.lookup("claude_cli"), ClaudeCliRuntime)

    sweep_model, sweep_handler = JOB_KINDS.lookup(RETENTION_SWEEP)
    assert sweep_model is RetentionSweepPayload
    assert sweep_handler is run_retention_sweep


def test_retention_sweep_payload_requires_workspace_id() -> None:
    with pytest.raises(ValidationError):
        RetentionSweepPayload.model_validate({})


def test_production_job_kinds_still_refuse_an_unknown_kind() -> None:
    with pytest.raises(JobKindUnknown, match="core.missing"):
        JOB_KINDS.lookup("core.missing")


@pytest.fixture
def restored_composition_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Let a test write into the real ``JOB_KINDS``/``CONSUMERS`` and hand them back.

    Neither registry publishes an unregister, so the table each holds is swapped for
    a copy of itself for the duration and ``monkeypatch`` puts the original back.
    Swapping the *table* rather than the registry object is what makes this a test of
    the production instances: ``register_modules()`` reads the module-level names, so
    a substituted registry would prove only that the function passes whatever those
    names hold. Reading the private attribute is deliberate and scoped here, the way
    ``tests/test_production_registration.py`` reads ``ConsumerRegistry._consumers``
    for the same absent accessor; a rename raises here rather than silently leaking
    the fixture module into every later test in the session.
    """
    monkeypatch.setattr(JOB_KINDS, "_kinds", dict(JOB_KINDS._kinds))
    monkeypatch.setattr(CONSUMERS, "_consumers", dict(CONSUMERS._consumers))
    reset_surfaces()
    reset_sinks()
    yield
    reset_surfaces()
    reset_sinks()


def test_register_modules_puts_a_modules_job_kind_and_consumer_on_the_worker(
    monkeypatch: pytest.MonkeyPatch, restored_composition_root: None
) -> None:
    """The production call site for job-kind and subscription registration.

    ``load_modules()`` registers neither category unless a composition root hands it
    a registry, and ``apps/worker``'s ``main()`` is the only caller that does. So the
    assertion is made against ``JOB_KINDS`` and ``CONSUMERS`` themselves — the
    instances ``worker_loop`` is handed — rather than against a pair built here.
    """
    monkeypatch.setattr(
        loader_module, "entry_points", lambda group: (WORKER_ENTRY_POINT,)
    )
    monkeypatch.setenv(MODULES_VARIABLE, WORKER_MODULE_ID)

    assert WORKER_JOB_KIND not in JOB_KINDS.names()

    register_modules()

    assert WORKER_JOB_KIND in JOB_KINDS.names()
    input_model, handler = JOB_KINDS.lookup(WORKER_JOB_KIND)
    assert input_model is _WorkerProbePayload
    assert handler is _worker_job_handler
    # The four core kinds are still there: a module contributes, it does not replace.
    assert {EXPORT_JOB_KIND, RESTORE_JOB_KIND, RUNTIME_RUN, RETENTION_SWEEP} <= (
        JOB_KINDS.names()
    )

    assert CONSUMERS.lookup(WORKER_CONSUMER_ID) is WORKER_SUBSCRIPTION
    assert CONSUMERS.for_type(WORKER_EVENT_TYPE) == (WORKER_SUBSCRIPTION,)


def test_main_registers_modules_after_the_login_check_and_before_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production call site, in the order ``main()`` performs it.

    Extracting ``register_modules()`` is what lets this run without a cluster, and
    the order is the half that matters: modules load after the configuration refusal
    and before ``worker_loop`` is handed ``JOB_KINDS`` and ``CONSUMERS``, so the loop
    starts with a module's job kinds and subscriptions already in them. Without this
    case, ``register_modules()`` could be a function nothing in production calls and
    the test above it would stay green.
    """
    calls: list[str] = []

    def _record(name: str) -> Callable[..., object]:
        def _call(*args: object, **kwargs: object) -> object:
            calls.append(name)
            return None

        return _call

    for attribute, label in (
        ("refuse_misconfigured_login", "login"),
        ("register_modules", "register_modules"),
        ("get_backend", "get_backend"),
        ("install_stop_signals", "install_stop_signals"),
        ("worker_loop", "worker_loop"),
        ("reset_backend", "reset_backend"),
    ):
        monkeypatch.setattr(worker_main, attribute, _record(label))

    worker_main.main()

    assert calls == [
        "login",
        "register_modules",
        "get_backend",
        "install_stop_signals",
        "worker_loop",
        "reset_backend",
    ]
