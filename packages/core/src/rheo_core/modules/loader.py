"""Entry-point discovery and the ``modules.installed`` allowlist: nothing loads unless
it is named.

``discovered()`` walks the ``rheo.modules`` packaging entry-point group.
``allowed_module_ids()`` reads the deployment-scope settings key ``modules.installed``,
a ``list[str]`` whose package default is empty — so an unconfigured deployment names no
module at all. ``load_modules()`` loads **only** the discovered entry points whose name
is in that set, validates each manifest, and then registers what it declares through
the shipped registries with ``origin = manifest.module_id``, so every registration
check a hand-written registration faces (name grammar, origin/prefix agreement,
reserved input fields, ``extra = "allow"``) applies unchanged.

**Six extension points, six shipped registries, and no registry of this module's
own.** ``_register`` attaches a manifest's operations (``OperationRegistry``),
resolvers (``ResolverRegistry``), tools (``ToolRegistry``), job kinds
(``JobKindRegistry``), subscriptions (``ConsumerRegistry``) and
``configuration_schema`` keys (``settings.schema.REGISTRY``) — each through the same
public ``register`` the core's own startup calls, never a loader-local table. Declared
**events** are the exception and deliberately so: ``rheo_core.events`` indexes
subscriptions, not declarations, so there is nothing to populate and a declared event
is read back from ``loaded_manifests()[module_id].events``. What the loader does for
events instead is refuse them — a type not namespaced under its declaring module, or
a type two loaded manifests both declare.

**The residual gap, stated rather than left for a reader to find: a module's job kind
or consumer id can silently replace the core's, and nothing here refuses it.**
``JobKindRegistry`` and ``ConsumerRegistry`` are last-writer-wins by design, and each
documents that as safe on the grounds that the composition root is its only writer
and populates it once at import. This module makes a module a *second* writer. A
loaded manifest declaring ``JobKind(name="core.retention_sweep", ...)`` therefore
replaces the production handler and input model for that kind with its own, refused
by nothing — not the registry, not ``_register``, not the manifest model — and a
``ConsumerSubscription`` reusing a core ``consumer_id`` displaces that consumer the
same way.

That is an asymmetry with the two guards above rather than an oversight, and the
reason is what is missing from the contract. Events get a namespacing rule because
``module-contract.md`` ratifies the grammar ``<module_id>.<record_type>.<verb>`` for
them; subscriptions get an ownership check because a ``ConsumerSubscription`` carries
the ``module_id`` there is something to check it against. Neither holds for a job-kind
name: the contract states no prefix rule for one, so a guard here would have to invent
the grammar it enforced, which is a ratification and not an implementation. Closing it
waits on that rule — the position ``tokens/sets.py`` already records for tool names
(nothing binds a tool's name to its registering origin, for want of a ratified prefix
rule), and the deferral the plan already applies to an event registry and a
``publish()`` gate over undeclared types. Nothing in the checkout declares a job kind
or a subscription today, so this is a contract gap and not a live exposure.

**Loading a module widens every workspace provisioned afterwards, and that is not a
bug to fix here.** ``settings.schema.REGISTRY`` is process-global, and
``storage/provisioning.py``'s ``_step_write_default_settings`` iterates
``REGISTRY.explicit_per_workspace()`` and writes a row for every spec it finds. So
from the moment a module loads, any ``KeySpec`` it declares as
``explicit_per_workspace`` is written into **every workspace this process provisions
from then on**, whether or not that module is installed or enabled there; a later
run's enable step writes the same rows for workspaces provisioned before. Both
writers go through ``insert_workspace_setting_if_absent``, so the overlap is
idempotent rather than a conflict, and the row is inert in a workspace that never
enables the module. Filtering it in ``provisioning.py`` would make that step depend on
per-workspace module state it does not hold, and giving modules a second,
module-local settings registry would put two answers behind one settings key. Recorded
here so the next run reads it as a known consequence rather than rediscovering it as a
defect.

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

from collections.abc import Mapping, Sequence
from graphlib import CycleError, TopologicalSorter
from importlib.metadata import EntryPoint, entry_points
from typing import Final

from packaging.specifiers import SpecifierSet
from rheo_contracts import CONTRACT_VERSION

from rheo_core.audit.sink import install_sink
from rheo_core.events.consumers import ConsumerRegistry
from rheo_core.modules.manifest import ManifestInvalid, ModuleManifest, WebSurface
from rheo_core.operations.registry import REGISTRY, OperationRegistry
from rheo_core.refs.resolver import RESOLVERS, ResolverRegistry
from rheo_core.settings import resolve
from rheo_core.settings.schema import REGISTRY as SETTINGS_REGISTRY
from rheo_core.tokens.sets import TOOL_REGISTRY, ToolRegistry
from rheo_core.work.kinds import JobKindRegistry

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


def loaded_in_dependency_order() -> tuple[ModuleManifest, ...]:
    """What this deployment loaded, every required dependency ahead of its dependant.

    The tree's one dependency-sorted order, made reachable. :func:`load_modules`
    computes that order to register in and then returns ids sorted *alphabetically* —
    ``apps/core``'s startup report and ``rheo openapi`` both read that return value
    and neither wants an ordering that shifts when a dependency edge is added — so
    without this accessor the order would leave no trace a later caller could read,
    and the next caller needing one (a module migration chain, which means exactly
    this by "the manifest's dependency-sorted order") would derive a second.

    **Re-derived from :data:`_LOADED` on every call, never stored**, which is what
    makes it survive a second load in one process. A tuple cached at registration
    time would answer for the set that existed when it was written, and
    ``apps/core``'s lifespan loads more than once per process under test.
    :func:`module_surfaces` derives from the same table for the same reason: one
    record of what loaded cannot disagree with itself.

    It answers for the **accumulated** loaded set, because that is what
    :data:`_LOADED` holds. Every manifest in there cleared :func:`_dependency_order`
    as part of the set it arrived with, so for one load, or for repeated identical
    loads, this cannot raise. It is deliberately *not* promised total across two loads
    under different allowlists: reloading one module id at a version that no longer
    satisfies a dependant loaded earlier leaves an inconsistent accumulated set, and
    this reports that with the same ``ManifestInvalid`` rather than handing back a
    stale order.
    """
    return _dependency_order(tuple(_LOADED.values()))


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
    tools: ToolRegistry = TOOL_REGISTRY,
    kinds: JobKindRegistry | None = None,
    consumers: ConsumerRegistry | None = None,
    allow: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Register the allowed discovered modules; return their ids, sorted.

    ``allow`` defaults to :func:`allowed_module_ids`. A module id in ``allow`` that
    nothing on the path provides is simply not loaded: the allowlist says what *may*
    load, not what must be present, and refusing startup over it would make a
    deployment's variable a dependency on its image's build context, which is the
    coupling finding F17 is about.

    **``kinds`` and ``consumers`` default to ``None``, and that is not an oversight.**
    ``registry``, ``resolvers`` and ``tools`` each default to the process-global
    instance their production consumers read.
    :class:`~rheo_core.work.kinds.JobKindRegistry` and
    :class:`~rheo_core.events.consumers.ConsumerRegistry` publish no module-level
    instance to default to — both are composition-root locals, built as ``JOB_KINDS``
    and ``CONSUMERS`` in ``apps/worker/src/rheo_app_worker/main.py`` — so ``None``
    means "do not register that category", and the ``core`` process, which runs
    neither a job handler nor a consumer, legitimately passes neither.

    **No manifest is registered until the whole loaded set has passed every check
    this module makes.** The permitted manifests are collected first, each gated on
    the entry-point name and on the contract version; then the set's event
    declarations, subscription ownership, dependency ranges and dependency graph are
    checked; and only then is anything registered, in dependency-sorted order. So a
    ``ManifestInvalid`` raised here leaves every registry as it found it rather than
    stranding half a set behind the manifest that failed — which matters most on a
    multi-module load, where the alternative is an earlier module fully registered
    behind a later one's refusal.

    The promise is bounded to this module's own refusals, deliberately. A shipped
    ``register`` called from :func:`_register` can still refuse on its own rules — a
    tool naming a non-token-issuable operation raises ``RegistrationRefused``, a
    settings key already declared under another origin raises ``SettingRedeclared`` —
    and one of those does land part-way through, because a registry's own validation
    is not something this function can run ahead of time without keeping a second
    copy of it.

    Idempotent, because everything it calls is: every registry treats an identical
    re-registration as a no-op and :func:`~rheo_core.audit.sink.install_sink` treats an
    identical re-install as one. ``apps/core``'s FastAPI lifespan runs more than once
    in the same process under test, so this function does too.
    """
    permitted = allowed_module_ids() if allow is None else allow
    manifests: list[ModuleManifest] = []
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
        if CONTRACT_VERSION not in manifest.core_contract_versions:
            # Before anything of this manifest's is registered: a module written
            # against a contract major this core does not publish is refused rather
            # than loaded and left to fail on the first shape that moved.
            raise ManifestInvalid(
                manifest.module_id,
                f"is written against core contract version(s) "
                f"{list(manifest.core_contract_versions)}, which do not include "
                f"this core's CONTRACT_VERSION {CONTRACT_VERSION}",
            )
        manifests.append(manifest)
    _check_events(manifests)
    _check_subscriptions(manifests)
    loaded: list[str] = []
    for manifest in _dependency_order(manifests):
        _register(
            manifest,
            registry=registry,
            resolvers=resolvers,
            tools=tools,
            kinds=kinds,
            consumers=consumers,
        )
        loaded.append(manifest.module_id)
    return tuple(sorted(loaded))


