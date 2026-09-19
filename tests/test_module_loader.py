"""FR 6 / AC 8: nothing loads unless ``modules.installed`` names it, and what does load
is registered through the shipped registries.

Seams under test: ``allowed_module_ids()``'s read of the deployment-scope settings key
``modules.installed``; ``load_modules()``'s allowlist (only a named entry point loads,
and a named one really does register its operations, its resolvers and its audit
sink); and ``module_surfaces()``'s bookkeeping — populated only for a loaded manifest
that declares a ``web`` surface, keyed by that surface's own name rather than by the
module id.

The key is set here the way a deployment sets it: through the deployment layer's own
environment spelling, ``RHEO__modules__installed``, derived from the key rather than
written out, so these tests follow the shipped mapping instead of restating it.

**The entry point is fabricated, and that is not a shortcut.** Nothing in the
checkout publishes a real ``rheo.modules`` entry point: the four ``modules/*``
distributions are empty placeholders (run 0v's finding F17 records the same fact).
Driving the negative direction against that reality would pass **vacuously**,
because nothing is discoverable to be refused, and the positive direction would have
nothing to name at all. So ``discovered()``'s own ``entry_points`` call is
monkeypatched to yield a real ``EntryPoint`` whose value points back at this test
module.

The fabricated ids are ``_probe``-suffixed rather than plausible product names,
because a fixture that borrowed the id of a distribution shipped later would collide
with it. ``test_the_fixture_id_cannot_collide_with_a_real_distribution`` pins that
against the real, unmonkeypatched environment, so the guard survives the first real
module rather than depending on nobody noticing.

The three helpers that build, publish and allow a fixture module live in
``tests/harness/modules.py`` — they were duplicated across four files — and are
imported below under the private names their call sites here already used.
"""

import os
from collections.abc import Iterator
from importlib.metadata import EntryPoint

import pytest
from harness.modules import MODULES_VARIABLE, MODULES_VARIABLE_UPPER
from harness.modules import install as _install
from harness.modules import manifest as _manifest
from harness.modules import publish as _publish
from pydantic import BaseModel, ConfigDict, ValidationError
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.audit import reset_sinks, sink_for
from rheo_core.modules import (
    ENTRY_POINT_GROUP,
    ManifestInvalid,
    ModuleManifest,
    StorageDeclaration,
    WebSurface,
    allowed_module_ids,
    discovered,
    load_modules,
    module_surfaces,
    reset_surfaces,
)
from rheo_core.modules.loader import ALLOWLIST_KEY
from rheo_core.operations import OperationRegistry
from rheo_core.refs.resolver import (
    NOT_FOUND,
    RecordHead,
    ResolverRegistry,
    Unavailable,
)
from rheo_core.storage.backend import UnitOfWork

MODULE_ID = "fixture_probe"
OPERATION = f"{MODULE_ID}.note.add"
RECORD_TYPE = "note"


# --- the fabricated module ------------------------------------------------------------


class _ProbeInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    body: str


class _ProbeOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    body: str


def _handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: _ProbeInput
) -> _ProbeOutput:
    return _ProbeOutput(body=model_input.body)


