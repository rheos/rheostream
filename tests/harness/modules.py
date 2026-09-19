"""Fixture module manifests, and the one recipe that gets them into the loader.

**One copy of three helpers that were four, three and three.** ``manifest``,
``publish`` and ``install`` were written out again in every file that drives the
loader — ``tests/test_module_loader.py``, ``tests/test_module_sets.py``,
``tests/test_module_registration.py`` and (for the manifest builder and an inline
``entry_points`` monkeypatch) ``tests/test_worker_job_kinds.py``. Each of those now
imports from here under its former private name, so no call site moved: the helpers
gained one home, not a rename.

**The recipe is the shipped one and no test may invent a second.** Nothing in the
checkout published a real ``rheo.modules`` entry point until Recallatron, so a fixture
module reaches ``loaded_manifests()`` by building a *real*
``importlib.metadata.EntryPoint`` whose ``value`` points at a module-level name in this
file — ``EntryPoint.load()`` then runs for real rather than being stubbed out alongside
the discovery it is meant to exercise — and by monkeypatching the loader's own
``entry_points`` so the ambient environment's real entry points are invisible for that
test. :func:`loaded_probe_modules` is that recipe end to end, and it takes real module
ids as readily as fabricated ones, so an install test that needs Recallatron and one
that needs a fixture drive the loader the same way.

**Every fabricated id is ``_probe``-suffixed**, never a plausible product name: a
fixture that borrowed the id of a distribution shipped later would collide with it, and
``tests/test_module_loader.py::test_the_fixture_id_cannot_collide_with_a_real_distribution``
pins that against the real, unmonkeypatched environment. ``recallatron`` is a real
distribution and is never a fixture id.

**The install fixtures declare a ``contract_tests`` path that really exists, and that is
load-bearing rather than tidy.** ``core.module.install``'s fourth pre-flight step
resolves the declared path against the checkout root and refuses
``contract_tests_missing`` when it is absent — under ``profile = test``, which is every
run of this suite. The builder's own default is a path that does **not** exist, which is
what makes it the fixture for that refusal; every manifest meant to get past pre-flight
overrides it with :data:`PROBE_CONTRACT_TESTS`.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import EntryPoint
from typing import Final

import pytest
from rheo_core.modules import (
    ENTRY_POINT_GROUP,
    Dependency,
    ExportDeclaration,
    ModuleManifest,
    StorageDeclaration,
    discovered,
    load_modules,
    loaded_manifests,
    reset_surfaces,
)
from rheo_core.modules import loader as loader_module
from rheo_core.modules.loader import ALLOWLIST_KEY
from rheo_core.operations import OperationRegistry
from rheo_core.refs.resolver import ResolverRegistry
from rheo_core.settings import env_variable_names
from rheo_core.tokens.sets import ToolRegistry

MODULES_VARIABLE, MODULES_VARIABLE_UPPER = env_variable_names(ALLOWLIST_KEY)
"""The two environment spellings the deployment layer accepts for the allowlist key.

Derived from the key rather than written out, so these tests follow the shipped
mapping instead of restating it.
"""

PROBE_CONTRACT_TESTS: Final = "tests/harness"
"""A directory that really exists in the checkout, for fixtures that must install.