def _check_events(manifests: Sequence[ModuleManifest]) -> None:
    """The two refusals a declared event faces, over the whole loaded set.

    A type's first dot-separated segment must be the declaring manifest's own
    ``module_id``, and no two loaded manifests may declare the same type.

    **The two rules are not independent, and the containment is worth stating.**
    Namespacing forces every declared type's first segment to be the declaring
    manifest's own id, so two manifests with *different* ids can never collide on a
    type: the second one's type fails namespacing first. What the duplicate rule is
    left covering is the case packaging can really produce — two installed
    distributions both publishing under one ``module_id``, both clearing the
    entry-point-name check above and both clearing namespacing, with nothing else to
    tell them apart. A test written as "two modules, one type" therefore asserts the
    namespacing refusal while believing it asserted this one.

    Nothing is populated here: ``rheo_core.events`` indexes subscriptions, not
    declarations, and a declared event is read back from
    ``loaded_manifests()[module_id].events``.
    """
    declared_by: dict[str, str] = {}
    for manifest in manifests:
        for event in manifest.events:
            namespace = event.type.split(".", 1)[0]
            if namespace != manifest.module_id:
                raise ManifestInvalid(
                    manifest.module_id,
                    f"declares event type {event.type!r}, which is namespaced under "
                    f"{namespace!r} rather than under its own module id",
                )
            owner = declared_by.get(event.type)
            if owner is not None:
                raise ManifestInvalid(
                    manifest.module_id,
                    f"declares event type {event.type!r}, which a manifest loaded "
                    f"under module id {owner!r} already declares; one event type is "
                    "declared by exactly one loaded manifest",
                )
            declared_by[event.type] = manifest.module_id


