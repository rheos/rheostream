"""Entry-point discovery and the ``modules.installed`` allowlist: nothing loads unless
it is named.

``discovered()`` walks the ``rheo.modules`` packaging entry-point group.
``allowed_module_ids()`` reads the deployment-scope settings key ``modules.installed``,
a ``list[str]`` whose package default is empty — so an unconfigured deployment names no
module at all. ``load_modules()`` loads **only** the discovered entry points whose name
is in that set, validates each manifest, and then registers its operations and
resolvers through the shipped registries with ``origin = manifest.module_id``, so every
registration check a hand-written registration faces (name grammar, origin/prefix
agreement, reserved input fields, ``extra = "allow"``) applies unchanged.

**Nothing loads by default, and that is the whole gate.** ``Dockerfile`` COPYs
``modules/`` and runs ``uv sync --frozen``, so every ``modules/*`` distribution in the
checkout is installed into the image and its entry point is discoverable at run time;
``make demo``'s ``core`` runs under ``RHEO_PROFILE: development``. A loader that
registered what it discovered would therefore change the behaviour of two shipped
targets, invisibly to the test suite. A profile branch would not help: ``development``
is not ``production``. So the gate is an explicit allowlist on the deployment's side
(run 0v's finding **F5**; **F17** on installed-versus-loaded), and a discovered id the
list does not name is simply not loaded — no exception, and no refusal to start.

**Read through the typed accessor, not a subscript.** ``ResolvedSettings.get_list``
returns ``list[str]`` and raises ``SettingTypeMismatch`` if ``modules.installed`` is
ever redeclared at another ``ValueType``; a bare subscript hands back the
``FrozenValue`` union, which would take a wrongly-typed key without a word.

**Three sets, three names, and no function here answers for two of them.**
:func:`discovered` is "installed on this host"; :func:`allowed_module_ids` and
:func:`loaded_manifests` are "loaded by this deployment"; the ``core.module_state``
rows read through ``rheo_core.storage.repositories.list_module_states`` are "installed
and enabled in this workspace". ``tests/test_module_sets.py`` is where that separation
is pinned.

**The entry-point name is the module id**, and the allowlist is matched against it
*before* the entry point is loaded. Loading an entry point imports the distribution, so
filtering afterwards would import every module on disk to decide it should not have
been imported. ``load_modules`` then refuses a manifest whose ``module_id`` disagrees
with the name it was found under, because otherwise the allowlist would gate one name
while a different one registered.
"""

from collections.abc import Mapping
from importlib.metadata import EntryPoint, entry_points
from typing import Final

from rheo_core.audit.sink import install_sink
from rheo_core.modules.manifest import ManifestInvalid, ModuleManifest, WebSurface
from rheo_core.operations.registry import REGISTRY, OperationRegistry
from rheo_core.refs.resolver import RESOLVERS, ResolverRegistry
from rheo_core.settings import resolve

ENTRY_POINT_GROUP: Final = "rheo.modules"
ALLOWLIST_KEY: Final = "modules.installed"

_LOADED: Final[dict[str, ModuleManifest]] = {}
"""module id -> the manifest ``load_modules()`` loaded under it.

The deployment-loaded set, and the only record of what this process actually
registered. Keyed by ``module_id`` because that is the name the allowlist gated and
the name both registries recorded as the registration's origin; ``module_surfaces()``
re-keys by ``WebSurface.surface`` on the way out, because the two are not required to
match and that consumer — the internal listener's ``routing_config()`` — looks a
surface up by name.
"""


def discovered() -> tuple[EntryPoint, ...]:
    """Every ``rheo.modules`` entry point visible to this interpreter, unloaded."""
    return tuple(entry_points(group=ENTRY_POINT_GROUP))


def allowed_module_ids() -> frozenset[str]:
    """The module ids ``modules.installed`` names; the empty set when it names none."""
    return frozenset(resolve().get_list(ALLOWLIST_KEY))


