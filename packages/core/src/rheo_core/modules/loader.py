"""Entry-point discovery and the ``RHEO_MODULES`` allowlist: nothing loads unless it
is named.

``discovered()`` walks the ``rheo.modules`` packaging entry-point group.
``allowed_module_ids()`` reads the comma-separated ``RHEO_MODULES`` process variable —
unset, empty, or all-whitespace is the empty set. ``load_modules()`` loads **only** the
discovered entry points whose name is in that set, validates each manifest, and then
registers its operations and resolvers through the shipped registries with
``origin = manifest.module_id``, so every registration check a hand-written
registration faces (name grammar, origin/prefix agreement, reserved input fields,
``extra = "allow"``) applies unchanged.

**Nothing loads by default, and that is the whole gate.** ``Dockerfile`` COPYs
``modules/`` and runs ``uv sync --frozen``, so every ``modules/*`` distribution in the
checkout is installed into the image and its entry point is discoverable at run time;
``make demo``'s ``core`` runs under ``RHEO_PROFILE: development``. A loader that
registered what it discovered would therefore change the behaviour of two shipped
targets this run does not own, invisibly to the test suite. A profile branch would not
help: ``development`` is not ``production``. So the gate is an explicit allowlist on
the deployment's side, which is also the shape the ratified ``modules.installed`` key
should have had (run 0v's finding **F5**; **F17** on installed-versus-loaded).

**``RHEO_MODULES`` carries one underscore, deliberately.** A ``RHEO__``-prefixed name
is read by the settings layer as a deployment override, and
``rheo_core.settings.deployment`` raises ``SettingUndeclared`` for any ``RHEO__``
variable no schema declares. This run declares no settings key (AC 18: open PR #22
already edits ``tests/test_settings.py``'s exact-key assertion). The run that lands the
real ``modules.installed`` key deletes this variable and the two lines that read it.

**The entry-point name is the module id**, and the allowlist is matched against it
*before* the entry point is loaded. Loading an entry point imports the distribution, so
filtering afterwards would import every module on disk to decide it should not have
been imported. ``load_modules`` then refuses a manifest whose ``module_id`` disagrees
with the name it was found under, because otherwise the allowlist would gate one name
while a different one registered.
"""

import os
from collections.abc import Mapping
from importlib.metadata import EntryPoint, entry_points
from typing import Final

from rheo_core.audit.sink import install_sink
from rheo_core.modules.manifest import ManifestInvalid, ModuleManifest, WebSurface
from rheo_core.modules.manifest import validate as validate_manifest
from rheo_core.operations.registry import REGISTRY, OperationRegistry
from rheo_core.refs.resolver import RESOLVERS, ResolverRegistry

ENTRY_POINT_GROUP: Final = "rheo.modules"
ALLOWLIST_VARIABLE: Final = "RHEO_MODULES"

_SURFACES: Final[dict[str, WebSurface]] = {}
"""surface name -> the loaded manifest's web surface.

The one thing ``load_modules()`` keeps about what it loaded. Keyed by the manifest's
own ``WebSurface.surface``, not by its ``module_id``: the two are not required to
match, and the consumer — the internal listener's ``routing_config()`` — looks a
surface up by name. Deliberately not a broader ``loaded_manifests()``: nothing else in
this run needs the manifest back, and an accessor with no caller is a shape guessed
early.
"""


def discovered() -> tuple[EntryPoint, ...]:
    """Every ``rheo.modules`` entry point visible to this interpreter, unloaded."""
    return tuple(entry_points(group=ENTRY_POINT_GROUP))


def allowed_module_ids() -> frozenset[str]:
    """The module ids ``RHEO_MODULES`` names; the empty set when it names none."""
    raw = os.environ.get(ALLOWLIST_VARIABLE, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def module_surfaces() -> Mapping[str, WebSurface]:
    """The web surfaces the loaded manifests declared, read-only, by surface name."""
    return dict(_SURFACES)


def reset_surfaces() -> None:
    """Forget what was loaded. For tests, and for a process that reloads modules."""
    _SURFACES.clear()


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
        manifest = validate_manifest(entry_point.load())
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
    if manifest.web is not None:
        _SURFACES[manifest.web.surface] = manifest.web
