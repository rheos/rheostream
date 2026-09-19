"""FR 5 / AC 8: on disk, loaded by this deployment, installed in a workspace — three
sets, three names, and no single function answering for more than one of them.

Seams under test, each called directly rather than through a shared private cache
that could silently conflate them:

- ``discovered()`` — what is host-installed and importable (set (a)).
- ``allowed_module_ids()`` / ``loaded_manifests()`` — what this deployment actually
  loaded, gated by the ``modules.installed`` setting (set (b)).
- ``list_module_states()`` — what one workspace has installed and enabled, read from
  that workspace's own ``core.module_state`` rows (set (c)).

The property is that each answer stands alone: a module on disk is not thereby
loaded, and a module loaded by this deployment is not thereby installed in any given
workspace. A function that started answering two of these would pass whichever test
asked only one question, so both cases below ask their question against a live
counter-example.

**The entry point is fabricated, and the manifest builder is borrowed.** Nothing in
the checkout publishes a real ``rheo.modules`` entry point yet, so discovery is driven
the one way ``tests/test_module_loader.py`` already ships: a real ``EntryPoint`` whose
value points back at a module-level name here, published by monkeypatching the
loader's own ``entry_points``. The twenty-four-field manifest builder is imported from
that file rather than written a second time; it fills the eighteen fields neither file
varies. The ids are ``_probe``-suffixed so a fixture can never borrow the id of a
distribution shipped later.
"""

from collections.abc import Iterator
from importlib.metadata import EntryPoint
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.registry import enable_harness_module
from rheo_core.audit import reset_sinks
from rheo_core.modules import (
    ENTRY_POINT_GROUP,
    discovered,
    load_modules,
    loaded_manifests,
    reset_surfaces,
)
from rheo_core.modules import loader as loader_module
from rheo_core.modules.loader import ALLOWLIST_KEY
from rheo_core.operations import HARNESS_MODULE_ID, OperationRegistry
from rheo_core.refs.resolver import ResolverRegistry
from rheo_core.settings import env_variable_names
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.repositories import list_module_states
from test_module_loader import _manifest

MODULES_VARIABLE = env_variable_names(ALLOWLIST_KEY)[0]

ON_DISK_ID = "on_disk_probe"
ON_DISK_MANIFEST = _manifest(ON_DISK_ID)
ON_DISK_ENTRY_POINT = EntryPoint(
    name=ON_DISK_ID,
    value=f"{__name__}:ON_DISK_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

LOADED_ID = "loaded_probe"
LOADED_MANIFEST = _manifest(LOADED_ID)
LOADED_ENTRY_POINT = EntryPoint(
    name=LOADED_ID,
    value=f"{__name__}:LOADED_MANIFEST",
    group=ENTRY_POINT_GROUP,
)


@pytest.fixture(autouse=True)
def clean_process_state() -> Iterator[None]:
    """The loader's tables are process-wide. Empty them either side."""
    reset_surfaces()
    reset_sinks()
    yield
    reset_surfaces()
    reset_sinks()


@pytest.fixture
def registries() -> tuple[OperationRegistry, ResolverRegistry]:
    """Local registries: the process-wide ones are never touched by this file."""
    return OperationRegistry(), ResolverRegistry()


def _publish(monkeypatch: pytest.MonkeyPatch, *entry_points: EntryPoint) -> None:
    """Make ``discovered()`` see exactly these, and nothing the environment has."""
    monkeypatch.setattr(
        loader_module, "entry_points", lambda group: tuple(entry_points)
    )


def _install(monkeypatch: pytest.MonkeyPatch, *module_ids: str) -> None:
    """Resolve ``modules.installed`` to exactly ``module_ids`` for one test."""
    if module_ids:
        monkeypatch.setenv(MODULES_VARIABLE, ",".join(module_ids))
    else:
        monkeypatch.delenv(MODULES_VARIABLE, raising=False)


# --- (a) on disk is not (b) loaded ----------------------------------------------------


def test_a_module_on_disk_is_not_loaded_unless_the_setting_names_it(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """``discovered()`` finds it; ``load_modules()`` does not load it.

    The counter-example is the same module in the same process: naming it in
    ``modules.installed`` loads it, so the empty result above is the setting's doing
    and not a loader that never loads anything. Without that second half, a
    ``load_modules()`` stubbed to return nothing would satisfy the first.
    """
    registry, resolvers = registries
    _publish(monkeypatch, ON_DISK_ENTRY_POINT)
    _install(monkeypatch)

    # (a) answers yes ...
    assert [entry.name for entry in discovered()] == [ON_DISK_ID]
    # ... and (b) answers no, on its own.
    assert load_modules(registry=registry, resolvers=resolvers) == ()
    assert loaded_manifests() == {}
    assert registry.names() == frozenset()

    _install(monkeypatch, ON_DISK_ID)
    assert load_modules(registry=registry, resolvers=resolvers) == (ON_DISK_ID,)
    assert set(loaded_manifests()) == {ON_DISK_ID}
    # Still on disk, and still found by the set that answers that question: loading
    # is a second fact about it, not a replacement for the first.
    assert [entry.name for entry in discovered()] == [ON_DISK_ID]


# --- (b) loaded is not (c) installed in a workspace -----------------------------------


@pytest.mark.postgres
def test_a_module_loaded_by_this_deployment_has_no_workspace_row(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
    cluster: ClusterSession,
    workspace: UUID,
) -> None:
    """``loaded_manifests()`` holds it; the workspace's ``core.module_state`` does not.

    **Paired with a positive control over the same call and the same workspace**, and
    it has to be: a ``list_module_states()`` that returned nothing at all would
    satisfy "no row for the loaded module" without knowing anything. So a row for a
    *different* module id is written first, through ``enable_harness_module``, and
    the same call is shown returning that one. An empty answer is only evidence when
    the same question, asked of the same workspace, is shown to have an answer.
    """
    registry, resolvers = registries
    _publish(monkeypatch, LOADED_ENTRY_POINT)
    _install(monkeypatch, LOADED_ID)

    assert load_modules(registry=registry, resolvers=resolvers) == (LOADED_ID,)
    assert set(loaded_manifests()) == {LOADED_ID}

    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        enable_harness_module(uow.connection)
        installed = {state.module_id for state in list_module_states(uow.connection)}

    # The control: (c) does answer, for a module this deployment never loaded.
    assert HARNESS_MODULE_ID in installed
    assert HARNESS_MODULE_ID not in loaded_manifests()
    # The property: being loaded by this deployment put no row in this workspace.
    assert LOADED_ID not in installed
