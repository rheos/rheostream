"""``ModuleManifest``: what one distribution declares about itself, as a model.

**Deliberately not in ``rheo_contracts``.** ``module-contract.md`` used to name the
type ``rheo_contracts.ModuleManifest``, but the manifest carries its operations'
handlers, typed against ``UnitOfWork``, and ``tests/test_imports.py`` refuses any
``rheo_contracts`` import outside the standard library, ``pydantic`` and itself —
the same collision that already forced ``OperationDeclaration.handler`` out of that
model. So the manifest lives here and each handler travels beside its declaration,
exactly as ``OperationRegistry.register(decl, handler, *, origin)`` already does.
The doc now names this module as the type's real home.

**Every field is required unless it carries ``= None``**, and only ``web``,
``agent_guidance`` and ``audit_sink`` do. Five required fields still have no
consumer (``subscriptions``, ``jobs``, ``schedules``, ``connector_bindings``,
``sensitivity``); ``record_types``, ``events`` and ``deletion_participants`` have
since gained one. They stay required so a module author writes an empty ``()`` or
``{}`` on purpose rather than by omission, and the run that builds the consumer
reads a declaration rather than a default nobody chose.

**The owned-delete pair is the one addition since**, and it is a field of
:class:`RecordType` rather than a twenty-fifth manifest field; that class's own
docstring carries why.

**``audit_sink`` is the twenty-fourth field and is not in the ratified table.** It
is carried over from the 0c0 stub because it is the only channel by which a module
supplies the sink ``loader.py``'s ``_register`` installs and ``dispatch.py``'s
``AUDIT_SINK_MISSING`` refuses every above-``READ`` operation without. Its
annotation is a ``PlainValidator`` rather than a bare ``AuditSink``, and that is
forced rather than stylistic: ``AuditSink`` is a ``typing.Protocol`` **without**
``@runtime_checkable``, so pydantic can build no schema for it and raises
``PydanticSchemaGenerationError`` at class-definition time. Adding
``arbitrary_types_allowed`` would not fix it — under that flag pydantic falls back
to an ``is_instance_schema`` whose build probes ``isinstance(None, AuditSink)``, a
probe a non-runtime-checkable Protocol refuses with ``TypeError`` — so the same
moment fails again, but silently, out of a flag that looks like it should have
helped. The validator applies the same duck check ``install_sink`` already does, so
the manifest and the installer agree on what a sink is.
"""

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Final, Protocol, Self, TypeVar

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    PlainValidator,
    StringConstraints,
    model_validator,
)
from rheo_contracts import OperationDeclaration, Role, SafetyClass, ToolDeclaration
from rheo_contracts.refs import is_reserved_module

from rheo_core.audit.sink import AuditSink
from rheo_core.deletion.registry import (
    DeleteAuthorizer as _DeleteAuthorizer,
)
from rheo_core.deletion.registry import (
    DeletionHandler as _DeletionHandler,
)
from rheo_core.deletion.registry import (
    OwnedDeleter as _OwnedDeleter,
)
from rheo_core.events.consumers import ConsumerSubscription
from rheo_core.operations.registry import Handler
from rheo_core.refs.resolver import RecordResolver
from rheo_core.settings.schema import KeySpec
from rheo_core.storage.backend import UnitOfWork
from rheo_core.work.kinds import JobHandler

if TYPE_CHECKING:  # pragma: no cover - see ``Exporter`` on why this cannot be runtime
    from rheo_core.exports.artifact import ExportSnapshot

_SEGMENT_MESSAGE: Final = "a module id is a lowercase identifier"


class ManifestInvalid(Exception):
    """A manifest the loader will not accept.

    Raised, never returned, mirroring ``RegistrationRefused``: a bad manifest is a
    packaging mistake found at load time, not a caller's error.

    **Not a wrapper for pydantic's own ``ValidationError``.** A field-shape failure
    is pydantic's to report, with the offending field named in its ``loc`` — which
    is exactly what wrapping would throw away. This exception covers the load-time
    failures that are *not* field shape: an entry point resolving to something other
    than a ``ModuleManifest``, and a manifest whose id disagrees with the
    entry-point name it was published under.
    """

    def __init__(self, module_id: str, detail: str) -> None:
        super().__init__(f"{module_id}: {detail}")
        self.module_id = module_id
        self.detail = detail


