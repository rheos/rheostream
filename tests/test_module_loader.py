"""FR 6 / AC 8: nothing loads unless ``RHEO_MODULES`` names it, and what does load is
registered through the shipped registries.

Seams under test: ``allowed_module_ids()``'s parse of the process variable;
``load_modules()``'s allowlist (only a named entry point loads, and a named one really
does register its operations, its resolvers and its audit sink); and
``module_surfaces()``'s bookkeeping — populated only for a loaded manifest that
declares a ``web`` surface, keyed by that surface's own name rather than by the
module id.

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
"""

import os
from collections.abc import Iterator
from importlib.metadata import EntryPoint

import pytest
from pydantic import BaseModel, ConfigDict
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.audit import NULL_SINK, reset_sinks, sink_for
from rheo_core.modules import (
    ALLOWLIST_VARIABLE,
    ENTRY_POINT_GROUP,
    ManifestInvalid,
    ModuleManifest,
    WebSurface,
    allowed_module_ids,
    discovered,
    load_modules,
    module_surfaces,
    reset_surfaces,
    validate,
)
from rheo_core.modules import loader as loader_module
from rheo_core.operations import OperationRegistry
from rheo_core.refs.resolver import (
    NOT_FOUND,
    RecordHead,
    ResolverRegistry,
    Unavailable,
)
from rheo_core.storage.backend import UnitOfWork
from sqlalchemy import Connection

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


def _create_schema(connection: Connection) -> None:
    """Never called: no test here provisions the fabricated module's schema."""


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