Any real directory would do; this one is where the fixtures themselves live, so a
reader who follows the path lands on the manifests that declare it.
"""


# --- the twenty-four-field builder ----------------------------------------------------


def _exporter(*args: object, **kwargs: object) -> object:
    """Never called: no test exports a fabricated module."""
    return None


def _importer(*args: object, **kwargs: object) -> object:
    """Never called: no test imports a fabricated module."""
    return None


def manifest(module_id: str, **overrides: object) -> ModuleManifest:
    """A valid manifest for ``module_id``, with everything unspecified declared empty.

    Twenty-one of the model's twenty-four fields are required, and most fixtures care
    about two or three of them. The rest are declared here once rather than once per
    fixture, so a reader sees what each fixture actually varies. That the required
    fields really are required is ``tests/test_module_manifest.py``'s assertion, not
    this file's — that module builds its own field dict on purpose, because its
    subject is omitting fields one at a time and a builder that fills them in is the
    one shape that cannot express it.
    """
    fields: dict[str, object] = {
        "module_id": module_id,
        "package_version": "0.0.0",
        "core_contract_versions": (1,),
        "dependencies": (),
        "record_types": (),
        "storage": StorageDeclaration(
            schema_name=module_id,
            migrations_path=f"modules/{module_id}/migrations",
            required_extensions=(),
        ),
        "configuration_schema": (),
        "operations": (),
        "tools": (),
        "events": (),
        "subscriptions": (),
        "jobs": (),
        "schedules": (),
        "resolvers": (),
        "deletion_participants": (),
        "export": ExportDeclaration(
            format_version=1,
            schema_path=f"modules/{module_id}/schema.json",
            exporter=_exporter,
            importer=_importer,
        ),
        "secret_scopes": (),
        "connector_bindings": (),
        "health_checks": (),
        # Deliberately a path that does not exist; see this module's docstring.
        "contract_tests": f"tests/modules/{module_id}",
        "sensitivity": {},
    }
    fields.update(overrides)
    return ModuleManifest(**fields)


# --- publishing and allowing ----------------------------------------------------------


def publish(monkeypatch: pytest.MonkeyPatch, *entry_points: EntryPoint) -> None:
    """Make ``discovered()`` see exactly these, and nothing the environment has."""
    monkeypatch.setattr(
        loader_module, "entry_points", lambda group: tuple(entry_points)
    )


def install(monkeypatch: pytest.MonkeyPatch, *module_ids: str) -> None:
    """Resolve ``modules.installed`` to exactly ``module_ids`` for one test.

    Through the deployment layer rather than by patching the loader: what a
    deployment does is set the key, and the coercion from text to ``list[str]`` is
    part of what these tests are about. No ids at all removes the variables, so the
    key falls back to its empty package default.

    **Both spellings, and the deleting half is why.** The deployment layer accepts
    ``RHEO__modules__installed`` and ``RHEO__MODULES__INSTALLED``, tries the exact
    form first and falls through to the uppercase one. Measured on this tree: with
    both set the exact form wins, so *setting* one would have been enough — but
    *deleting* only the exact form falls through to an ambient uppercase value, and
    that is precisely the direction AC 8's "nothing loads unless it is named" cases
    drive. A developer with ``RHEO__MODULES__INSTALLED`` exported would have those
    cases quietly loading a module.

    ``tests/test_module_loader.py::test_the_allowlist_setting_is_not_set_by_the_suite``
    asserts both are unset across the session, but that is one test in a session: it
    makes such a leak *loud*, it does not prevent it, and it protects nothing that ran
    before it. Handling both here lets this helper answer for itself.
    """
    for variable in (MODULES_VARIABLE, MODULES_VARIABLE_UPPER):
        if module_ids:
            monkeypatch.setenv(variable, ",".join(module_ids))
        else:
            monkeypatch.delenv(variable, raising=False)


# --- the install fixtures -------------------------------------------------------------

EXTENSION_ID: Final = "ext_probe"
EXTENSION_NAME: Final = "citext"
"""A trusted extension every stock PostgreSQL 13+ installation carries.

Trusted matters: a non-superuser that owns the database may create it, so the positive
half of AC 11 does not quietly depend on the test cluster's role being a superuser.
"""

ABSENT_EXTENSION_ID: Final = "noext_probe"
ABSENT_EXTENSION_NAME: Final = "rheo_no_such_extension_probe"
"""An extension name no installation provides, so ``CREATE EXTENSION`` really fails.

**DEVIATION FROM RATIFIED TEXT — run 1a0, phase 6, Edge Case 3.** The ratified edge
case asks for the extension failure to be driven by "a test role explicitly denied the
privilege". That is unreachable on this cluster and the premise was measured, not
assumed: the ``rheo`` role is a PostgreSQL **superuser** (``usesuper = true``, and it
is the only login role), and a superuser is refused no extension whose files are
present. So the privilege branch of ``CREATE EXTENSION`` cannot be entered through the
install path here at all.