@dataclass(frozen=True, slots=True)
class WebSurface:
    """A module's own routing surface: the name it is keyed by, its host label, and
    its path prefix.

    Not ``rheo_core.routing.SurfaceConfig``, and that is the point: this package must
    not depend on ``rheo_core.routing``. The conversion happens at the point of use,
    in the internal listener's ``routing_config()``, which is the only consumer.
    """

    surface: str
    host: str
    path: str


_SLUG: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_ROUTE_SEGMENT: Final = re.compile(r"[a-z0-9-]+")
_TS_IDENTIFIER: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
WEB_PACKAGE_SCOPE: Final = "@rheo-stream/"


def _slug(value: str) -> str:
    if not _SLUG.fullmatch(value):
        raise ValueError(f"{value!r} is not a slug (lowercase words joined by '-')")
    return value


def _route_path(value: str) -> str:
    """``/`` or ``/segment(/segment)*``: an exact path, never a pattern.

    No path parameters, because parameters travel in the query string; a segment
    that could carry one (``[id]``, ``:id``, ``*``) fails the segment grammar.
    """
    if value == "/":
        return value
    if not value.startswith("/") or not all(
        _ROUTE_SEGMENT.fullmatch(segment) for segment in value[1:].split("/")
    ):
        raise ValueError(
            f"route path {value!r} is not '/' or '/segment(/segment)*' with "
            "segments of [a-z0-9-]; parameters travel in the query string"
        )
    return value


def _ts_identifier(value: str) -> str:
    """A TypeScript identifier, because ``rheo web compose`` emits it as a property
    access (``recallatronWeb.screens.browse``) rather than as a string."""
    if not _TS_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{value!r} is not a TypeScript identifier")
    return value


def _web_package_name(value: str) -> str:
    if not value.startswith(WEB_PACKAGE_SCOPE) or value == WEB_PACKAGE_SCOPE:
        raise ValueError(
            f"package_name {value!r} does not start with {WEB_PACKAGE_SCOPE!r}"
        )
    return value


Slug = Annotated[str, AfterValidator(_slug)]
RoutePath = Annotated[str, AfterValidator(_route_path)]
TsIdentifier = Annotated[str, AfterValidator(_ts_identifier)]
NavigationLabel = Annotated[str, StringConstraints(min_length=1, max_length=40)]


class NavigationEntry(BaseModel):
    """One navigation item, pointing at one of the same contribution's routes.

    Visible to ``roles`` only; the default is the two workspace roles a person holds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Slug
    label: NavigationLabel
    path: RoutePath
    roles: frozenset[Role] = frozenset({Role.OWNER, Role.MEMBER})


class WebRoute(BaseModel):
    """One exact path under the module's surface, and the screen export it renders."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Slug
    path: RoutePath
    screen: TsIdentifier


class RecordView(BaseModel):
    """The component that renders one of this module's own record types."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_type: str
    component: TsIdentifier


class FormDeclaration(BaseModel):
    """A bespoke form component for one of this module's own operations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: str
    component: TsIdentifier


class SearchProvider(BaseModel):
    """A search entry naming one of this module's own ``READ``-class operations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Slug
    operation: str


class WebContribution(BaseModel):
    """What a module contributes to ``apps/web``: its surface and its screens.

    ``rheo web compose`` reads this off every installed distribution and renders
    ``apps/web/src/modules.generated.ts`` from it; the ownership rules that need the
    manifest's own id, operations and record types are in
    :meth:`ModuleManifest._check_invariants`, the grammar rules on the fields here.

    **Two named deviations from ``module-contract.md``'s ratified table.**

    - **``surface`` is not in the ratified field list and is added deliberately.**
      ``routing_config()`` (``apps/core/src/rheo_app_core/internal_routes.py``) is a
      live consumer of the module's routing surface and has no other source for it,
      so the ``WebSurface`` this field used to be travels inside the contribution
      rather than being dropped from the manifest.
    - **A navigation entry carries ``path`` and no per-entry ``surface``**, where the
      ratified export row lists ``surface``: a module contributes navigation only
      under its own single surface, so a per-entry surface could only repeat this one
      or name somebody else's.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: WebSurface
    package_name: Annotated[str, AfterValidator(_web_package_name)]
    navigation: tuple[NavigationEntry, ...]
    routes: tuple[WebRoute, ...]
    record_views: tuple[RecordView, ...]
    forms: tuple[FormDeclaration, ...]
    search_providers: tuple[SearchProvider, ...]