MANIFEST = ModuleManifest(
    module_id=MODULE_ID,
    package_version="0.0.0",
    schema_name=MODULE_ID,
    create_schema=_create_schema,
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
SURFACE_ONLY_MANIFEST = ModuleManifest(
    module_id=SURFACE_ONLY_ID,
    package_version="0.0.0",
    schema_name=SURFACE_ONLY_ID,
    create_schema=_create_schema,
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
NO_SURFACE_MANIFEST = ModuleManifest(
    module_id=NO_SURFACE_ID,
    package_version="0.0.0",
    schema_name=NO_SURFACE_ID,
    create_schema=_create_schema,
)
NO_SURFACE_ENTRY_POINT = EntryPoint(
    name=NO_SURFACE_ID,
    value=f"{__name__}:NO_SURFACE_MANIFEST",
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


def _publish(monkeypatch: pytest.MonkeyPatch, *entry_points: EntryPoint) -> None:
    """Make ``discovered()`` see exactly these, and nothing the environment has."""
    monkeypatch.setattr(
        loader_module, "entry_points", lambda group: tuple(entry_points)
    )


# --- the allowlist's parse ------------------------------------------------------------


def test_the_allowlist_variable_is_not_a_settings_override() -> None:
    """One underscore, deliberately. ``rheo_core.settings.deployment`` raises
    ``SettingUndeclared`` for any ``RHEO__``-prefixed variable no schema declares, and
    AC 18 forbids adding that key — so the name is load-bearing, not cosmetic."""
    assert ALLOWLIST_VARIABLE == "RHEO_MODULES"
    assert not ALLOWLIST_VARIABLE.startswith("RHEO__")


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
def test_allowed_module_ids_reads_the_variable(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: frozenset[str]
) -> None:
    if value is None:
        monkeypatch.delenv(ALLOWLIST_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(ALLOWLIST_VARIABLE, value)
    assert allowed_module_ids() == expected


# --- AC 8, both directions ------------------------------------------------------------


def test_no_module_loads_unless_it_is_named(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """A discoverable module, an unset variable, and nothing registers.

    This is the configuration ``make demo`` and every image ``make build`` produces
    run in: ``Dockerfile`` COPYs ``modules/`` and ``uv sync --frozen`` installs each
    distribution, so discovery finds them there exactly as it finds this fixture here.
    """
    registry, resolvers = registries
    _publish(monkeypatch, ENTRY_POINT)
    monkeypatch.delenv(ALLOWLIST_VARIABLE, raising=False)

    assert discovered() == (ENTRY_POINT,)
    assert load_modules(registry=registry, resolvers=resolvers) == ()

    assert registry.names() == frozenset()
    assert resolvers.record_types() == frozenset()
    assert module_surfaces() == {}
    assert sink_for(MODULE_ID) is NULL_SINK


def test_a_named_module_registers_its_operations(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    registry, resolvers = registries
    _publish(monkeypatch, ENTRY_POINT)
    monkeypatch.setenv(ALLOWLIST_VARIABLE, MODULE_ID)

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
    assert sink_for("core") is NULL_SINK


def test_only_the_named_module_loads_when_two_are_discoverable(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """The allowlist is a filter, not a switch: one discoverable module loading does
    not carry its neighbour in."""
    registry, resolvers = registries
    _publish(monkeypatch, ENTRY_POINT, NO_SURFACE_ENTRY_POINT)
    monkeypatch.setenv(ALLOWLIST_VARIABLE, MODULE_ID)

    assert load_modules(registry=registry, resolvers=resolvers) == (MODULE_ID,)
    assert registry.names() == frozenset({OPERATION})


def test_a_named_module_that_is_not_installed_is_simply_absent(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """The allowlist says what *may* load. Refusing startup over a name nothing
    provides would make a deployment's variable a dependency on its image's build
    context, which is the coupling finding F17 is about."""
    registry, resolvers = registries
    _publish(monkeypatch)
    monkeypatch.setenv(ALLOWLIST_VARIABLE, MODULE_ID)

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
    monkeypatch.setenv(ALLOWLIST_VARIABLE, MODULE_ID)

    first = load_modules(registry=registry, resolvers=resolvers)
    second = load_modules(registry=registry, resolvers=resolvers)

    assert first == second == (MODULE_ID,)
    assert registry.names() == frozenset({OPERATION})
    assert sink_for(MODULE_ID) is SINK


def test_the_explicit_allow_argument_overrides_the_variable(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    registry, resolvers = registries
    _publish(monkeypatch, ENTRY_POINT)
    monkeypatch.setenv(ALLOWLIST_VARIABLE, MODULE_ID)

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
    monkeypatch.setenv(ALLOWLIST_VARIABLE, SURFACE_ONLY_ID)

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
    monkeypatch.setenv(ALLOWLIST_VARIABLE, NO_SURFACE_ID)

    assert load_modules(registry=registry, resolvers=resolvers) == (NO_SURFACE_ID,)
    assert module_surfaces() == {}


def test_module_surfaces_hands_back_a_copy(
    monkeypatch: pytest.MonkeyPatch,
    registries: tuple[OperationRegistry, ResolverRegistry],
) -> None:
    """Read-only in the way that matters: a caller cannot edit the loader's table."""
    registry, resolvers = registries
    _publish(monkeypatch, ENTRY_POINT)
    monkeypatch.setenv(ALLOWLIST_VARIABLE, MODULE_ID)
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
    monkeypatch.setenv(ALLOWLIST_VARIABLE, "something_else")

    with pytest.raises(ManifestInvalid) as excinfo:
        load_modules(registry=registry, resolvers=resolvers)
    assert excinfo.value.module_id == MODULE_ID
    assert registry.names() == frozenset()


def test_validate_refuses_a_schema_that_is_not_the_module_id() -> None:
    with pytest.raises(ManifestInvalid):
        validate(
            ModuleManifest(
                module_id=MODULE_ID,
                package_version="0.0.0",
                schema_name="somewhere_else",
                create_schema=_create_schema,
            )
        )


def test_validate_refuses_something_that_is_not_a_manifest() -> None:
    with pytest.raises(ManifestInvalid):
        validate(object())


def test_validate_accepts_the_fixture() -> None:
    assert validate(MANIFEST) is MANIFEST


# --- the fixture's own guard ----------------------------------------------------------


def test_the_fixture_id_cannot_collide_with_a_real_distribution() -> None:
    """The first real module distribution will publish under the same entry-point
    group as these fixtures.

    This walks the *real* environment, unmonkeypatched, and asserts only that nothing
    claims the fabricated ids — not that the group is empty, which would go red the
    moment a real module is installed.
    """
    fabricated = {MODULE_ID, SURFACE_ONLY_ID, NO_SURFACE_ID}
    assert fabricated.isdisjoint({entry.name for entry in discovered()})


def test_the_process_variable_is_not_set_by_the_suite() -> None:
    """If a developer exported ``RHEO_MODULES`` in their shell, the negative
    direction above would still pass (it deletes the variable) but the rest of the
    suite would silently run with a module loaded. Fail loudly instead."""
    assert os.environ.get(ALLOWLIST_VARIABLE) in (None, "")