Two substitutes cover what the edge case is *for* — a ``CREATE EXTENSION`` the
database genuinely refuses, told apart from a name it has never heard of:

1. this fixture, naming an extension the server does not provide; and
2. :data:`EXTENSION_MANIFEST` installed into a workspace where a conflicting object of
   that name already exists — the extension is available, the manifest is fine, and
   the database still says no.

Recorded here, on the fixture that exists because of it, rather than only in the test
that uses it: this is the module a later prompt reads when it reaches for these
fixtures, and a deviation from ratified text should be found by whoever picks the
fixture up. ``tests/postgres/test_module_install.py`` carries the same note beside the
assertions.
"""

PROVIDER_ID: Final = "provider_probe"
PROVIDER_VERSION: Final = "1.4.0"
DEPENDANT_ID: Final = "dependant_probe"
DEPENDANT_RANGE: Final = ">=1,<2"
"""The dependant's declared range, satisfied by :data:`PROVIDER_VERSION`.

Satisfied at *load* time on purpose: ``load_modules`` refuses a set whose required
dependency is absent from it or outside the range, so a pair that could not load
together could never reach install's own dependency check at all. What install adds is
the second question — whether the dependency is installed *in this workspace*, at a
version this workspace records — and that is the one the fixtures vary.
"""


def _storage(module_id: str, *extensions: str) -> StorageDeclaration:
    """A storage declaration pointing at this module's own fixture chain."""
    return StorageDeclaration(
        schema_name=module_id,
        migrations_path=f"harness.module_migrations.{module_id}",
        required_extensions=extensions,
    )


EXTENSION_MANIFEST: Final = manifest(
    EXTENSION_ID,
    storage=_storage(EXTENSION_ID, EXTENSION_NAME),
    contract_tests=PROBE_CONTRACT_TESTS,
)
"""Installs cleanly, creating :data:`EXTENSION_NAME` before its chain runs."""

ABSENT_EXTENSION_MANIFEST: Final = manifest(
    ABSENT_EXTENSION_ID,
    storage=_storage(ABSENT_EXTENSION_ID, ABSENT_EXTENSION_NAME),
    contract_tests=PROBE_CONTRACT_TESTS,
)
"""Fails at the extension step. **Its chain is real**, which is the whole point: the
assertion that no migration ran is only worth something if a migration could have."""

PROVIDER_MANIFEST: Final = manifest(
    PROVIDER_ID,
    package_version=PROVIDER_VERSION,
    storage=_storage(PROVIDER_ID),
    contract_tests=PROBE_CONTRACT_TESTS,
)

DEPENDANT_MANIFEST: Final = manifest(
    DEPENDANT_ID,
    storage=_storage(DEPENDANT_ID),
    contract_tests=PROBE_CONTRACT_TESTS,
    dependencies=(Dependency(module_id=PROVIDER_ID, version_range=DEPENDANT_RANGE),),
)

OPTIONAL_DEPENDANT_ID: Final = "optional_probe"
OPTIONAL_DEPENDANT_MANIFEST: Final = manifest(
    OPTIONAL_DEPENDANT_ID,
    storage=_storage(OPTIONAL_DEPENDANT_ID),
    contract_tests=PROBE_CONTRACT_TESTS,
    dependencies=(
        Dependency(module_id=PROVIDER_ID, version_range=DEPENDANT_RANGE, optional=True),
    ),
)
"""The same edge from the other side: an optional dependency this workspace lacks.

``optional = True`` says the dependency may be *absent*, so this one installs cleanly
into a workspace that has never heard of :data:`PROVIDER_ID` — and it loads cleanly
beside nothing, because ``load_modules`` skips an absent optional dependency too. It
is a separate manifest from :data:`DEPENDANT_MANIFEST` rather than a flag on it
because both have to be loadable at once for the dependency cases to be set up in one
workspace.
"""