def _refuse_duplicates(field: str, what: str, keys: Sequence[str]) -> None:
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            raise ValueError(f"web.{field}: {what} {key!r} is declared twice")
        seen.add(key)


def _check_web_contribution(
    web: WebContribution,
    *,
    module_id: str,
    operations: Mapping[str, SafetyClass],
    record_types: frozenset[str],
) -> None:
    """The contribution's ownership rules, each refusal naming what it refused.

    Everything here needs the manifest around the contribution — its id, its own
    operations and its own record types — which is why it runs from
    :meth:`ModuleManifest._check_invariants` rather than on :class:`WebContribution`.
    """
    _refuse_duplicates("navigation", "id", [entry.id for entry in web.navigation])
    _refuse_duplicates("routes", "id", [route.id for route in web.routes])
    _refuse_duplicates("routes", "path", [route.path for route in web.routes])
    _refuse_duplicates(
        "record_views", "record type", [view.record_type for view in web.record_views]
    )
    _refuse_duplicates("forms", "operation", [form.operation for form in web.forms])
    _refuse_duplicates(
        "search_providers", "id", [provider.id for provider in web.search_providers]
    )
    route_paths = {route.path for route in web.routes}
    for entry in web.navigation:
        if entry.path not in route_paths:
            raise ValueError(
                f"web.navigation: entry {entry.id!r} points at {entry.path!r}, "
                "which none of this contribution's routes declares"
            )
    for provider in web.search_providers:
        safety = operations.get(provider.operation)
        if safety is None:
            raise ValueError(
                f"web.search_providers: provider {provider.id!r} names "
                f"{provider.operation!r}, which is not one of module "
                f"{module_id!r}'s own operations"
            )
        if safety is not SafetyClass.READ:
            raise ValueError(
                f"web.search_providers: provider {provider.id!r} names "
                f"{provider.operation!r}, which is {safety.value!r}-class; a search "
                "provider names a read-class operation"
            )
    for form in web.forms:
        if form.operation not in operations:
            raise ValueError(
                f"web.forms: form {form.component!r} names {form.operation!r}, which "
                f"is not one of module {module_id!r}'s own operations"
            )
    owned_types = {f"{module_id}.{name}" for name in record_types}
    for view in web.record_views:
        if view.record_type not in owned_types:
            raise ValueError(
                f"web.record_views: record type {view.record_type!r} is not "
                f"'{module_id}.<name>' for one of module {module_id!r}'s own "
                "record types"
            )


HealthCheck = Callable[[UnitOfWork], None]
"""``(uow) -> None``: one check install step 6 calls directly, so it is typed."""


def _deletion_handler(value: object) -> object:
    """The duck check :class:`DeletionParticipant` applies to its handler.

    A plain validator rather than an ``isinstance`` probe for the reason this module's
    docstring gives at ``audit_sink``: the narrowed type is a ``typing.Protocol``
    without ``@runtime_checkable``, so pydantic can build no schema for it and would
    raise ``PydanticSchemaGenerationError`` at class-definition time.
    """
    if callable(value):
        return value
    raise ValueError("a deletion participant's handler must be callable")


DeletionHandler = Annotated[_DeletionHandler, PlainValidator(_deletion_handler)]
"""What the deletion coordinator calls for a participant's record types.

Narrowed in 1a1 from ``Callable[..., object]`` to
:class:`rheo_core.deletion.registry.DeletionHandler`, the protocol the coordinator
actually calls: ``(ctx, uow, ref, *, disposition) -> RemovedMemories``. The old comment
here said the alias stayed unnarrowed because no run read it and a *guessed* signature
is worse than none — that reasoning held exactly until a caller existed, and the
signature is read off that caller rather than guessed.

``Exporter`` and ``Importer`` below were narrowed in the same run and for the same
reason, one prompt later: the export collector became their caller.
"""


