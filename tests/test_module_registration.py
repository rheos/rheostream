"""AC 14 / FR 6: what a manifest declares reaches a live registry, and what it
declares badly does not load at all.

Seams under test: ``load_modules()``'s six-category registration contract, each
category read back **through the public registry its production consumer reads** —
``OperationRegistry``, ``ResolverRegistry``, ``ToolRegistry``, a ``JobKindRegistry``,
a ``ConsumerRegistry``, and ``rheo_core.settings``'s process-wide registry for
``configuration_schema`` — plus ``loaded_manifests()`` for declared events, which
have no registry by design. Nothing here reaches into ``_register``.

**Every expectation is a module-level constant, never a read of the manifest under
test.** A case that asserted ``tools.lookup(name).declaration is manifest.tools[0]``
would round-trip the manifest back to itself and pass against a loader that
registered nothing but ``_LOADED``. So each case names the tool, the job kind, the
consumer id, the settings key and the handler objects independently, and asks the
registry whether it holds *those*.

**One test per category, deliberately.** The checkpoint's mutation pass deletes one
``_register`` branch at a time and requires exactly that category's case to red. A
single case asserting all six would red for any of the six, which proves
``load_modules()`` ran rather than what it registered.

The fabricated-entry-point recipe is the one ``tests/test_module_loader.py`` ships and
this file borrows rather than reinvents: a real ``EntryPoint`` whose value points back
at a module-level name here, published by monkeypatching the loader's own
``entry_points``, with ``_probe``-suffixed ids so a fixture can never borrow the id of
a distribution shipped later. The twenty-four-field manifest builder is imported from
that file for the same reason ``tests/test_module_sets.py`` imports it.

**The one piece of process-wide state this file cannot hand back.** The settings
registry is process-global and publishes no unregister, so
``alpha_probe.retention_days`` stays declared for the rest of the session. It is
declared under the module's own origin (never ``core``, which
``tests/test_settings.py`` asserts the exact key set of) and carries
``explicit_per_workspace = False``, so provisioning writes no row for it. That is the
same residue a real module load leaves, which is why the loader's own docstring
records the consequence rather than filtering it.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from importlib.metadata import EntryPoint

import pytest
from pydantic import BaseModel, ConfigDict
from rheo_contracts import (
    CONTRACT_VERSION,
    AuditSpec,
    EventEnvelope,
    Idempotency,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
    ToolDeclaration,
    WorkspaceContext,
)
from rheo_core.audit import reset_sinks, sink_for
from rheo_core.audit.records import AuditOutcome
from rheo_core.events.consumers import ConsumerRegistry, ConsumerSubscription
from rheo_core.modules import (
    ENTRY_POINT_GROUP,
    Dependency,
    EventDeclaration,
    JobKind,
    ManifestInvalid,
    load_modules,
    loaded_in_dependency_order,
    loaded_manifests,
    reset_surfaces,
)
from rheo_core.modules import loader as loader_module
from rheo_core.modules.loader import ALLOWLIST_KEY
from rheo_core.operations import OperationRegistry
from rheo_core.refs.resolver import (
    NOT_FOUND,
    RecordHead,
    ResolverRegistry,
    Unavailable,
)
from rheo_core.settings import (
    REGISTRY as SETTINGS_REGISTRY,
)
from rheo_core.settings import (
    KeySpec,
    Scope,
    ValueType,
    env_variable_names,
    spec_for,
)
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.tokens.sets import ToolRegistry
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.kinds import JobKindRegistry
from test_module_loader import _manifest

MODULES_VARIABLE = env_variable_names(ALLOWLIST_KEY)[0]

MODULE_ID = "alpha_probe"
OPERATION = f"{MODULE_ID}.note.add"
RECORD_TYPE = "note"
TOOL_NAME = f"{MODULE_ID}_add_note"
JOB_KIND = f"{MODULE_ID}.reindex"
CONSUMER_ID = f"{MODULE_ID}.note.indexer"
EVENT_TYPE = f"{MODULE_ID}.note.created"
SETTING_KEY = f"{MODULE_ID}.retention_days"


# --- what the fabricated module declares ----------------------------------------------


class _ProbeInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    body: str


class _ProbeOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    body: str


class _ProbeJobInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    since: int


class _ProbeEventData(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str


def _operation_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: _ProbeInput
) -> _ProbeOutput:
    return _ProbeOutput(body=model_input.body)


def _resolver(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> RecordHead | Unavailable:
    return Unavailable(ref.format(), NOT_FOUND)


def _job_handler(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    """Never run: no case here dispatches a job."""
    return None


def _consumer_handler(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
    """Never run: no case here delivers an event."""
    return None


class _ProbeSink:
    def record(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        operation: str,
        safety_class: SafetyClass,
        subject_ref: RecordRef | None,
        request_digest: bytes,
        outcome: AuditOutcome,
        operation_id: object,
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
TOOL = ToolDeclaration(
    name=TOOL_NAME,
    safety_class=SafetyClass.MUTATE,
    operation=OPERATION,
    input_model=_ProbeInput,
)
JOB = JobKind(
    name=JOB_KIND,
    input_model=_ProbeJobInput,
    handler=_job_handler,
    max_attempts=3,
    cancellable=True,
)
SUBSCRIPTION = ConsumerSubscription(
    consumer_id=CONSUMER_ID,
    event_type=EVENT_TYPE,
    module_id=MODULE_ID,
    replay_safe=True,
    handler=_consumer_handler,
)
EVENT = EventDeclaration(type=EVENT_TYPE, schema_version=1, data=_ProbeEventData)
SETTING = KeySpec(
    key=SETTING_KEY,
    type=ValueType.INT,
    scope=Scope.WORKSPACE,
    floor=None,
    explicit_per_workspace=False,
    default=30,
)
SINK = _ProbeSink()

ALL_POINTS_MANIFEST = _manifest(
    MODULE_ID,
    operations=((DECLARATION, _operation_handler),),
    resolvers=((RECORD_TYPE, _resolver),),
    tools=(TOOL,),
    jobs=(JOB,),
    subscriptions=(SUBSCRIPTION,),
    events=(EVENT,),
    configuration_schema=(SETTING,),
    audit_sink=SINK,
)
"""One manifest declaring exactly one of each of the six extension points, one event
and one audit sink — so every case below loads the same declaration set and differs
only in which registry it interrogates."""

ALL_POINTS_ENTRY_POINT = EntryPoint(
    name=MODULE_ID, value=f"{__name__}:ALL_POINTS_MANIFEST", group=ENTRY_POINT_GROUP
)


# --- fixtures -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Registries:
    """The five registries a load is driven against, all local to one test.

    The two the production ``core`` process leaves at ``None`` (``kinds``,
    ``consumers``) are supplied here because this file is asserting what reaches
    them; ``tests/test_worker_job_kinds.py`` is where the production pair is driven.
    """

    operations: OperationRegistry
    resolvers: ResolverRegistry
    tools: ToolRegistry
    kinds: JobKindRegistry
    consumers: ConsumerRegistry


@pytest.fixture(autouse=True)
def clean_process_state() -> Iterator[None]:
    """The loader's ``_LOADED`` table and the sink table are process-wide."""
    reset_surfaces()
    reset_sinks()
    yield
    reset_surfaces()
    reset_sinks()