MISSING_TESTS_ID: Final = "notests_probe"
MISSING_TESTS_MANIFEST: Final = manifest(MISSING_TESTS_ID)
"""Every field on the builder's defaults, including a ``contract_tests`` path that is
not in the checkout — which is what makes it the fixture for
``contract_tests_missing``. Its ``migrations_path`` is bogus too and never reached:
pre-flight refuses this module before any job is enqueued, which is the ordering the
refusal's own test asserts.
"""


def entry_point_for(module_manifest: ModuleManifest, attribute: str) -> EntryPoint:
    """A real entry point loading ``attribute`` from this module, under its own id."""
    return EntryPoint(
        name=module_manifest.module_id,
        value=f"{__name__}:{attribute}",
        group=ENTRY_POINT_GROUP,
    )


PROBE_ENTRY_POINTS: Final[dict[str, EntryPoint]] = {
    fixture.module_id: entry_point_for(fixture, attribute)
    for fixture, attribute in (
        (EXTENSION_MANIFEST, "EXTENSION_MANIFEST"),
        (ABSENT_EXTENSION_MANIFEST, "ABSENT_EXTENSION_MANIFEST"),
        (PROVIDER_MANIFEST, "PROVIDER_MANIFEST"),
        (DEPENDANT_MANIFEST, "DEPENDANT_MANIFEST"),
        (OPTIONAL_DEPENDANT_MANIFEST, "OPTIONAL_DEPENDANT_MANIFEST"),
        (MISSING_TESTS_MANIFEST, "MISSING_TESTS_MANIFEST"),
    )
}
"""Every fixture module this file publishes, by id.

The attribute name travels beside the manifest rather than being derived from the id,
because :func:`entry_point_for` builds a real ``EntryPoint`` and ``EntryPoint.load()``
resolves that name for real — a derived name that did not exist would fail at load
time with an ``AttributeError`` rather than at the assertion the test came for.
"""


# --- the one loading recipe -----------------------------------------------------------


@contextmanager
def loaded_probe_modules(
    monkeypatch: pytest.MonkeyPatch, *module_ids: str
) -> Iterator[None]:
    """Load exactly ``module_ids`` for the body of the ``with``, then forget them.

    A fabricated id is published from this file; any other id is taken from the real
    ``rheo.modules`` entry points, so a test that needs Recallatron beside a fixture
    module — or instead of one — drives the loader through this same recipe rather
    than a second one.

    The registries are local: a fixture module's registrations have no business
    reaching the process-wide tables the rest of the session reads. ``_LOADED`` is
    process-wide and cannot be local, which is what :func:`reset_surfaces` either side
    is for.

    **The audit-sink table is deliberately left alone.** Every fixture here declares
    ``audit_sink = None``, so loading one installs nothing to clean up, while
    ``reset_sinks()`` would take out the ``core`` sink ``tests/conftest.py`` installs
    before every test — and an install dispatch is a ``mutate`` that refuses
    ``audit_sink_missing`` without it.
    """
    reset_surfaces()
    try:
        publish(monkeypatch, *_entry_points_for(module_ids))
        install(monkeypatch, *module_ids)
        loaded = load_modules(
            registry=OperationRegistry(),
            resolvers=ResolverRegistry(),
            tools=ToolRegistry(),
            allow=frozenset(module_ids),
        )
        assert loaded == tuple(sorted(module_ids)), loaded
        assert set(loaded_manifests()) == set(module_ids), sorted(loaded_manifests())
        yield
    finally:
        reset_surfaces()


def _entry_points_for(module_ids: tuple[str, ...]) -> tuple[EntryPoint, ...]:
    """The entry points ``module_ids`` names: fabricated here, or really installed.

    ``discovered()`` is read **before** :func:`publish` replaces it, so a real
    distribution's entry point is the one the environment actually publishes.
    """
    published: list[EntryPoint] = []
    for module_id in module_ids:
        fabricated = PROBE_ENTRY_POINTS.get(module_id)
        if fabricated is not None:
            published.append(fabricated)
            continue
        real = [entry for entry in discovered() if entry.name == module_id]
        assert real, (
            f"no {ENTRY_POINT_GROUP} entry point named {module_id!r} is installed in "
            "this environment, and it is not one of this file's fixtures"
        )
        published.extend(real)
    return tuple(published)