def _owned_delete_callable(value: object) -> object:
    """The duck check :class:`RecordType`'s owned-delete pair applies.

    ``None`` passes, because the pair is absent on every record type whose module
    does not own deleting it. Everything else must be callable, and the check is a
    plain validator rather than an ``isinstance`` probe for the reason this module's
    docstring gives at ``audit_sink``: both narrowed types are ``typing.Protocol``
    without ``@runtime_checkable``.
    """
    if value is None or callable(value):
        return value
    raise ValueError("an owned-delete callable must be callable")


DeleteAuthorizer = Annotated[
    _DeleteAuthorizer | None, PlainValidator(_owned_delete_callable)
]
"""``(ctx, uow, ref, *, disposition) -> DeleteAuthorization | Refusal``: may this be
deleted, and at which revision. Content-free by shape — see
:class:`~rheo_core.deletion.registry.DeleteAuthorization`. ``disposition`` is
keyword-only with no default; see :class:`~rheo_core.deletion.registry.Disposition`."""

OwnedDeleter = Annotated[_OwnedDeleter | None, PlainValidator(_owned_delete_callable)]
"""``(ctx, uow, authorization) -> RemovedMemories``: remove the record and the rows the
owner holds because of it."""


def _export_callable(value: object) -> object:
    """The duck check :class:`ExportDeclaration`'s pair applies.

    A plain validator for the reason this module's docstring gives at ``audit_sink``:
    both narrowed types below are ``typing.Protocol`` without ``@runtime_checkable``.
    """
    if callable(value):
        return value
    raise ValueError("an export callable must be callable")


class _ModuleExporter(Protocol):
    """``(snapshot) -> rows``: the module's own records, read through one snapshot."""

    def __call__(
        self, snapshot: "ExportSnapshot", /
    ) -> Sequence[Mapping[str, object]]: ...


class _ModuleImporter(Protocol):
    """``(uow, rows) -> second pass``: insert, then hand back what finishes.

    The return value is the reason this is not a plain ``(uow, rows) -> None``. A12's
    restore is explicitly two passes with core's own deletion-evidence import
    *between* them — parents go in with their self-references null, the ledger lands,
    and only then are the self-references resolved and the ancestry validated against
    that evidence. A module cannot schedule that itself without committing or
    reaching into core's ordering, so it returns the continuation and core calls it at
    the point A12 names.
    """

    def __call__(
        self, uow: UnitOfWork, rows: Sequence[Mapping[str, object]], /
    ) -> Callable[[], None]: ...


Exporter = Annotated[_ModuleExporter, PlainValidator(_export_callable)]
"""What writes the module's records into an export.

Narrowed in 1a1 from ``Callable[..., object]`` for the reason ``DeletionHandler``
above gives: the old note said an unnarrowed alias beat a guessed signature while no
run read it, and that held exactly until ``exports/artifact.py`` began calling it.

The exporter receives **only** ``rheo_core.exports.artifact.ExportSnapshot`` — A12's
sealed view of the one source transaction, carrying the connection, the fixed
``snapshot_at`` and the transaction-bound settings source — and never a bare
connection, which is what would let a module read a second, later view and put it in
the same artifact. That class is imported under ``TYPE_CHECKING`` only: the export
package imports this one, so a runtime import here would close the cycle.
"""

Importer = Annotated[_ModuleImporter, PlainValidator(_export_callable)]
"""What reads the module's records back out of an export.

Narrowed alongside :data:`Exporter`. It receives the restore transaction's sealed
unit of work and the already-validated rows, and returns the second pass core runs
after the deletion evidence is in. It never commits: the whole restore is one
transaction and a module ending it would take the rollback guarantee with it.
"""


class SensitivityTier(StrEnum):
    """The three redaction tiers a field can carry (``runtime-and-mcp.md``)."""

    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


_V = TypeVar("_V")


