"""AC 2 and AC 11, Python half: criterion 24's interface contributions, end to end.

Seams under test: the committed ``apps/web/src/modules.generated.ts`` against a fresh
``web_compose.render()`` over every discovered manifest, and ``core.workspace.status``
plus ``routing_config()`` for a workspace that enabled Recallatron and for one that
never installed it. The TypeScript half is ``apps/web``'s vitest suite, which runs
``composeNavigation`` and ``findModuleRoute`` over the real ``MODULES`` and the same two
``tests/fixtures/web/workspace-status-{enabled,absent}.json`` files this module reads.
Both halves are held against those fixtures, so they agree by construction rather
than by two claims that happen to match today.

**Two different claims, and they are tested apart.**

1. The generated file is one build artifact for the whole deployment. ``rheo web
   compose`` reads every *discovered* distribution and never consults
   ``modules.installed`` or any workspace's enabled set, so the file is byte-identical
   whichever workspace a request comes from. It is checked once, with no workspace.
2. Whether a workspace *sees* a contribution is decided at request time: the shell
   reads ``core.workspace.status`` and the routing configuration and filters
   ``MODULES`` through ``composeNavigation``. What that runtime filter is fed is what
   the enabled/absent pair pins here. The absent case says nothing about the
   generated file, because the file is the same in both cases.

**Nothing here names the module's Python package.** The module is reached through its
``rheo.modules`` entry point, by the id ``recallatron``, the same way the loader and
``rheo web compose`` reach it, so ``tests/test_module_import_graph.py`` has nothing to
declare for this file.
"""

import json
import re
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.modules import install_and_enable_module, loaded_probe_modules
from rheo_app_cli.web_compose import installed_manifests, render
from rheo_app_core.internal_routes import routing_config
from rheo_contracts import SafetyClass, WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.operations import (
    WORKSPACE_STATUS,
    WorkspaceStatus,
    dispatch,
    register_core_operations,
)

pytestmark = pytest.mark.postgres

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
_GENERATED: Final = _REPO_ROOT / "apps" / "web" / "src" / "modules.generated.ts"
_FIXTURES: Final = _REPO_ROOT / "tests" / "fixtures" / "web"

RECALLATRON: Final = "recallatron"
RECALL_OPERATION: Final = "recallatron.memory.recall"

# Written out rather than read off the manifest: a pin derived from the manifest would
# agree with whatever the manifest declares, including a dropped entry.
_NAVIGATION_IDS: Final = ["memory", "search", "duplicates"]
_ROUTE_SCREENS: Final = [
    ("browse", "browse"),
    ("search", "search"),
    ("item", "item"),
    ("duplicates", "duplicates"),
]
_RECALL_PROVIDER_LINE: Final = f'{{ id: "recall", operation: "{RECALL_OPERATION}" }}'
_ROUTE_LINE: Final = re.compile(
    r'\{ id: "([^"]+)", path: "[^"]*", '
    r"screen: recallatronWeb\.screens\.(\w+) \}"
)
_NAVIGATION_LINE: Final = re.compile(r'\{ id: "([^"]+)", label: ')
_MODULE_BLOCK: Final = re.compile(
    r'^  \{\n    id: "(?P<id>[^"]+)",\n.*?^  \},$', re.DOTALL | re.MULTILINE
)
"""One ``MODULES`` entry, from its opening brace to its closing ``},`` at the same
indent. ``render()`` writes every entry this way (``web_compose._module``)."""


@pytest.fixture(autouse=True)
def registrations() -> None:
    """The shipped core operations: install, enable and status all live there."""
    register_core_operations()


def _status(workspace: UUID) -> WorkspaceStatus:
    """``core.workspace.status`` under a context built now, never a reused one."""
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext), ctx
    outcome = dispatch(ctx, WORKSPACE_STATUS, {})
    assert outcome.ok, outcome
    assert isinstance(outcome.result, WorkspaceStatus), outcome.result
    return outcome.result


def _pairs(modules: object) -> list[tuple[str, str]]:
    """``(module_id, state)`` for each entry, the shape both halves compare on."""
    assert isinstance(modules, list), modules
    pairs: list[tuple[str, str]] = []
    for entry in modules:
        assert isinstance(entry, dict), entry
        pairs.append((str(entry["module_id"]), str(entry["state"])))
    return pairs


def _fixture_pairs(name: str) -> list[tuple[str, str]]:
    raw = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(raw, dict), name
    return _pairs(raw["modules"])


def _live_pairs(status: WorkspaceStatus) -> list[tuple[str, str]]:
    return _pairs([module.model_dump(mode="json") for module in status.modules])


def _module_block(text: str, module_id: str) -> str:
    """``module_id``'s own ``MODULES`` entry, so a check of its contents cannot be
    satisfied, or broken, by another discovered module's entries beside it."""
    blocks = [
        match.group(0)
        for match in _MODULE_BLOCK.finditer(text)
        if match.group("id") == module_id
    ]
    assert len(blocks) == 1, f"{module_id!r} has {len(blocks)} MODULES entries"
    return blocks[0]