def _check_subscriptions(manifests: Sequence[ModuleManifest]) -> None:
    """Every declared subscription is owned by the manifest that declares it.

    ``ConsumerRegistry.register`` does not check this and should not: it stores
    whatever ``module_id`` the subscription carries, and the fan-out later tests that
    id against the workspace's enabled modules. Only the loader knows which manifest
    a subscription arrived on, so only the loader can catch one declared under
    somebody else's id — which would register cleanly and then be filtered by, or
    delivered under, the wrong module.

    **Here rather than in ``_register``, and the placement is the substance.** This
    check began inside ``_register``'s consumer branch, where it ran *after* that
    manifest's operations, resolvers and tools were already registered and after
    every earlier manifest in dependency order was fully registered. It was therefore
    the one refusal that falsified the "a refusal leaves every registry untouched"
    promise :func:`load_modules` is documented under — proven rather than reasoned
    about: a two-module load whose second manifest borrowed the first's id left the
    first module's operation, resolver, tool, job kind, audit sink and ``_LOADED``
    entry in place behind the raise.

    Unconditional, unlike the registration it guards. ``_register`` skips
    subscriptions entirely when no ``ConsumerRegistry`` is supplied, but a manifest
    declaring somebody else's subscription is malformed whether or not *this* process
    is the one that would have registered it. A core process that accepted it while
    the worker refused it would be two answers to one question about the same
    distribution.
    """
    for manifest in manifests:
        for subscription in manifest.subscriptions:
            if subscription.module_id != manifest.module_id:
                raise ManifestInvalid(
                    manifest.module_id,
                    f"declares subscription {subscription.consumer_id!r} owned by "
                    f"module {subscription.module_id!r}",
                )