class FrozenMap(Mapping[str, _V]):
    """A read-only, hashable mapping, so ``frozen=True`` is true of the whole model.

    ``ConfigDict(frozen=True)`` promises two things: no field reassignment, and a
    working ``__hash__``. Pydantic validates a ``Mapping[...]`` annotation into a
    plain ``dict``, which keeps neither — ``manifest.sensitivity["note"]["body"] =
    ...`` mutates a "frozen" manifest in place, and ``hash(manifest)`` raises
    ``TypeError`` on the unhashable dict. Every other field on the manifest is a
    tuple, a frozen model or a frozen dataclass and so already holds; ``sensitivity``
    was the one that did not, and the 0c0 frozen dataclass this model replaced *was*
    hashable, so leaving it would have been a quiet regression behind a flag that
    says otherwise.

    Deliberately small. It is not a general-purpose frozendict and nothing here
    needs one: it exists so one declared property of ``ModuleManifest`` is true.
    """

    __slots__ = ("_items",)

    def __init__(self, items: Mapping[str, _V]) -> None:
        self._items: dict[str, _V] = dict(items)

    def __getitem__(self, key: str) -> _V:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        return hash(frozenset(self._items.items()))

    def __repr__(self) -> str:
        return f"FrozenMap({self._items!r})"


def _freeze_sensitivity(
    value: Mapping[str, Mapping[str, SensitivityTier]],
) -> Mapping[str, Mapping[str, SensitivityTier]]:
    """Both levels, because only freezing the outer one leaves the inner dict open."""
    return FrozenMap({key: FrozenMap(tiers) for key, tiers in value.items()})


Sensitivity = Annotated[
    Mapping[str, Mapping[str, SensitivityTier]],
    AfterValidator(_freeze_sensitivity),
]
"""Record type -> field name -> tier, frozen at both levels on validation."""


class Dependency(BaseModel):
    """Another module this one needs, and the versions that satisfy it."""

    model_config = ConfigDict(frozen=True)

    module_id: str
    version_range: str
    """A PEP 440 specifier set (``>=1.2,<2``), not npm's caret and tilde grammar.

    Parsed by :class:`~packaging.specifiers.SpecifierSet` in the manifest's own
    ``@model_validator``, so a range that is not PEP 440 **syntax** is a validation
    error at construction. Satisfiability is a different question and is not asked
    here: ``SpecifierSet(">=2,<1")`` parses cleanly and matches nothing. Resolving a
    range against the versions a workspace actually has is install-time work, and
    the manifest table's "cycles and unsatisfiable ranges are rejected at install"
    is still the contract for it.

    **Two things a module author needs to know about how the range is applied**, both
    decided by ``loader.py``'s ``_dependency_order`` at load time:

    - **A prerelease sorts below its own release, which bites at a lower bound.**
      ``">=2,<3"`` does **not** admit ``2.0.0rc1``: PEP 440 orders the release
      candidate below ``2.0.0``, so the lower bound excludes it and the load is
      refused naming both. This is not a ban on prereleases — ``">=1,<3"`` accepts
      ``2.0.0rc1`` — so the rule worth carrying is about the *boundary*, not about
      prereleases in general. Write ``">=2.0.0rc1,<3"`` when a particular release's
      prereleases are acceptable. Measured against ``packaging`` 26.3, the version
      resolved here: ``SpecifierSet.contains`` **admits** prereleases by default and
      passing ``prereleases=True`` changes no answer this code can produce, so there
      is no filtering flag in play. Older ``packaging`` did exclude them by default,
      which makes this a property of the resolved version rather than of PEP 440.
    - **``optional = True`` exempts the dependency from being present, not from the
      range.** An optional dependency this deployment did not load is skipped; one
      that *is* loaded is held to the range exactly as a required one is.
    """

    optional: bool = False