# --- 1. the generated file: one artifact, checked once --------------------------------


def test_the_committed_composition_is_what_compose_renders() -> None:
    """Byte-equal, so a hand edit, a stale file or a module added without re-running
    ``rheo web compose`` all red here. ``make codegen`` in CI proves the same file is
    reproducible; this proves the committed bytes are the ones the Python side renders
    from the manifests this checkout actually publishes."""
    manifests = installed_manifests()
    assert RECALLATRON in {manifest.module_id for manifest in manifests}

    rendered = render(manifests)
    committed = _GENERATED.read_bytes()

    assert committed == rendered.encode("utf-8")


def test_the_composition_carries_recallatron_navigation_routes_and_recall() -> None:
    """What the committed file composes for Recallatron, read off Recallatron's own
    entry in it: every other discovered module's entry is ignored, so a second
    module's navigation cannot change the comparison either way."""
    text = _GENERATED.read_text(encoding="utf-8")
    assert 'import * as recallatronWeb from "@rheo-stream/recallatron-web";' in text

    block = _module_block(text, RECALLATRON)
    assert f'surface: "{RECALLATRON}",' in block
    assert _NAVIGATION_LINE.findall(block) == _NAVIGATION_IDS
    assert _ROUTE_LINE.findall(block) == _ROUTE_SCREENS
    assert _RECALL_PROVIDER_LINE in block


def test_the_recall_provider_names_a_registered_read_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 11's Python half: the ``recall`` provider is composed from the manifest,
    whether or not any screen renders a global search box.

    The generated file carries the provider line (the byte check above makes that the
    committed file and not a local rendering), and the operation it names is one the
    module really registers when loaded, as a ``read``.
    ``tests/postgres/test_memory_browse.py::test_recallatron_registers_exactly_the_1a2_set_plus_get_and_coverage``
    pins the module's whole operation inventory; this pins the one entry the provider
    depends on, so a renamed recall operation reds here rather than in a browser.
    """
    assert _RECALL_PROVIDER_LINE in _GENERATED.read_text(encoding="utf-8")

    with loaded_probe_modules(monkeypatch, RECALLATRON) as loaded:
        registered = loaded.operations.lookup(RECALL_OPERATION)

    assert registered is not None, sorted(loaded.operations.names())
    assert registered.declaration.safety_class is SafetyClass.READ


# --- 2. per-workspace visibility: the enabled/absent pair -----------------------------


def test_enabling_recallatron_in_a_fresh_workspace_reports_it_enabled_and_routed(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
) -> None:
    """What the shell's runtime filter reads for a workspace that enabled the module:
    ``core.workspace.status`` names it ``enabled``, and the routing configuration the
    internal listener serves carries its surface. Both are the inputs
    ``composeNavigation`` needs to show its entries.

    The status is read before the enable as well as after, so "reported enabled" is a
    change this test caused and not something the fresh workspace already carried.
    """
    assert _live_pairs(_status(workspace)) == []

    with loaded_probe_modules(monkeypatch, RECALLATRON):
        ctx = context_for_operator(workspace)
        assert isinstance(ctx, WorkspaceContext), ctx
        install_and_enable_module(cluster.backend, ctx, workspace, RECALLATRON)

        routing = routing_config()

        after = context_for_operator(workspace)
        assert isinstance(after, WorkspaceContext), after
        assert RECALLATRON in after.enabled_modules
        status = _status(workspace)

    surfaces = routing["surfaces"]
    assert isinstance(surfaces, dict), routing
    modules = surfaces["modules"]
    assert set(modules) == {RECALLATRON}, modules
    assert modules[RECALLATRON]["host"] == "recallatron"
    assert modules[RECALLATRON]["path"] == "/recallatron"

    live = _live_pairs(status)
    assert live == [(RECALLATRON, "enabled")]
    assert live == _fixture_pairs("workspace-status-enabled.json")


def test_a_never_installed_workspace_reports_recallatron_absent(
    monkeypatch: pytest.MonkeyPatch,
    make_workspace: MakeWorkspace,
) -> None:
    """The companion: a workspace that never installed the module reports no module
    at all, which is what makes ``composeNavigation`` show none of it.

    Recallatron is **loaded in this deployment** for the read, so the absence is this
    workspace's own state and not a side effect of the module being missing from the
    process. Nothing about ``modules.generated.ts`` is asserted: it is identical here
    and in the enabled case, and visibility is the runtime filter's decision.
    """
    never_installed = make_workspace()

    with loaded_probe_modules(monkeypatch, RECALLATRON):
        ctx = context_for_operator(never_installed)
        assert isinstance(ctx, WorkspaceContext), ctx
        assert RECALLATRON not in ctx.enabled_modules
        status = _status(never_installed)

    live = _live_pairs(status)
    assert live == []
    assert live == _fixture_pairs("workspace-status-absent.json")