def _dependency_order(
    manifests: Sequence[ModuleManifest],
) -> tuple[ModuleManifest, ...]:
    """The loaded set in registration order: every required dependency ahead of its
    dependent.

    Dependencies resolve against the **loaded** set rather than against what is
    installed on the host, because a module this deployment did not load cannot
    satisfy anything. A required dependency missing from that set, or present at a
    version outside the declared range, refuses the load naming both module ids and
    the range; an optional dependency absent from the set is skipped, which is what
    ``optional = True`` means.

    **Named deviation: the range is enforced for an optional dependency too, and the
    ratified text scoped it to required ones.** ``optional = True`` says the
    dependency may be *absent*; it does not say a declared range stops meaning
    anything when the module is right there. A manifest that writes
    ``Dependency(module_id="x", version_range=">=2,<3", optional=True)`` has stated
    which versions of ``x`` it can work beside, and loading it against ``x`` at 1.4
    would be using the one piece of information the author gave us to ignore them.
    So an optional dependency that is present and out of range refuses the load, with
    the same message a required one gets, and an optional dependency that is absent
    still loads clean.

    **``SpecifierSet.contains`` excludes prereleases, and nothing here overrides
    that.** A provider loaded at ``2.0.0rc1`` does not satisfy ``">=2,<3"`` and the
    load is refused naming both — PEP 440's own default, and ``packaging``'s. Passing
    ``prereleases=True`` would be a policy choice no requirement in this run asks for,
    so it is left at the default and recorded here and on
    :attr:`~rheo_core.modules.manifest.Dependency.version_range`, which is where a
    module author reads about the field.

    ``graphlib.TopologicalSorter`` produces the order and ``graphlib.CycleError`` is
    re-raised as :class:`~rheo_core.modules.manifest.ManifestInvalid` naming the
    cycle. This is the tree's one dependency order: a later run's module migration
    chains mean this when they say "the manifest's dependency-sorted order", rather
    than computing a second one.
    """
    by_id = {manifest.module_id: manifest for manifest in manifests}
    graph: dict[str, set[str]] = {}
    for manifest in manifests:
        predecessors = graph.setdefault(manifest.module_id, set())
        for dependency in manifest.dependencies:
            required = by_id.get(dependency.module_id)
            if required is None:
                if dependency.optional:
                    continue
                raise ManifestInvalid(
                    manifest.module_id,
                    f"requires module {dependency.module_id!r} at "
                    f"{dependency.version_range!r}, which this deployment did not "
                    "load",
                )
            if not SpecifierSet(dependency.version_range).contains(
                required.package_version
            ):
                raise ManifestInvalid(
                    manifest.module_id,
                    f"requires module {dependency.module_id!r} at "
                    f"{dependency.version_range!r}, but {dependency.module_id!r} is "
                    f"loaded at version {required.package_version}",
                )
            predecessors.add(dependency.module_id)
    try:
        order = tuple(TopologicalSorter(graph).static_order())
    except CycleError as cycle:
        nodes = [str(node) for node in cycle.args[1]]
        raise ManifestInvalid(
            nodes[0], f"is in a dependency cycle: {' -> '.join(nodes)}"
        ) from cycle
    position = {module_id: index for index, module_id in enumerate(order)}
    # ``sorted`` is stable, so two manifests published under one module id keep the
    # order discovery handed them over in.
    return tuple(sorted(manifests, key=lambda manifest: position[manifest.module_id]))


def _register(
    manifest: ModuleManifest,
    *,
    registry: OperationRegistry,
    resolvers: ResolverRegistry,
    tools: ToolRegistry,
    kinds: JobKindRegistry | None,
    consumers: ConsumerRegistry | None,
) -> None:
    """One validated manifest's six extension points, its sink and its web surface.

    Every branch iterates the manifest's own tuples and calls a shipped ``register``,
    so "an item registered that the manifest did not declare" and "declared and not
    registered" are both unreachable by construction — there is no second,
    author-supplied ``register`` function in this tree for a cross-check to compare
    against. ``module-contract.md``'s discovery step 4 records the same.
    """
    for declaration, handler in manifest.operations:
        registry.register(declaration, handler, origin=manifest.module_id)
    for record_type, resolver in manifest.resolvers:
        resolvers.register(
            manifest.module_id, record_type, resolver, origin=manifest.module_id
        )
    for tool in manifest.tools:
        tools.register(tool, origin=manifest.module_id)
    if kinds is not None:
        for job in manifest.jobs:
            # ``JobKindRegistry.register`` takes exactly these three. ``max_attempts``
            # and ``cancellable`` travel on the declaration with no reader in release
            # one — an enqueue call site reads the first and Disable the second — and
            # widening a shipped signature for a caller that does not exist yet is
            # what this deliberately does not do.
            kinds.register(job.name, job.input_model, job.handler)
    if consumers is not None:
        # Ownership was settled by ``_check_subscriptions`` before any registration
        # began; this branch registers and nothing else, so it cannot raise a
        # ``ManifestInvalid`` after an earlier module is already in the registries.
        for subscription in manifest.subscriptions:
            consumers.register(subscription)
    for spec in manifest.configuration_schema:
        # The process-global settings registry, which is also what provisioning
        # reads — see this module's docstring for what that means for every
        # workspace provisioned after a module loads.
        SETTINGS_REGISTRY.register(spec, origin=manifest.module_id)
    if manifest.audit_sink is not None:
        # Under the manifest's own module id, and never any other key — the
        # dispatcher resolves a sink by the *operation's* owning module, so a sink
        # installed anywhere else would fire on somebody else's operations.
        install_sink(manifest.module_id, manifest.audit_sink)
    # The deployment-loaded record, written for every manifest rather than only for one
    # declaring a web surface: ``module_surfaces()`` derives its answer from here.
    _LOADED[manifest.module_id] = manifest