class RecordType(BaseModel):
    """One record type the module owns (``module-contract.md`` § Owned record
    types). A record type appears in exactly one manifest.

    **``authorize_delete``/``delete_owned`` are the additive owned-delete
    declaration**, and they live here rather than in a twenty-fifth manifest field
    on purpose. "A record type appears in exactly one manifest" is already the
    invariant :class:`~rheo_core.deletion.registry.OwnedDeletionRegistry` needs —
    exactly one declaration owns a record type — so hanging the pair off the type
    makes that structural: there is nowhere to write an owned-delete declaration for
    a type this manifest does not own, and nowhere to write two for one type. A
    parallel top-level tuple keyed by ``record_type`` would have made both
    expressible and then had to refuse them.

    Both default to ``None`` — the empty default every manifest written before an
    owner existed keeps — and the pair is whole or absent: a type carrying one half
    is a module that meant to own its deletion and stopped half way, which is worth
    a validation error rather than a registration the coordinator silently never
    makes. A pair is meaningful only on a ``deletable`` type, so one on a type that
    is not deletable is refused too.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    table: str
    deletable: bool
    delete_roles: frozenset[Role]
    exportable: bool
    audience_field: str | None
    authorize_delete: DeleteAuthorizer = None
    delete_owned: OwnedDeleter = None

    @model_validator(mode="after")
    def _the_owned_delete_pair_is_whole(self) -> Self:
        halves = (self.authorize_delete, self.delete_owned)
        if any(half is not None for half in halves) and not all(
            half is not None for half in halves
        ):
            raise ValueError(
                f"record type {self.name!r} declares one half of an owned-delete "
                "pair; authorize_delete and delete_owned are declared together"
            )
        if self.authorize_delete is not None and not self.deletable:
            raise ValueError(
                f"record type {self.name!r} declares an owned-delete pair but is "
                "not deletable"
            )
        return self


class StorageDeclaration(BaseModel):
    """Where the module's tables live and how they get there.

    ``schema_name`` must equal the ``module_id``; the manifest's own
    ``@model_validator`` is what holds it to that, so the resolver, the audit sink
    and the migration chain all address one place without being told twice.
    """

    model_config = ConfigDict(frozen=True)

    schema_name: str
    migrations_path: str
    required_extensions: tuple[str, ...]


_ModelClass = type[BaseModel]
"""``type[BaseModel]``, spelled once so :class:`EventDeclaration` can name it.

That class's first field is called ``type`` — the ratified name — which shadows the
builtin for a type checker reading the class body, so ``data: type[BaseModel]``
there is "Variable ... is not valid as a type" under ``mypy --strict``.
"""


class EventDeclaration(BaseModel):
    """One event type the module publishes.

    **The payload field is ``data``, not the ratified table's ``data_model``**, and
    the doc row was corrected to match rather than the other way around: ``data`` is
    the name every later run types and the one the namespacing check reads past.
    """

    model_config = ConfigDict(frozen=True)

    type: str
    schema_version: int
    data: _ModelClass


class JobKind(BaseModel):
    """One background work kind the module contributes to the worker.

    **``input_model`` is not in the ratified row and is added deliberately.**
    ``JobKindRegistry.register(kind, input_model, handler)``
    (``rheo_core/work/kinds.py``) takes exactly those three positional arguments, so
    a ``JobKind`` carrying no input model could not be registered at all. The doc
    row gained the field rather than the registry losing it.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    input_model: type[BaseModel]
    handler: JobHandler
    max_attempts: int
    cancellable: bool


class Schedule(BaseModel):
    """A per-workspace schedule row created when the module is enabled."""

    model_config = ConfigDict(frozen=True)

    name: str
    job_kind: str
    cron: str
    enabled_by_default: bool


class DeletionParticipant(BaseModel):
    """A hook the deletion coordinator calls for the named record types."""

    model_config = ConfigDict(frozen=True)

    record_types: tuple[str, ...]
    handler: DeletionHandler


class ExportDeclaration(BaseModel):
    """The module's versioned export format and the two callables that move it."""

    model_config = ConfigDict(frozen=True)

    format_version: int
    schema_path: str
    exporter: Exporter
    importer: Importer


class ConnectorBinding(BaseModel):
    """Which of the module's operations a transport connector may call.

    ``route`` is the HTTP route the binding registers on the ``api`` surface when
    the transport has one, and ``None`` otherwise.
    """

    model_config = ConfigDict(frozen=True)

    transport: str
    service_operation: str
    route: str | None


def _audit_sink(value: object) -> object:
    """The duck check ``install_sink`` applies, as a pydantic validator.

    A plain validator rather than an ``isinstance`` probe because ``AuditSink`` is a
    Protocol that is not ``@runtime_checkable``; see this module's docstring.
    """
    if callable(getattr(value, "record", None)):
        return value
    raise ValueError("an audit sink must provide record()")