def _resolver(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> RecordHead | Unavailable:
    return Unavailable(ref.format(), NOT_FOUND)


class _ProbeSink:
    def record(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        operation: str,
        subject_ref: RecordRef | None,
    ) -> None:
        return None


DECLARATION = OperationDeclaration(
    name=OPERATION,
    safety_class=SafetyClass.MUTATE,
    roles=frozenset({Role.OWNER}),
    input_model=_ProbeInput,
    output=_ProbeOutput,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)

SINK = _ProbeSink()


MANIFEST = _manifest(
    MODULE_ID,
    operations=((DECLARATION, _handler),),
    resolvers=((RECORD_TYPE, _resolver),),
    web=WebSurface(surface=MODULE_ID, host=MODULE_ID, path="/fixture-probe"),
    audit_sink=SINK,
)
"""The object the fabricated entry point loads to. ``ENTRY_POINT`` points at this
name by module path, so ``EntryPoint.load()`` runs for real rather than being stubbed
out alongside the discovery it is meant to exercise."""

ENTRY_POINT = EntryPoint(
    name=MODULE_ID, value=f"{__name__}:MANIFEST", group=ENTRY_POINT_GROUP
)

SURFACE_ONLY_ID = "surface_probe"
SURFACE_ONLY_MANIFEST = _manifest(
    SURFACE_ONLY_ID,
    # A surface whose name is NOT the module id: ``module_surfaces()`` is keyed by
    # the surface, and the two are not required to agree.
    web=WebSurface(surface="probe_ui", host="probe", path="/probe"),
)
SURFACE_ONLY_ENTRY_POINT = EntryPoint(
    name=SURFACE_ONLY_ID,
    value=f"{__name__}:SURFACE_ONLY_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

NO_SURFACE_ID = "quiet_probe"
NO_SURFACE_MANIFEST = _manifest(NO_SURFACE_ID)
NO_SURFACE_ENTRY_POINT = EntryPoint(
    name=NO_SURFACE_ID,
    value=f"{__name__}:NO_SURFACE_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

IMPOSTOR_ID = "impostor_probe"
NOT_A_MANIFEST = object()
"""What the impostor entry point below loads to: a real, importable module-level
object that is simply not a ``ModuleManifest``. The entry point resolves for real,
so the refusal is the loader's rather than an import error's."""

IMPOSTOR_ENTRY_POINT = EntryPoint(
    name=IMPOSTOR_ID,
    value=f"{__name__}:NOT_A_MANIFEST",
    group=ENTRY_POINT_GROUP,
)


# --- fixtures -------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_process_state() -> Iterator[None]:
    """Both tables the loader writes are process-wide. Empty them either side."""
    reset_surfaces()
    reset_sinks()
    yield
    reset_surfaces()
    reset_sinks()


@pytest.fixture
def registries() -> tuple[OperationRegistry, ResolverRegistry]:
    """Local registries: the process-wide ones are never touched by this file."""
    return OperationRegistry(), ResolverRegistry()


# --- the allowlist's read -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, frozenset()),
        ("", frozenset()),
        ("   ", frozenset()),
        (",", frozenset()),
        ("surface_probe", frozenset({"surface_probe"})),
        (
            " surface_probe , fixture_probe ",
            frozenset({"surface_probe", "fixture_probe"}),
        ),
        ("surface_probe,,surface_probe", frozenset({"surface_probe"})),
    ],
)
def test_allowed_module_ids_reads_the_setting(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: frozenset[str]
) -> None:
    """The key's declared ``list[str]`` coercion, read back through the loader.

    Unset, empty and all-whitespace all resolve to the empty set — the package
    default and the two ways an operator can write "none" — and a duplicate or a
    stray comma collapses, because the loader answers with a ``frozenset``.
    """
    if value is None:
        monkeypatch.delenv(MODULES_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(MODULES_VARIABLE, value)
    assert allowed_module_ids() == expected


# --- AC 8, both directions ------------------------------------------------------------


def test_no_module_loads_unless_it_is_named(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """A discoverable module, an unset key, and nothing registers.

    This is the configuration ``make demo`` and every image ``make build`` produces
    run in: ``Dockerfile`` COPYs ``modules/`` and ``uv sync --frozen`` installs each
    distribution, so discovery finds them there exactly as it finds this fixture here.
    """
    registry, resolvers = registries
    _publish(monkeypatch, ENTRY_POINT)
    _install(monkeypatch)

    assert discovered() == (ENTRY_POINT,)
    assert load_modules(registry=registry, resolvers=resolvers) == ()

    assert registry.names() == frozenset()
    assert resolvers.record_types() == frozenset()
    assert module_surfaces() == {}
    assert sink_for(MODULE_ID) is None


def test_a_named_module_registers_its_operations(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    registry, resolvers = registries
    _publish(monkeypatch, ENTRY_POINT)
    _install(monkeypatch, MODULE_ID)

    assert load_modules(registry=registry, resolvers=resolvers) == (MODULE_ID,)

    assert registry.names() == frozenset({OPERATION})
    registered = registry.lookup(OPERATION)
    assert registered is not None
    # Registered through the shipped path, with the manifest's own id as the origin —
    # so every registration check a hand-written registration faces applied here too.
    assert registered.origin == MODULE_ID
    assert registered.module_id == MODULE_ID
    assert resolvers.record_types() == frozenset({f"{MODULE_ID}.{RECORD_TYPE}"})
    assert resolvers.lookup(MODULE_ID, RECORD_TYPE) is _resolver
    # The sink lands under the manifest's own module id, and never under ``core``.
    assert sink_for(MODULE_ID) is SINK
    assert sink_for("core") is None


def test_only_the_named_module_loads_when_two_are_discoverable(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """The allowlist is a filter, not a switch: one discoverable module loading does
    not carry its neighbour in."""
    registry, resolvers = registries
    _publish(monkeypatch, ENTRY_POINT, NO_SURFACE_ENTRY_POINT)
    _install(monkeypatch, MODULE_ID)

    assert load_modules(registry=registry, resolvers=resolvers) == (MODULE_ID,)
    assert registry.names() == frozenset({OPERATION})


def test_a_named_module_that_is_not_installed_is_simply_absent(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """The allowlist says what *may* load. Refusing startup over a name nothing
    provides would make a deployment's own setting a dependency on its image's build
    context, which is the coupling finding F17 is about."""
    registry, resolvers = registries
    _publish(monkeypatch)
    _install(monkeypatch, MODULE_ID)

    assert load_modules(registry=registry, resolvers=resolvers) == ()


def test_load_modules_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """``apps/core``'s FastAPI lifespan runs more than once in one process under test,
    so ``run_startup()`` -> ``load_modules()`` does too. A refuse-on-second rule in
    either registry or in ``install_sink`` would crash the second run."""
    registry, resolvers = registries
    _publish(monkeypatch, ENTRY_POINT)
    _install(monkeypatch, MODULE_ID)

    first = load_modules(registry=registry, resolvers=resolvers)
    second = load_modules(registry=registry, resolvers=resolvers)

    assert first == second == (MODULE_ID,)
    assert registry.names() == frozenset({OPERATION})
    assert sink_for(MODULE_ID) is SINK


def test_the_explicit_allow_argument_overrides_the_setting(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    registry, resolvers = registries
    _publish(monkeypatch, ENTRY_POINT)
    _install(monkeypatch, MODULE_ID)

    assert load_modules(registry=registry, resolvers=resolvers, allow=frozenset()) == ()
    assert registry.names() == frozenset()


# --- module_surfaces() ----------------------------------------------------------------


def test_module_surfaces_is_empty_before_anything_loads() -> None:
    assert module_surfaces() == {}


def test_a_loaded_manifest_records_its_surface_keyed_by_the_surface_name(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """D-6's producer half, and the mutant it kills: keying by ``module_id``.

    ``SURFACE_ONLY_MANIFEST``'s surface is ``probe_ui`` while its module id is
    ``surface_probe``, so a loader that keyed by the module id would put the entry
    under the wrong name and C3's ``routing_config()`` would not find it.
    """
    registry, resolvers = registries
    _publish(monkeypatch, SURFACE_ONLY_ENTRY_POINT)
    _install(monkeypatch, SURFACE_ONLY_ID)

    assert load_modules(registry=registry, resolvers=resolvers) == (SURFACE_ONLY_ID,)

    surfaces = module_surfaces()
    assert set(surfaces) == {"probe_ui"}
    assert surfaces["probe_ui"] == WebSurface(
        surface="probe_ui", host="probe", path="/probe"
    )
    assert SURFACE_ONLY_ID not in surfaces


def test_a_loaded_manifest_without_a_surface_records_none(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    registry, resolvers = registries
    _publish(monkeypatch, NO_SURFACE_ENTRY_POINT)
    _install(monkeypatch, NO_SURFACE_ID)

    assert load_modules(registry=registry, resolvers=resolvers) == (NO_SURFACE_ID,)
    assert module_surfaces() == {}


def test_module_surfaces_hands_back_a_copy(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """Read-only in the way that matters: a caller cannot edit the loader's table."""
    registry, resolvers = registries
    _publish(monkeypatch, ENTRY_POINT)
    _install(monkeypatch, MODULE_ID)
    load_modules(registry=registry, resolvers=resolvers)

    surfaces = dict(module_surfaces())
    surfaces.clear()
    assert set(module_surfaces()) == {MODULE_ID}


# --- manifest validation --------------------------------------------------------------


def test_a_manifest_published_under_a_different_name_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """The allowlist gates the entry-point *name*, so the name and the manifest's own
    module id must agree — otherwise one name would be authorized and a different one
    would register."""
    registry, resolvers = registries
    mislabelled = EntryPoint(
        name="something_else", value=f"{__name__}:MANIFEST", group=ENTRY_POINT_GROUP
    )
    _publish(monkeypatch, mislabelled)
    _install(monkeypatch, "something_else")

    with pytest.raises(ManifestInvalid) as excinfo:
        load_modules(registry=registry, resolvers=resolvers)
    assert excinfo.value.module_id == MODULE_ID
    assert registry.names() == frozenset()


def test_a_schema_that_is_not_the_module_id_is_refused() -> None:
    """The rule the deleted ``validate()`` carried, now the model's own: one module
    owns one schema, named for it."""
    with pytest.raises(ValidationError) as excinfo:
        _manifest(
            MODULE_ID,
            storage=StorageDeclaration(
                schema_name="somewhere_else",
                migrations_path="modules/fixture_probe/migrations",
                required_extensions=(),
            ),
        )
    assert "somewhere_else" in str(excinfo.value)


def test_an_entry_point_that_is_not_a_manifest_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """Asserted at the loader's public seam rather than against the private
    ``_as_manifest``: what a deployment actually does is publish an entry point and
    start, so that is the direction the refusal has to hold in."""
    registry, resolvers = registries
    _publish(monkeypatch, IMPOSTOR_ENTRY_POINT)
    _install(monkeypatch, IMPOSTOR_ID)

    with pytest.raises(ManifestInvalid):
        load_modules(registry=registry, resolvers=resolvers)
    assert registry.names() == frozenset()


def test_the_fixture_manifest_is_a_manifest_and_loads(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """The positive case the deleted ``test_validate_accepts_the_fixture`` held:
    a manifest this file constructs is accepted and loads under its own id."""
    registry, resolvers = registries
    assert isinstance(MANIFEST, ModuleManifest)
    _publish(monkeypatch, ENTRY_POINT)
    _install(monkeypatch, MODULE_ID)

    assert load_modules(registry=registry, resolvers=resolvers) == (MODULE_ID,)


# --- the fixture's own guard ----------------------------------------------------------


def test_the_fixture_id_cannot_collide_with_a_real_distribution() -> None:
    """The first real module distribution will publish under the same entry-point
    group as these fixtures.

    This walks the *real* environment, unmonkeypatched, and asserts only that nothing
    claims the fabricated ids — not that the group is empty, which would go red the
    moment a real module is installed.
    """
    fabricated = {MODULE_ID, SURFACE_ONLY_ID, NO_SURFACE_ID, IMPOSTOR_ID}
    assert fabricated.isdisjoint({entry.name for entry in discovered()})


def test_the_allowlist_setting_is_not_set_by_the_suite() -> None:
    """``modules.installed`` is deployment-scope, so there is no process variable a
    developer's shell can leak into a *workspace*'s answer — but the deployment layer
    still reads one, and an exported ``RHEO__modules__installed`` would resolve the
    key for the whole session.

    The negative directions above would still pass (each removes the variable for its
    own test) while every other file ran with a module loaded, so this fails loudly
    instead. Both spellings the deployment layer accepts are checked, because it tries
    the exact form first and then the fully-uppercased one.
    """
    for name in (MODULES_VARIABLE, MODULES_VARIABLE_UPPER):
        assert os.environ.get(name) in (None, ""), (
            f"{name} is set in this process; the suite must resolve "
            f"{ALLOWLIST_KEY} to its empty package default"
        )