def loaded_manifests() -> Mapping[str, ModuleManifest]:
    """What this deployment loaded, read-only, by module id.

    Read-only in the way that matters: a fresh dict, so a caller cannot edit the
    loader's own table through it. The manifests themselves are frozen models.
    """
    return dict(_LOADED)


def module_surfaces() -> Mapping[str, WebSurface]:
    """The web surfaces the loaded manifests declared, read-only, by surface name.

    Derived from :data:`_LOADED` rather than from a second table kept beside it: one
    record of what loaded cannot disagree with itself about which surfaces are live.
    """
    return {
        manifest.web.surface: manifest.web
        for manifest in _LOADED.values()
        if manifest.web is not None
    }


def reset_surfaces() -> None:
    """Forget what was loaded. For tests, and for a process that reloads modules.

    Keeps the name it had when the table it emptied held surfaces alone. What callers
    want of it has not changed — "empty the loader's process-wide record" — and the
    surfaces are still forgotten, because :func:`module_surfaces` now reads the table
    this clears.
    """
    _LOADED.clear()


def _as_manifest(loaded: object) -> ModuleManifest:
    """The one load-time check that is not a field-shape check.

    A ``ModuleManifest`` instance was already validated by pydantic at construction,
    so nothing here re-validates it: a second ``model_validate`` pass would either
    be a no-op or, on a model carrying callables, a rebuild with no gain. What an
    entry point can still hand back is *not a manifest at all*, and that is what
    this refuses — the failure the deleted ``manifest.validate()`` opened with.
    """
    if not isinstance(loaded, ModuleManifest):
        raise ManifestInvalid(
            str(getattr(loaded, "module_id", loaded)),
            "a rheo.modules entry point must load to a ModuleManifest",
        )
    return loaded


def load_modules(
    *,
    registry: OperationRegistry = REGISTRY,
    resolvers: ResolverRegistry = RESOLVERS,
    allow: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Register the allowed discovered modules; return their ids, sorted.

    ``allow`` defaults to :func:`allowed_module_ids`. A module id in ``allow`` that
    nothing on the path provides is simply not loaded: the allowlist says what *may*
    load, not what must be present, and refusing startup over it would make a
    deployment's variable a dependency on its image's build context, which is the
    coupling finding F17 is about.

    Idempotent, because everything it calls is: both registries treat an identical
    re-registration as a no-op and :func:`~rheo_core.audit.sink.install_sink` treats an
    identical re-install as one. ``apps/core``'s FastAPI lifespan runs more than once
    in the same process under test, so this function does too.
    """
    permitted = allowed_module_ids() if allow is None else allow
    loaded: list[str] = []
    for entry_point in discovered():
        if entry_point.name not in permitted:
            continue
        manifest = _as_manifest(entry_point.load())
        if manifest.module_id != entry_point.name:
            raise ManifestInvalid(
                manifest.module_id,
                f"is published under the {ENTRY_POINT_GROUP} entry-point name "
                f"{entry_point.name!r}; the allowlist gates the name, so the two "
                "must agree",
            )
        _register(manifest, registry=registry, resolvers=resolvers)
        loaded.append(manifest.module_id)
    return tuple(sorted(loaded))


def _register(
    manifest: ModuleManifest,
    *,
    registry: OperationRegistry,
    resolvers: ResolverRegistry,
) -> None:
    """One validated manifest's operations, resolvers, sink and web surface."""
    for declaration, handler in manifest.operations:
        registry.register(declaration, handler, origin=manifest.module_id)
    for record_type, resolver in manifest.resolvers:
        resolvers.register(
            manifest.module_id, record_type, resolver, origin=manifest.module_id
        )
    if manifest.audit_sink is not None:
        # Under the manifest's own module id, and never any other key — the
        # dispatcher resolves a sink by the *operation's* owning module, so a sink
        # installed anywhere else would fire on somebody else's operations.
        install_sink(manifest.module_id, manifest.audit_sink)
    # The deployment-loaded record, written for every manifest rather than only for one
    # declaring a web surface: ``module_surfaces()`` derives its answer from here.
    _LOADED[manifest.module_id] = manifest