class ModuleManifest(BaseModel):
    """What one distribution exposes through its ``rheo.modules`` entry point."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    module_id: str
    package_version: str
    core_contract_versions: tuple[int, ...]
    dependencies: tuple[Dependency, ...]
    record_types: tuple[RecordType, ...]
    storage: StorageDeclaration
    configuration_schema: tuple[KeySpec, ...]
    """The module's settings keys, as ``KeySpec`` values.

    **Not a pydantic model class**, which is the ratified table's word for it: the
    settings system already declares keys as ``KeySpec`` and registers them through
    ``SettingsRegistry.register(spec, *, origin)``, so a second declaration form
    would be a shape with no reader. Recorded as a deviation in the doc.
    """

    operations: tuple[tuple[OperationDeclaration, Handler], ...]
    """Each declaration with the handler that runs it.

    The handler travels beside the declaration rather than inside it because it is
    typed against ``UnitOfWork``, which ``rheo_contracts`` may not import.
    """

    tools: tuple[ToolDeclaration, ...]
    events: tuple[EventDeclaration, ...]
    subscriptions: tuple[ConsumerSubscription, ...]
    jobs: tuple[JobKind, ...]
    schedules: tuple[Schedule, ...]
    resolvers: tuple[tuple[str, RecordResolver], ...]
    deletion_participants: tuple[DeletionParticipant, ...]
    export: ExportDeclaration
    web: WebContribution | None = None
    agent_guidance: str | None = None
    secret_scopes: tuple[str, ...]
    connector_bindings: tuple[ConnectorBinding, ...]
    health_checks: tuple[HealthCheck, ...]
    contract_tests: str
    sensitivity: Sensitivity
    audit_sink: Annotated[AuditSink, PlainValidator(_audit_sink)] | None = None

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        """The five rules the 0c0 stub's ``validate()`` carried, now inside the
        model so a manifest cannot exist in a state that breaks one.

        Everything a *registration* refuses — the operation-name grammar, the
        origin/prefix agreement, reserved input fields, ``extra = "allow"`` — is
        still left to ``OperationRegistry.register`` and ``ResolverRegistry.
        register``, which the loader calls with ``origin = manifest.module_id``.
        Re-checking any of it here would be a second, weaker copy of a shipped gate.

        **The web contribution's ownership rules were added in run 1a3** (see
        :func:`_check_web_contribution`): they are here rather than on
        :class:`WebContribution` because each one reads this manifest's own id,
        operations or record types. No registration re-checks them, because nothing
        registers a web contribution: ``rheo web compose`` reads it straight off the
        manifest.
        """
        module_id = self.module_id
        if not module_id.isidentifier() or module_id != module_id.lower():
            raise ValueError(f"module_id {module_id!r}: {_SEGMENT_MESSAGE}")
        if is_reserved_module(module_id):
            raise ValueError(
                f"module_id {module_id!r} is the segment reserved for core records"
            )
        if self.storage.schema_name != module_id:
            # One module owns one schema, named for it: the resolver, the audit sink
            # and the migration all address the same place without being told twice.
            raise ValueError(
                f"storage.schema_name {self.storage.schema_name!r} is not the "
                f"module id {module_id!r}"
            )
        for spec in self.configuration_schema:
            if not spec.key.startswith(f"{module_id}."):
                raise ValueError(
                    f"configuration_schema key {spec.key!r} does not start with "
                    f"{module_id + '.'!r}"
                )
        for dependency in self.dependencies:
            try:
                SpecifierSet(dependency.version_range)
            except InvalidSpecifier as exc:
                raise ValueError(
                    f"dependencies: version_range "
                    f"{dependency.version_range!r} for {dependency.module_id!r} "
                    "is not a PEP 440 specifier set"
                ) from exc
        if self.web is not None:
            _check_web_contribution(
                self.web,
                module_id=module_id,
                operations={
                    declaration.name: declaration.safety_class
                    for declaration, _ in self.operations
                },
                record_types=frozenset(record.name for record in self.record_types),
            )
        return self
