"""Issue #123: two modules may not claim one web surface name.

``module_surfaces()`` keys by ``WebSurface.surface`` and keeps one entry per name,
while ``render()`` emits every contribution. Two modules sharing a surface name would
compose both sets of screens and route only one, so the generated
``modules.generated.ts`` and the runtime routing configuration would disagree.

One shared check, ``rheo_core.modules.check_web_surfaces``, runs from both paths:

- ``rheo web compose`` (``installed_manifests()`` and ``render()``), and the command
  itself prints the refusal as one ``module_invalid`` line with exit ``1``;
- ``load_modules()``, before anything registers, against the incoming set and against
  what this process loaded earlier.

The refusal names both module ids and the surface. The real installed set (Recallatron
alone) passes.
"""

from collections.abc import Iterator
from importlib.metadata import EntryPoint

import pytest
from harness.modules import install, manifest, publish
from rheo_app_cli.main import main
from rheo_app_cli.web_compose import installed_manifests, render
from rheo_core.modules import (
    ENTRY_POINT_GROUP,
    ManifestInvalid,
    WebContribution,
    WebRoute,
    WebSurface,
    check_web_surfaces,
    discovered,
    load_modules,
    loaded_manifests,
    module_surfaces,
    reset_surfaces,
)
from rheo_core.operations import OperationRegistry
from rheo_core.refs.resolver import ResolverRegistry
from rheo_core.tokens.sets import ToolRegistry

FIRST_ID = "first_surface_probe"
SECOND_ID = "second_surface_probe"
OTHER_ID = "other_surface_probe"
SHARED_SURFACE = "shared_ui"


def _web(surface: str, package: str, host: str, path: str) -> WebContribution:
    return WebContribution(
        surface=WebSurface(surface=surface, host=host, path=path),
        package_name=package,
        navigation=(),
        routes=(WebRoute(id="home", path="/", screen="home"),),
        record_views=(),
        forms=(),
        search_providers=(),
    )


FIRST_MANIFEST = manifest(
    FIRST_ID,
    web=_web(SHARED_SURFACE, "@rheo-stream/first-surface-probe-web", "first", "/first"),
)
SECOND_MANIFEST = manifest(
    SECOND_ID,
    web=_web(
        SHARED_SURFACE, "@rheo-stream/second-surface-probe-web", "second", "/second"
    ),
)
OTHER_MANIFEST = manifest(
    OTHER_ID,
    web=_web("other_ui", "@rheo-stream/other-surface-probe-web", "other", "/other"),
)
QUIET_MANIFEST = manifest("quiet_surface_probe")
"""No web contribution at all, so it claims no surface."""

EXPECTED_REFUSAL = (
    f"{SECOND_ID}: declares web surface {SHARED_SURFACE!r}, which module "
    f"{FIRST_ID!r} already declares; one surface name belongs to exactly one module"
)


def _entry_point(module_id: str, attribute: str) -> EntryPoint:
    return EntryPoint(
        name=module_id, value=f"{__name__}:{attribute}", group=ENTRY_POINT_GROUP
    )


FIRST_ENTRY_POINT = _entry_point(FIRST_ID, "FIRST_MANIFEST")
SECOND_ENTRY_POINT = _entry_point(SECOND_ID, "SECOND_MANIFEST")
OTHER_ENTRY_POINT = _entry_point(OTHER_ID, "OTHER_MANIFEST")


@pytest.fixture(autouse=True)
def clean_loaded() -> Iterator[None]:
    reset_surfaces()
    yield
    reset_surfaces()


def _load(
    monkeypatch: pytest.MonkeyPatch, *entry_points: EntryPoint
) -> OperationRegistry:
    publish(monkeypatch, *entry_points)
    install(monkeypatch, *(entry_point.name for entry_point in entry_points))
    operations = OperationRegistry()
    load_modules(
        registry=operations, resolvers=ResolverRegistry(), tools=ToolRegistry()
    )
    return operations


# --- the shared check -----------------------------------------------------------------


def test_the_check_refuses_a_shared_surface_naming_both_modules() -> None:
    with pytest.raises(ManifestInvalid) as excinfo:
        check_web_surfaces([FIRST_MANIFEST, SECOND_MANIFEST])
    assert str(excinfo.value) == EXPECTED_REFUSAL
    assert excinfo.value.module_id == SECOND_ID


def test_distinct_surfaces_and_modules_without_web_pass() -> None:
    check_web_surfaces([FIRST_MANIFEST, OTHER_MANIFEST, QUIET_MANIFEST])


# --- rheo web compose -----------------------------------------------------------------


def test_installed_manifests_refuses_a_shared_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish(monkeypatch, SECOND_ENTRY_POINT, FIRST_ENTRY_POINT)
    with pytest.raises(ManifestInvalid) as excinfo:
        installed_manifests()
    assert str(excinfo.value) == EXPECTED_REFUSAL


def test_render_refuses_a_shared_surface() -> None:
    with pytest.raises(ManifestInvalid) as excinfo:
        render([FIRST_MANIFEST, SECOND_MANIFEST])
    assert str(excinfo.value) == EXPECTED_REFUSAL


def test_the_compose_command_prints_the_refusal_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    publish(monkeypatch, FIRST_ENTRY_POINT, SECOND_ENTRY_POINT)
    capsys.readouterr()

    code = main(["web", "compose", "--out", "-"])

    out, err = capsys.readouterr()
    assert code == 1
    assert out == ""
    assert err == f"module_invalid: {EXPECTED_REFUSAL}\n"


def test_the_real_installed_set_composes() -> None:
    """Recallatron is the one shipped module with a web contribution; the check must
    admit the environment's own installed set."""
    assert "recallatron" in {entry.name for entry in discovered()}
    manifests = installed_manifests()
    check_web_surfaces(manifests)
    assert "recallatron" in {m.module_id for m in manifests if m.web is not None}


# --- load_modules ---------------------------------------------------------------------


def test_load_modules_refuses_a_shared_surface_before_registering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ManifestInvalid) as excinfo:
        _load(monkeypatch, FIRST_ENTRY_POINT, SECOND_ENTRY_POINT)
    assert str(excinfo.value) == EXPECTED_REFUSAL
    assert loaded_manifests() == {}
    assert module_surfaces() == {}


def test_load_modules_refuses_a_surface_an_earlier_load_already_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``module_surfaces()`` reads the accumulated loaded table, so a second load in
    the same process must not take a surface the first one registered."""
    _load(monkeypatch, FIRST_ENTRY_POINT)
    assert set(module_surfaces()) == {SHARED_SURFACE}

    with pytest.raises(ManifestInvalid) as excinfo:
        _load(monkeypatch, SECOND_ENTRY_POINT)

    assert str(excinfo.value) == EXPECTED_REFUSAL
    assert set(loaded_manifests()) == {FIRST_ID}


def test_reloading_the_same_module_is_not_a_collision_with_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _load(monkeypatch, FIRST_ENTRY_POINT)
    _load(monkeypatch, FIRST_ENTRY_POINT)
    assert set(loaded_manifests()) == {FIRST_ID}


def test_load_modules_admits_distinct_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    _load(monkeypatch, FIRST_ENTRY_POINT, OTHER_ENTRY_POINT)
    assert set(module_surfaces()) == {SHARED_SURFACE, "other_ui"}