@pytest.fixture
def registries() -> _Registries:
    return _Registries(
        operations=OperationRegistry(),
        resolvers=ResolverRegistry(),
        tools=ToolRegistry(),
        kinds=JobKindRegistry(),
        consumers=ConsumerRegistry(),
    )


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


def _load(
    monkeypatch: pytest.MonkeyPatch,
    registries: _Registries,
    *entry_points: EntryPoint,
) -> tuple[str, ...]:
    """Publish ``entry_points``, allow every name they carry, and load.

    Allowing exactly the published names keeps each case's allowlist out of its own
    way: what these cases are about is what happens *after* the gate, and the gate
    itself is ``tests/test_module_loader.py``'s.
    """
    _publish(monkeypatch, *entry_points)
    _install(monkeypatch, *(entry_point.name for entry_point in entry_points))
    return load_modules(
        registry=registries.operations,
        resolvers=registries.resolvers,
        tools=registries.tools,
        kinds=registries.kinds,
        consumers=registries.consumers,
    )


# --- the six extension points, one case each ------------------------------------------


def test_a_declared_operation_reaches_the_operation_registry(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    assert _load(monkeypatch, registries, ALL_POINTS_ENTRY_POINT) == (MODULE_ID,)

    assert registries.operations.names() == frozenset({OPERATION})
    registered = registries.operations.lookup(OPERATION)
    assert registered is not None
    assert registered.declaration is DECLARATION
    assert registered.origin == MODULE_ID


def test_a_declared_resolver_reaches_the_resolver_registry(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    assert _load(monkeypatch, registries, ALL_POINTS_ENTRY_POINT) == (MODULE_ID,)

    assert registries.resolvers.record_types() == frozenset(
        {f"{MODULE_ID}.{RECORD_TYPE}"}
    )
    assert registries.resolvers.lookup(MODULE_ID, RECORD_TYPE) is _resolver


def test_a_declared_tool_reaches_the_tool_registry(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """The registry the MCP façade lists from and ``agent_default`` reads, not the
    manifest's own tuple."""
    assert _load(monkeypatch, registries, ALL_POINTS_ENTRY_POINT) == (MODULE_ID,)

    assert registries.tools.names() == frozenset({TOOL_NAME})
    registered = registries.tools.lookup(TOOL_NAME)
    assert registered is not None
    assert registered.declaration is TOOL
    # Through the shipped ``register``, with the manifest's id as the origin — so the
    # reserved-field, ``extra = "allow"`` and non-token-issuable refusals all applied.
    assert registered.origin == MODULE_ID


def test_a_declared_job_kind_reaches_the_job_kind_registry(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """Read through ``lookup()``, which is what ``worker_loop`` calls to decide what
    a stored job row is and who runs it."""
    assert _load(monkeypatch, registries, ALL_POINTS_ENTRY_POINT) == (MODULE_ID,)

    assert registries.kinds.names() == frozenset({JOB_KIND})
    input_model, handler = registries.kinds.lookup(JOB_KIND)
    assert input_model is _ProbeJobInput
    assert handler is _job_handler


def test_a_declared_subscription_reaches_the_consumer_registry(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """Read through both accessors the fan-out uses: ``for_type`` at publish time and
    ``lookup`` when a delivery row names its consumer."""
    assert _load(monkeypatch, registries, ALL_POINTS_ENTRY_POINT) == (MODULE_ID,)

    assert registries.consumers.lookup(CONSUMER_ID) is SUBSCRIPTION
    assert registries.consumers.for_type(EVENT_TYPE) == (SUBSCRIPTION,)


def test_a_declared_settings_key_reaches_the_settings_registry(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """The process-wide settings registry, read back through ``spec_for`` — the same
    accessor the write path and ``rheo doctor`` use — and its recorded origin.

    Unlike the five above, this registry is not a parameter of ``load_modules()``:
    there is one declaration table per process and the loader writes into it, which
    is exactly the coupling the loader's docstring records.
    """
    assert _load(monkeypatch, registries, ALL_POINTS_ENTRY_POINT) == (MODULE_ID,)

    assert spec_for(SETTING_KEY) == SETTING
    assert SETTINGS_REGISTRY.origin_of(SETTING_KEY) == MODULE_ID
    # Declared, but not one provisioning writes a row for: see the module docstring.
    assert SETTING not in SETTINGS_REGISTRY.explicit_per_workspace()


def test_a_declared_event_is_readable_from_the_loaded_manifest(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """The one extension point with no registry. ``rheo_core.events`` indexes
    subscriptions, not declarations, and this run builds no second index — so the
    loader's own per-module record is where a declared event is read back."""
    assert _load(monkeypatch, registries, ALL_POINTS_ENTRY_POINT) == (MODULE_ID,)

    loaded = loaded_manifests()
    assert set(loaded) == {MODULE_ID}
    assert loaded[MODULE_ID].events == (EVENT,)
    assert [event.type for event in loaded[MODULE_ID].events] == [EVENT_TYPE]


def test_a_declared_audit_sink_installs_under_the_module_id(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """By identity, and under the manifest's own id — the dispatcher resolves a sink
    by the *operation's* owning module, so a sink under any other key would fire on
    somebody else's operations."""
    assert _load(monkeypatch, registries, ALL_POINTS_ENTRY_POINT) == (MODULE_ID,)

    assert sink_for(MODULE_ID) is SINK
    assert sink_for("core") is None


def test_job_kinds_and_subscriptions_are_skipped_when_no_registry_is_supplied(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """The ``core`` process's call, which passes neither ``kinds`` nor ``consumers``.

    Paired with the two cases above, which load the *same* manifest with both
    supplied: a loader that never registered a job kind at all would satisfy this on
    its own.
    """
    _publish(monkeypatch, ALL_POINTS_ENTRY_POINT)
    _install(monkeypatch, MODULE_ID)

    loaded_ids = load_modules(
        registry=registries.operations,
        resolvers=registries.resolvers,
        tools=registries.tools,
    )
    assert loaded_ids == (MODULE_ID,)

    # The four categories with a registry still registered ...
    assert registries.operations.names() == frozenset({OPERATION})
    assert registries.tools.names() == frozenset({TOOL_NAME})
    # ... and the two without one registered nothing, rather than raising.
    assert registries.kinds.names() == frozenset()
    assert registries.consumers.for_type(EVENT_TYPE) == ()


# --- the manifest-to-registration cross-check, as a property --------------------------

CROSS_CHECK_ID = "crosscheck_probe"
CROSS_CHECK_OPERATION = f"{CROSS_CHECK_ID}.thing.add"
CROSS_CHECK_TOOL_NAME = f"{CROSS_CHECK_ID}_add_thing"
CROSS_CHECK_CONSUMER_ID = f"{CROSS_CHECK_ID}.thing.indexer"
CROSS_CHECK_DECLARATION = OperationDeclaration(
    name=CROSS_CHECK_OPERATION,
    safety_class=SafetyClass.MUTATE,
    roles=frozenset({Role.OWNER}),
    input_model=_ProbeInput,
    output=_ProbeOutput,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)
CROSS_CHECK_MANIFEST = _manifest(
    CROSS_CHECK_ID,
    operations=((CROSS_CHECK_DECLARATION, _operation_handler),),
    resolvers=(("thing", _resolver),),
    tools=(
        ToolDeclaration(
            name=CROSS_CHECK_TOOL_NAME,
            safety_class=SafetyClass.MUTATE,
            operation=CROSS_CHECK_OPERATION,
            input_model=_ProbeInput,
        ),
    ),
    subscriptions=(
        ConsumerSubscription(
            consumer_id=CROSS_CHECK_CONSUMER_ID,
            event_type=f"{CROSS_CHECK_ID}.thing.created",
            module_id=CROSS_CHECK_ID,
            replay_safe=False,
            handler=_consumer_handler,
        ),
    ),
)
CROSS_CHECK_ENTRY_POINT = EntryPoint(
    name=CROSS_CHECK_ID,
    value=f"{__name__}:CROSS_CHECK_MANIFEST",
    group=ENTRY_POINT_GROUP,
)


def test_every_registration_from_a_loaded_module_is_in_that_modules_manifest(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """Discovery step 4 as a property rather than a mechanism.

    ``module-contract.md`` asks for a cross-check between what a module registers and
    what its manifest declares. In this tree that check has nothing to compare
    against: there is no author-supplied ``register`` function, only ``_register``
    iterating the manifest's own tuples, so both directions are unreachable by
    construction. This asserts the property instead of building a second mechanism
    for it — over two modules at once, so "registered by the wrong module" is
    expressible and would be caught, and in **both** directions, so a loader that
    registered nothing would fail the declared-but-not-registered half.
    """
    assert _load(
        monkeypatch, registries, ALL_POINTS_ENTRY_POINT, CROSS_CHECK_ENTRY_POINT
    ) == (MODULE_ID, CROSS_CHECK_ID)

    loaded = loaded_manifests()
    assert set(loaded) == {MODULE_ID, CROSS_CHECK_ID}

    for module_id, manifest in loaded.items():
        registered_operations = {
            name
            for name in registries.operations.names()
            if (found := registries.operations.lookup(name)) is not None
            and found.origin == module_id
        }
        assert registered_operations == {
            declaration.name for declaration, _ in manifest.operations
        }

        registered_tools = {
            name
            for name in registries.tools.names()
            if (tool := registries.tools.lookup(name)) is not None
            and tool.origin == module_id
        }
        assert registered_tools == {tool.name for tool in manifest.tools}

        registered_record_types = {
            record_type
            for record_type in registries.resolvers.record_types()
            if record_type.startswith(f"{module_id}.")
        }
        assert registered_record_types == {
            f"{module_id}.{record_type}" for record_type, _ in manifest.resolvers
        }

        registered_consumers: set[str] = set()
        for declared in manifest.subscriptions:
            registered_consumers.update(
                subscription.consumer_id
                for subscription in registries.consumers.for_type(declared.event_type)
                if subscription.module_id == module_id
            )
        assert registered_consumers == {
            subscription.consumer_id for subscription in manifest.subscriptions
        }


# --- AC 14's two event refusals -------------------------------------------------------

MISNAMED_EVENT_ID = "misnamed_probe"
FOREIGN_EVENT_TYPE = "beta_probe.note.created"
MISNAMED_EVENT_MANIFEST = _manifest(
    MISNAMED_EVENT_ID,
    events=(
        EventDeclaration(
            type=FOREIGN_EVENT_TYPE, schema_version=1, data=_ProbeEventData
        ),
    ),
)
MISNAMED_EVENT_ENTRY_POINT = EntryPoint(
    name=MISNAMED_EVENT_ID,
    value=f"{__name__}:MISNAMED_EVENT_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

TWIN_EVENT = EventDeclaration(type=EVENT_TYPE, schema_version=1, data=_ProbeEventData)
TWIN_OTHER_EVENT = EventDeclaration(
    type=f"{MODULE_ID}.note.archived", schema_version=1, data=_ProbeEventData
)
TWIN_ONE_MANIFEST = _manifest(MODULE_ID, events=(TWIN_EVENT,))
TWIN_TWO_MANIFEST = _manifest(MODULE_ID, events=(TWIN_EVENT,))
TWIN_TWO_DISTINCT_MANIFEST = _manifest(MODULE_ID, events=(TWIN_OTHER_EVENT,))
"""Two distributions publishing under one ``module_id``, which is the only shape that
reaches the duplicate-event rule — see the loader's ``_check_events`` docstring. The
third is the same pair with a different second type, so the pair below can show the
rule is about the *type* and not about the repeated id."""

TWIN_ONE_ENTRY_POINT = EntryPoint(
    name=MODULE_ID, value=f"{__name__}:TWIN_ONE_MANIFEST", group=ENTRY_POINT_GROUP
)
TWIN_TWO_ENTRY_POINT = EntryPoint(
    name=MODULE_ID, value=f"{__name__}:TWIN_TWO_MANIFEST", group=ENTRY_POINT_GROUP
)
TWIN_TWO_DISTINCT_ENTRY_POINT = EntryPoint(
    name=MODULE_ID,
    value=f"{__name__}:TWIN_TWO_DISTINCT_MANIFEST",
    group=ENTRY_POINT_GROUP,
)


def test_an_event_not_namespaced_under_its_own_module_refuses_the_load(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """AC 14's "namespaced under the module's own id"."""
    with pytest.raises(ManifestInvalid) as excinfo:
        _load(monkeypatch, registries, MISNAMED_EVENT_ENTRY_POINT)

    assert FOREIGN_EVENT_TYPE in str(excinfo.value)
    assert excinfo.value.module_id == MISNAMED_EVENT_ID
    assert loaded_manifests() == {}


def test_two_manifests_declaring_one_event_type_refuse_the_load(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """AC 14's "rejected as a duplicate against another loaded module's event of the
    same type", through the only shape that can reach it.

    Two entry points under one name, two distinct manifests, both carrying
    ``module_id = "alpha_probe"`` and both declaring ``alpha_probe.note.created``.
    Both clear the entry-point-name check and both clear namespacing, so the
    duplicate rule is the only thing left to refuse them. Written with two *different*
    module ids this case would assert the namespacing refusal instead, while reading
    as though it asserted this one.
    """
    with pytest.raises(ManifestInvalid) as excinfo:
        _load(monkeypatch, registries, TWIN_ONE_ENTRY_POINT, TWIN_TWO_ENTRY_POINT)

    assert EVENT_TYPE in str(excinfo.value)
    assert MODULE_ID in str(excinfo.value)
    assert loaded_manifests() == {}


def test_the_same_pair_declaring_different_types_loads(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """The refusal above is about the *type*, not about the repeated module id.

    Without this half, the duplicate case passes against a loader that refuses any
    manifest whose id it has already seen. Both entry points load, so the returned
    tuple carries the id twice: two distributions really were loaded, and the record
    says so rather than silently collapsing them.
    """
    loaded_ids = _load(
        monkeypatch,
        registries,
        TWIN_ONE_ENTRY_POINT,
        TWIN_TWO_DISTINCT_ENTRY_POINT,
    )

    assert loaded_ids == (MODULE_ID, MODULE_ID)
    assert set(loaded_manifests()) == {MODULE_ID}


# --- the contract-version gate --------------------------------------------------------

STALE_CONTRACT_ID = "stale_contract_probe"
FUTURE_CONTRACT_VERSION = CONTRACT_VERSION + 1
STALE_CONTRACT_MANIFEST = _manifest(
    STALE_CONTRACT_ID, core_contract_versions=(FUTURE_CONTRACT_VERSION,)
)
STALE_CONTRACT_ENTRY_POINT = EntryPoint(
    name=STALE_CONTRACT_ID,
    value=f"{__name__}:STALE_CONTRACT_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

CURRENT_CONTRACT_ID = "current_contract_probe"
CURRENT_CONTRACT_MANIFEST = _manifest(
    CURRENT_CONTRACT_ID, core_contract_versions=(CONTRACT_VERSION,)
)
CURRENT_CONTRACT_ENTRY_POINT = EntryPoint(
    name=CURRENT_CONTRACT_ID,
    value=f"{__name__}:CURRENT_CONTRACT_MANIFEST",
    group=ENTRY_POINT_GROUP,
)


def test_a_manifest_that_omits_the_running_contract_version_is_refused(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    with pytest.raises(ManifestInvalid) as excinfo:
        _load(monkeypatch, registries, STALE_CONTRACT_ENTRY_POINT)

    message = str(excinfo.value)
    # Both versions named: what the module asked for and what this core publishes.
    assert str(FUTURE_CONTRACT_VERSION) in message
    assert str(CONTRACT_VERSION) in message
    assert loaded_manifests() == {}


def test_a_manifest_naming_the_running_contract_version_loads(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """The gate's other half. Without it the case above passes against a gate that
    refuses every manifest."""
    assert _load(monkeypatch, registries, CURRENT_CONTRACT_ENTRY_POINT) == (
        CURRENT_CONTRACT_ID,
    )


# --- dependency ranges, ordering and cycles -------------------------------------------

PROVIDER_ID = "provider_probe"
PROVIDER_VERSION = "1.4.0"
PROVIDER_MANIFEST = _manifest(PROVIDER_ID, package_version=PROVIDER_VERSION)
PROVIDER_ENTRY_POINT = EntryPoint(
    name=PROVIDER_ID, value=f"{__name__}:PROVIDER_MANIFEST", group=ENTRY_POINT_GROUP
)

UNSATISFIED_ID = "unsatisfied_probe"
UNSATISFIED_RANGE = ">=2,<3"
UNSATISFIED_MANIFEST = _manifest(
    UNSATISFIED_ID,
    dependencies=(Dependency(module_id=PROVIDER_ID, version_range=UNSATISFIED_RANGE),),
)
UNSATISFIED_ENTRY_POINT = EntryPoint(
    name=UNSATISFIED_ID,
    value=f"{__name__}:UNSATISFIED_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

MISSING_REQUIRED_ID = "missing_required_probe"
MISSING_REQUIRED_MANIFEST = _manifest(
    MISSING_REQUIRED_ID,
    dependencies=(Dependency(module_id="absent_probe", version_range=">=1"),),
)
MISSING_REQUIRED_ENTRY_POINT = EntryPoint(
    name=MISSING_REQUIRED_ID,
    value=f"{__name__}:MISSING_REQUIRED_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

MISSING_OPTIONAL_ID = "missing_optional_probe"
MISSING_OPTIONAL_MANIFEST = _manifest(
    MISSING_OPTIONAL_ID,
    dependencies=(
        Dependency(module_id="absent_probe", version_range=">=1", optional=True),
    ),
)
MISSING_OPTIONAL_ENTRY_POINT = EntryPoint(
    name=MISSING_OPTIONAL_ID,
    value=f"{__name__}:MISSING_OPTIONAL_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

# The two optional-dependency cases that are NOT about absence. `optional = True`
# exempts the dependency from having to be there; it does not exempt it from the
# declared range once it is.
OPTIONAL_IN_RANGE_ID = "optional_in_range_probe"
OPTIONAL_IN_RANGE_MANIFEST = _manifest(
    OPTIONAL_IN_RANGE_ID,
    dependencies=(
        Dependency(module_id=PROVIDER_ID, version_range=">=1,<2", optional=True),
    ),
)
OPTIONAL_IN_RANGE_ENTRY_POINT = EntryPoint(
    name=OPTIONAL_IN_RANGE_ID,
    value=f"{__name__}:OPTIONAL_IN_RANGE_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

OPTIONAL_OUT_OF_RANGE_ID = "optional_out_of_range_probe"
OPTIONAL_OUT_OF_RANGE_MANIFEST = _manifest(
    OPTIONAL_OUT_OF_RANGE_ID,
    dependencies=(
        Dependency(
            module_id=PROVIDER_ID, version_range=UNSATISFIED_RANGE, optional=True
        ),
    ),
)
OPTIONAL_OUT_OF_RANGE_ENTRY_POINT = EntryPoint(
    name=OPTIONAL_OUT_OF_RANGE_ID,
    value=f"{__name__}:OPTIONAL_OUT_OF_RANGE_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

# PEP 440 excludes prereleases from a range unless the range names one.
PRERELEASE_PROVIDER_ID = "prerelease_provider_probe"
PRERELEASE_VERSION = "2.0.0rc1"
PRERELEASE_PROVIDER_MANIFEST = _manifest(
    PRERELEASE_PROVIDER_ID, package_version=PRERELEASE_VERSION
)
PRERELEASE_PROVIDER_ENTRY_POINT = EntryPoint(
    name=PRERELEASE_PROVIDER_ID,
    value=f"{__name__}:PRERELEASE_PROVIDER_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

NEEDS_STABLE_TWO_ID = "needs_stable_two_probe"
NEEDS_STABLE_TWO_MANIFEST = _manifest(
    NEEDS_STABLE_TWO_ID,
    dependencies=(
        Dependency(module_id=PRERELEASE_PROVIDER_ID, version_range=">=2,<3"),
    ),
)
NEEDS_STABLE_TWO_ENTRY_POINT = EntryPoint(
    name=NEEDS_STABLE_TWO_ID,
    value=f"{__name__}:NEEDS_STABLE_TWO_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

ACCEPTS_PRERELEASE_ID = "accepts_prerelease_probe"
ACCEPTS_PRERELEASE_MANIFEST = _manifest(
    ACCEPTS_PRERELEASE_ID,
    dependencies=(
        Dependency(module_id=PRERELEASE_PROVIDER_ID, version_range=">=2.0.0rc1,<3"),
    ),
)
ACCEPTS_PRERELEASE_ENTRY_POINT = EntryPoint(
    name=ACCEPTS_PRERELEASE_ID,
    value=f"{__name__}:ACCEPTS_PRERELEASE_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

SHARED_EVENT_TYPE = "core.probe.happened"
ORDERED_PROVIDER_ID = "ordered_provider_probe"
ORDERED_DEPENDENT_ID = "ordered_dependent_probe"


def _ordering_subscription(module_id: str) -> ConsumerSubscription:
    """A subscription whose only job is to record when its module registered.

    ``ConsumerRegistry.for_type`` answers "in registration order", so two modules
    subscribed to one event type make the registration order observable through a
    shipped accessor rather than through a probe the loader would have to grow.
    """
    return ConsumerSubscription(
        consumer_id=f"{module_id}.order",
        event_type=SHARED_EVENT_TYPE,
        module_id=module_id,
        replay_safe=True,
        handler=_consumer_handler,
    )


ORDERED_PROVIDER_MANIFEST = _manifest(
    ORDERED_PROVIDER_ID,
    package_version=PROVIDER_VERSION,
    subscriptions=(_ordering_subscription(ORDERED_PROVIDER_ID),),
)
ORDERED_DEPENDENT_MANIFEST = _manifest(
    ORDERED_DEPENDENT_ID,
    dependencies=(Dependency(module_id=ORDERED_PROVIDER_ID, version_range=">=1,<2"),),
    subscriptions=(_ordering_subscription(ORDERED_DEPENDENT_ID),),
)
ORDERED_PROVIDER_ENTRY_POINT = EntryPoint(
    name=ORDERED_PROVIDER_ID,
    value=f"{__name__}:ORDERED_PROVIDER_MANIFEST",
    group=ENTRY_POINT_GROUP,
)
ORDERED_DEPENDENT_ENTRY_POINT = EntryPoint(
    name=ORDERED_DEPENDENT_ID,
    value=f"{__name__}:ORDERED_DEPENDENT_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

CYCLE_A_ID = "cycle_a_probe"
CYCLE_B_ID = "cycle_b_probe"
CYCLE_A_MANIFEST = _manifest(
    CYCLE_A_ID,
    package_version=PROVIDER_VERSION,
    dependencies=(Dependency(module_id=CYCLE_B_ID, version_range=">=1"),),
)
CYCLE_B_MANIFEST = _manifest(
    CYCLE_B_ID,
    package_version=PROVIDER_VERSION,
    dependencies=(Dependency(module_id=CYCLE_A_ID, version_range=">=1"),),
)
CYCLE_A_ENTRY_POINT = EntryPoint(
    name=CYCLE_A_ID, value=f"{__name__}:CYCLE_A_MANIFEST", group=ENTRY_POINT_GROUP
)
CYCLE_B_ENTRY_POINT = EntryPoint(
    name=CYCLE_B_ID, value=f"{__name__}:CYCLE_B_MANIFEST", group=ENTRY_POINT_GROUP
)


def test_a_dependency_outside_its_declared_range_refuses_the_load(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    with pytest.raises(ManifestInvalid) as excinfo:
        _load(monkeypatch, registries, UNSATISFIED_ENTRY_POINT, PROVIDER_ENTRY_POINT)

    message = str(excinfo.value)
    assert UNSATISFIED_ID in message
    assert PROVIDER_ID in message
    assert UNSATISFIED_RANGE in message
    assert PROVIDER_VERSION in message
    assert loaded_manifests() == {}


def test_a_required_dependency_the_deployment_did_not_load_refuses_the_load(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    with pytest.raises(ManifestInvalid) as excinfo:
        _load(monkeypatch, registries, MISSING_REQUIRED_ENTRY_POINT)

    message = str(excinfo.value)
    assert MISSING_REQUIRED_ID in message
    assert "absent_probe" in message


def test_an_optional_dependency_the_deployment_did_not_load_is_skipped(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """Paired with the case above against the same absent module id: the only
    difference is ``optional = True``, so this is the flag's own behaviour rather
    than a loader that never checks."""
    assert _load(monkeypatch, registries, MISSING_OPTIONAL_ENTRY_POINT) == (
        MISSING_OPTIONAL_ID,
    )


def test_an_optional_dependency_that_is_present_and_in_range_loads(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """The middle of the three optional cases, and the one that keeps the third
    honest: without it, "optional + out of range refuses" would also be satisfied by
    a loader that refused every optional dependency it could actually see."""
    assert _load(
        monkeypatch, registries, OPTIONAL_IN_RANGE_ENTRY_POINT, PROVIDER_ENTRY_POINT
    ) == (OPTIONAL_IN_RANGE_ID, PROVIDER_ID)


def test_an_optional_dependency_that_is_present_and_out_of_range_is_refused(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """**Deviation from the ratified text, pinned deliberately.** Task 3 scopes the
    range check to required dependencies. ``optional = True`` says the dependency may
    be *absent*; it does not say a declared range stops meaning anything when the
    module is right there. A manifest that named a range has told us which versions
    it can work beside, and loading it against one it excluded would be using the
    author's own information to ignore them.

    Read with the two cases above, the three of them isolate the flag: absent loads,
    present and in range loads, present and out of range refuses — naming both
    modules and the range, exactly as a required dependency does.
    """
    with pytest.raises(ManifestInvalid) as excinfo:
        _load(
            monkeypatch,
            registries,
            OPTIONAL_OUT_OF_RANGE_ENTRY_POINT,
            PROVIDER_ENTRY_POINT,
        )

    message = str(excinfo.value)
    assert OPTIONAL_OUT_OF_RANGE_ID in message
    assert PROVIDER_ID in message
    assert UNSATISFIED_RANGE in message
    assert PROVIDER_VERSION in message


def test_a_prerelease_does_not_satisfy_a_dependency_range(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """``SpecifierSet.contains`` excludes prereleases and nothing here overrides it.

    PEP 440's own default rather than a decision this run made, which is why the
    behaviour is documented on ``Dependency.version_range`` instead of being changed.
    A module author who means to accept one writes the prerelease into the range.
    """
    with pytest.raises(ManifestInvalid) as excinfo:
        _load(
            monkeypatch,
            registries,
            NEEDS_STABLE_TWO_ENTRY_POINT,
            PRERELEASE_PROVIDER_ENTRY_POINT,
        )

    assert PRERELEASE_VERSION in str(excinfo.value)

    # The same pair with the prerelease written into the range loads, so the refusal
    # above is the prerelease rule and not the provider being unreachable.
    assert _load(
        monkeypatch,
        registries,
        ACCEPTS_PRERELEASE_ENTRY_POINT,
        PRERELEASE_PROVIDER_ENTRY_POINT,
    ) == (ACCEPTS_PRERELEASE_ID, PRERELEASE_PROVIDER_ID)


def test_the_dependency_order_is_readable_and_survives_a_second_load(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """``loaded_in_dependency_order()`` is how a later caller reaches the order.

    ``load_modules()`` answers alphabetically — ``apps/core``'s startup report and
    ``rheo openapi`` read that and neither wants an ordering that shifts when an edge
    is added — so the registration order it computes would otherwise leave no trace.
    A module migration chain that means "the manifest's dependency-sorted order" has
    to be able to read it rather than derive a second one.

    The dependent is published first, so discovery order and dependency order
    disagree and an accessor that just handed back ``_LOADED``'s insertion order
    would answer the wrong way round. The load is then repeated, because
    ``apps/core``'s lifespan loads more than once in one process and an order cached
    at registration time would answer for whichever set was current when it was
    written.
    """
    ids = _load(
        monkeypatch,
        registries,
        ORDERED_DEPENDENT_ENTRY_POINT,
        ORDERED_PROVIDER_ENTRY_POINT,
    )
    assert ids == (ORDERED_DEPENDENT_ID, ORDERED_PROVIDER_ID)

    expected = [ORDERED_PROVIDER_ID, ORDERED_DEPENDENT_ID]
    assert [manifest.module_id for manifest in loaded_in_dependency_order()] == expected

    _load(
        monkeypatch,
        registries,
        ORDERED_DEPENDENT_ENTRY_POINT,
        ORDERED_PROVIDER_ENTRY_POINT,
    )
    assert [manifest.module_id for manifest in loaded_in_dependency_order()] == expected


def test_a_satisfied_dependency_registers_before_its_dependent(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """The topological sort, observed through ``ConsumerRegistry.for_type``.

    The dependent is published **first**, so discovery order and dependency order
    disagree. A loader that registered in discovery order would answer
    ``[dependent, provider]`` here.
    """
    assert _load(
        monkeypatch,
        registries,
        ORDERED_DEPENDENT_ENTRY_POINT,
        ORDERED_PROVIDER_ENTRY_POINT,
    ) == (ORDERED_DEPENDENT_ID, ORDERED_PROVIDER_ID)

    registered_order = [
        subscription.module_id
        for subscription in registries.consumers.for_type(SHARED_EVENT_TYPE)
    ]
    assert registered_order == [ORDERED_PROVIDER_ID, ORDERED_DEPENDENT_ID]


def test_a_dependency_cycle_refuses_the_load_naming_both_modules(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    with pytest.raises(ManifestInvalid) as excinfo:
        _load(monkeypatch, registries, CYCLE_A_ENTRY_POINT, CYCLE_B_ENTRY_POINT)

    message = str(excinfo.value)
    assert CYCLE_A_ID in message
    assert CYCLE_B_ID in message
    assert loaded_manifests() == {}


# --- a refusal leaves every registry untouched ----------------------------------------
#
# The canary is a wholly valid module declaring five registrable things plus a sink.
# It is published FIRST in every refusal case below, so under a loader that registered
# as it walked, it would be fully in the registries by the time the offender behind it
# was refused. It declares no `configuration_schema`: the settings registry is
# process-global and publishes no unregister, so "absent after a refusal" is not a
# claim any one test in a shared session can make about it.

CANARY_ID = "canary_probe"
CANARY_VERSION = "1.0.0"
CANARY_OPERATION = f"{CANARY_ID}.note.add"
CANARY_TOOL_NAME = f"{CANARY_ID}_add_note"
CANARY_JOB_KIND = f"{CANARY_ID}.sweep"
CANARY_DECLARATION = OperationDeclaration(
    name=CANARY_OPERATION,
    safety_class=SafetyClass.MUTATE,
    roles=frozenset({Role.OWNER}),
    input_model=_ProbeInput,
    output=_ProbeOutput,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)
CANARY_SINK = _ProbeSink()
CANARY_MANIFEST = _manifest(
    CANARY_ID,
    package_version=CANARY_VERSION,
    operations=((CANARY_DECLARATION, _operation_handler),),
    resolvers=((RECORD_TYPE, _resolver),),
    tools=(
        ToolDeclaration(
            name=CANARY_TOOL_NAME,
            safety_class=SafetyClass.MUTATE,
            operation=CANARY_OPERATION,
            input_model=_ProbeInput,
        ),
    ),
    jobs=(
        JobKind(
            name=CANARY_JOB_KIND,
            input_model=_ProbeJobInput,
            handler=_job_handler,
            max_attempts=1,
            cancellable=False,
        ),
    ),
    subscriptions=(
        ConsumerSubscription(
            consumer_id=f"{CANARY_ID}.order",
            event_type=SHARED_EVENT_TYPE,
            module_id=CANARY_ID,
            replay_safe=True,
            handler=_consumer_handler,
        ),
    ),
    audit_sink=CANARY_SINK,
)
CANARY_ENTRY_POINT = EntryPoint(
    name=CANARY_ID, value=f"{__name__}:CANARY_MANIFEST", group=ENTRY_POINT_GROUP
)

BORROWED_SUB_ID = "borrowed_sub_probe"
BORROWED_SUB_MANIFEST = _manifest(
    BORROWED_SUB_ID,
    subscriptions=(
        ConsumerSubscription(
            consumer_id=f"{BORROWED_SUB_ID}.order",
            event_type=SHARED_EVENT_TYPE,
            # Somebody else's module id, which the registry itself would store
            # without a word.
            module_id=CANARY_ID,
            replay_safe=True,
            handler=_consumer_handler,
        ),
    ),
)
BORROWED_SUB_ENTRY_POINT = EntryPoint(
    name=BORROWED_SUB_ID,
    value=f"{__name__}:BORROWED_SUB_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

UNSATISFIED_ON_CANARY_ID = "needs_canary_probe"
UNSATISFIED_ON_CANARY_RANGE = ">=2,<3"
UNSATISFIED_ON_CANARY_MANIFEST = _manifest(
    UNSATISFIED_ON_CANARY_ID,
    dependencies=(
        Dependency(module_id=CANARY_ID, version_range=UNSATISFIED_ON_CANARY_RANGE),
    ),
)
UNSATISFIED_ON_CANARY_ENTRY_POINT = EntryPoint(
    name=UNSATISFIED_ON_CANARY_ID,
    value=f"{__name__}:UNSATISFIED_ON_CANARY_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

# The three remaining ManifestInvalid raise sites, so the matrix below covers all
# nine rather than the six it started with. All three refuse during collection,
# which is why the property is expected to hold for them — expected is not checked,
# so they get a row each.
IMPOSTOR_ID = "impostor_probe"
NOT_A_MANIFEST = object()
"""A real, importable module-level object that is simply not a ``ModuleManifest``,
so the entry point resolves for real and the refusal is ``_as_manifest``'s rather
than an import error's."""

IMPOSTOR_ENTRY_POINT = EntryPoint(
    name=IMPOSTOR_ID, value=f"{__name__}:NOT_A_MANIFEST", group=ENTRY_POINT_GROUP
)

MISLABELLED_NAME = "mislabelled_probe"
MISLABELLED_ENTRY_POINT = EntryPoint(
    name=MISLABELLED_NAME,
    # Points at a perfectly valid manifest whose module_id is `current_contract_probe`,
    # so the allowlist would gate one name while a different one registered.
    value=f"{__name__}:CURRENT_CONTRACT_MANIFEST",
    group=ENTRY_POINT_GROUP,
)


def test_a_subscription_declared_under_another_modules_id_is_refused(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """``ConsumerRegistry.register`` checks the three identifiers are non-empty and
    stores whatever ``module_id`` it is handed; agreement with the declaring manifest
    is the loader's check, because only the loader knows which manifest it came
    from."""
    with pytest.raises(ManifestInvalid) as excinfo:
        _load(monkeypatch, registries, BORROWED_SUB_ENTRY_POINT)

    message = str(excinfo.value)
    assert BORROWED_SUB_ID in message
    assert CANARY_ID in message


def test_a_subscription_is_checked_even_when_no_consumer_registry_is_supplied(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """The ``core`` process refuses it too, though it would register no subscription.

    A manifest declaring somebody else's subscription is malformed whatever this
    process would have done with it; a core that accepted what the worker refused
    would be two answers to one question about the same distribution.
    """
    _publish(monkeypatch, BORROWED_SUB_ENTRY_POINT)
    _install(monkeypatch, BORROWED_SUB_ID)

    with pytest.raises(ManifestInvalid):
        load_modules(
            registry=registries.operations,
            resolvers=registries.resolvers,
            tools=registries.tools,
        )


def _nothing_is_registered(registries: _Registries) -> bool:
    """Every surface a load writes, other than the process-global settings registry."""
    return (
        not registries.operations.names()
        and not registries.resolvers.record_types()
        and not registries.tools.names()
        and not registries.kinds.names()
        and not registries.consumers.for_type(SHARED_EVENT_TYPE)
        and sink_for(CANARY_ID) is None
        and loaded_manifests() == {}
    )


def test_the_canary_module_really_registers_on_a_clean_load(
    monkeypatch: pytest.MonkeyPatch, registries: _Registries
) -> None:
    """The positive control for the refusal cases below.

    Without it, "nothing was registered after the refusal" is equally satisfied by a
    canary that registers nothing under any circumstances, and the whole parametrised
    case below would be vacuous.
    """
    assert _load(monkeypatch, registries, CANARY_ENTRY_POINT) == (CANARY_ID,)

    assert registries.operations.names() == frozenset({CANARY_OPERATION})
    assert registries.resolvers.record_types() == frozenset(
        {f"{CANARY_ID}.{RECORD_TYPE}"}
    )
    assert registries.tools.names() == frozenset({CANARY_TOOL_NAME})
    assert registries.kinds.names() == frozenset({CANARY_JOB_KIND})
    assert registries.consumers.for_type(SHARED_EVENT_TYPE) != ()
    assert sink_for(CANARY_ID) is CANARY_SINK
    assert set(loaded_manifests()) == {CANARY_ID}
    assert not _nothing_is_registered(registries)


@pytest.mark.parametrize(
    ("refusal", "offenders"),
    [
        ("event namespacing", (MISNAMED_EVENT_ENTRY_POINT,)),
        ("duplicate event type", (TWIN_ONE_ENTRY_POINT, TWIN_TWO_ENTRY_POINT)),
        ("contract version", (STALE_CONTRACT_ENTRY_POINT,)),
        ("dependency range", (UNSATISFIED_ON_CANARY_ENTRY_POINT,)),
        ("dependency cycle", (CYCLE_A_ENTRY_POINT, CYCLE_B_ENTRY_POINT)),
        ("subscription ownership", (BORROWED_SUB_ENTRY_POINT,)),
        ("not a manifest", (IMPOSTOR_ENTRY_POINT,)),
        ("entry-point name mismatch", (MISLABELLED_ENTRY_POINT,)),
        ("required dependency not loaded", (MISSING_REQUIRED_ENTRY_POINT,)),
    ],
)
def test_a_refused_load_leaves_every_registry_untouched(
    monkeypatch: pytest.MonkeyPatch,
    registries: _Registries,
    refusal: str,
    offenders: tuple[EntryPoint, ...],
) -> None:
    """`load_modules()` validates the whole set before it registers any of it.

    Asserting the raise is not enough, and the subscription-ownership case is why:
    that check began life inside ``_register``, where it fired only after the canary
    ahead of it was fully registered — so the load raised, every "does it refuse?"
    case stayed green, and the registries were left holding a module from a load that
    failed. On a multi-module set that is the difference between a refused startup
    and a half-configured process.

    The canary is published first in every case, so a loader that registered as it
    walked would leave it behind regardless of which check does the refusing.

    All nine ``ManifestInvalid`` raise sites are covered, not the six the property was
    first written against. The last three — an entry point that does not resolve to a
    manifest, one whose name disagrees with the manifest's own id, and a required
    dependency the deployment did not load — all refuse during collection, so the
    property is near-certain for them by inspection. Near-certain by inspection is
    what the subscription case looked like too, right up until it was measured.
    """
    with pytest.raises(ManifestInvalid):
        _load(monkeypatch, registries, CANARY_ENTRY_POINT, *offenders)

    assert _nothing_is_registered(registries), refusal
