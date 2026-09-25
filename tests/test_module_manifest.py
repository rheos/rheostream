"""AC 2 and AC 5: the manifest is a validated model, and it lives in ``rheo_core``.

Seams under test: ``ModuleManifest``'s own construction contract, exercised through
its public constructor — the way a module author and the loader both reach it — and
never through a private helper. Every case here is a construction that either
succeeds or raises ``pydantic.ValidationError`` naming the field at fault.

**The module-level import is itself the first assertion.** ``audit_sink`` is typed
against ``AuditSink``, a ``typing.Protocol`` that is not ``@runtime_checkable``; get
its annotation wrong and pydantic fails at *class-definition* time, so this file
reds at collection before a single test body runs. That is the intended signal, not
a defect in the test.
"""

import tomllib
from pathlib import Path

import pydantic
import pytest
import rheo_contracts
from packaging.requirements import Requirement
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
)
from rheo_contracts.refs import RESERVED_MODULE_SEGMENT
from rheo_core.audit import CORE_AUDIT_SINK
from rheo_core.modules.manifest import (
    Dependency,
    ExportDeclaration,
    FormDeclaration,
    ModuleManifest,
    NavigationEntry,
    RecordType,
    RecordView,
    SearchProvider,
    SensitivityTier,
    StorageDeclaration,
    WebContribution,
    WebRoute,
    WebSurface,
)
from rheo_core.settings.schema import KeySpec, Scope, ValueType

MODULE_ID = "manifest_probe"

RATIFIED_FIELDS = (
    "module_id",
    "package_version",
    "core_contract_versions",
    "dependencies",
    "record_types",
    "storage",
    "configuration_schema",
    "operations",
    "tools",
    "events",
    "subscriptions",
    "jobs",
    "schedules",
    "resolvers",
    "deletion_participants",
    "export",
    "web",
    "agent_guidance",
    "secret_scopes",
    "connector_bindings",
    "health_checks",
    "contract_tests",
    "sensitivity",
)
"""The twenty-three names ``docs/architecture/module-contract.md``'s manifest table
ratifies, in its own order."""

OPTIONAL_FIELDS = frozenset({"web", "agent_guidance", "audit_sink"})

DEFERRED_COLLECTIONS = (
    "record_types",
    "events",
    "subscriptions",
    "jobs",
    "schedules",
    "deletion_participants",
    "connector_bindings",
    "sensitivity",
)
"""FR 3's eight: declared now, consumed by a later run, and **required anyway** — a
module says ``()`` or ``{}`` on purpose rather than leaving a default nobody chose."""

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CORE_PYPROJECT = _REPO_ROOT / "packages" / "core" / "pyproject.toml"


def _exporter(*args: object, **kwargs: object) -> object:
    return None


def _importer(*args: object, **kwargs: object) -> object:
    return None


def _fields(**overrides: object) -> dict[str, object]:
    """A complete, valid set of constructor arguments, before any override."""
    fields: dict[str, object] = {
        "module_id": MODULE_ID,
        "package_version": "0.1.0",
        "core_contract_versions": (1,),
        "dependencies": (),
        "record_types": (),
        "storage": StorageDeclaration(
            schema_name=MODULE_ID,
            migrations_path=f"modules/{MODULE_ID}/migrations",
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
            schema_path=f"modules/{MODULE_ID}/schema.json",
            exporter=_exporter,
            importer=_importer,
        ),
        "secret_scopes": (),
        "connector_bindings": (),
        "health_checks": (),
        "contract_tests": f"tests/modules/{MODULE_ID}",
        "sensitivity": {},
    }
    fields.update(overrides)
    return fields


def _load_note(*args: object) -> None:
    """Never called: these manifests are validated, not rendered."""
    return None


def _tiered_note(tiers: dict[str, dict[str, SensitivityTier]]) -> dict[str, object]:
    """Overrides for a manifest tiering its own ``note`` type.

    Since issue #130 a ``sensitivity`` key must name one of the manifest's own record
    types and a tiered type must declare ``load_for_model``
    (``tests/test_redaction_manifest.py`` covers both refusals), so the tests below
    that exercise the map's freezing declare the type they tier.
    """
    note = RecordType(
        name="note",
        table=f"{MODULE_ID}.note",
        deletable=False,
        delete_roles=frozenset(),
        exportable=False,
        audience_field=None,
        load_for_model=_load_note,
    )
    return {"record_types": (note,), "sensitivity": tiers}


def _locations(error: pydantic.ValidationError) -> set[object]:
    """Every ``loc`` element pydantic reported, flattened."""
    return {part for detail in error.errors() for part in detail["loc"]}


# --- AC 2: a missing field fails validation, with the field named ---------------------


@pytest.mark.parametrize(
    "omitted",
    ["record_types", "storage", "configuration_schema", "tools", "export"],
)
def test_an_omitted_field_is_refused_by_name(omitted: str) -> None:
    """Criterion 25's own sentence: "a missing field fails validation at install
    with the field named". The name has to reach the caller, which is why nothing
    wraps pydantic's ``ValidationError`` — wrapping is what would lose the ``loc``.
    """
    fields = {k: v for k, v in _fields().items() if k != omitted}
    with pytest.raises(pydantic.ValidationError) as excinfo:
        ModuleManifest(**fields)
    assert omitted in _locations(excinfo.value)


# --- FR 3: twenty-four fields, and exactly three of them optional ---------------------


def test_the_model_carries_the_ratified_fields_plus_audit_sink() -> None:
    assert set(ModuleManifest.model_fields) == {*RATIFIED_FIELDS, "audit_sink"}
    assert len(ModuleManifest.model_fields) == 24


def test_only_web_agent_guidance_and_audit_sink_are_optional() -> None:
    """Every other field is required, including the eight nothing reads yet."""
    optional = {
        name
        for name, field in ModuleManifest.model_fields.items()
        if not field.is_required()
    }
    assert optional == OPTIONAL_FIELDS


def test_the_deferred_collections_validate_when_declared_empty() -> None:
    """Present-and-empty is a declaration, and the model accepts it."""
    manifest = ModuleManifest(**_fields())
    for name in DEFERRED_COLLECTIONS[:-1]:
        assert getattr(manifest, name) == ()
    assert manifest.sensitivity == {}


@pytest.mark.parametrize("omitted", DEFERRED_COLLECTIONS)
def test_a_deferred_collection_is_still_required(omitted: str) -> None:
    """The other half of the pair: empty is fine, absent is not."""
    fields = {k: v for k, v in _fields().items() if k != omitted}
    with pytest.raises(pydantic.ValidationError) as excinfo:
        ModuleManifest(**fields)
    assert omitted in _locations(excinfo.value)


def test_sensitivity_carries_the_three_tiers() -> None:
    manifest = ModuleManifest(
        **_fields(**_tiered_note({"note": {"body": SensitivityTier.RESTRICTED}}))
    )
    assert manifest.sensitivity["note"]["body"] is SensitivityTier.RESTRICTED
    assert manifest.sensitivity == {"note": {"body": SensitivityTier.RESTRICTED}}


# --- frozen=True means both of the things it promises ---------------------------------


def test_a_manifest_is_hashable() -> None:
    """``ConfigDict(frozen=True)`` promises a working ``__hash__``, and the frozen
    dataclass this model replaced had one. A plain ``dict`` in ``sensitivity`` would
    make this raise TypeError."""
    manifest = ModuleManifest(
        **_fields(**_tiered_note({"note": {"body": SensitivityTier.INTERNAL}}))
    )
    assert isinstance(hash(manifest), int)
    assert hash(manifest) == hash(manifest)


def test_sensitivity_cannot_be_mutated_in_place_at_either_level() -> None:
    """The other half of the promise. Only freezing the outer mapping would leave
    ``manifest.sensitivity["note"]["body"] = ...`` working on a frozen manifest."""
    manifest = ModuleManifest(
        **_fields(**_tiered_note({"note": {"body": SensitivityTier.INTERNAL}}))
    )
    with pytest.raises(TypeError):
        manifest.sensitivity["other"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        manifest.sensitivity["note"]["body"] = SensitivityTier.PUBLIC  # type: ignore[index]
    assert manifest.sensitivity["note"]["body"] is SensitivityTier.INTERNAL


def test_the_caller_s_own_dict_cannot_reach_back_into_the_manifest() -> None:
    """A copy, not a view: mutating the mapping that was passed in must not change
    what the manifest holds."""
    source = {"note": {"body": SensitivityTier.INTERNAL}}
    manifest = ModuleManifest(**_fields(**_tiered_note(source)))
    source["note"]["body"] = SensitivityTier.PUBLIC
    source["added"] = {}
    assert manifest.sensitivity["note"]["body"] is SensitivityTier.INTERNAL
    assert "added" not in manifest.sensitivity


# --- audit_sink: the duck check, not an isinstance probe ------------------------------


def test_a_real_sink_validates_and_is_returned_unchanged() -> None:
    """Identity matters: ``loader.py``'s ``_register`` hands this very object to
    ``install_sink``, whose idempotence is an ``is`` comparison."""
    manifest = ModuleManifest(**_fields(), audit_sink=CORE_AUDIT_SINK)
    assert manifest.audit_sink is CORE_AUDIT_SINK


def test_an_object_that_is_not_a_sink_is_refused_by_name() -> None:
    """A bare ``object()`` is what ``SkipValidation`` would have let through."""
    with pytest.raises(pydantic.ValidationError) as excinfo:
        ModuleManifest(**_fields(), audit_sink=object())
    assert "audit_sink" in _locations(excinfo.value)


def test_no_audit_sink_is_allowed() -> None:
    assert ModuleManifest(**_fields()).audit_sink is None


# --- the model validator's own rules --------------------------------------------------


def test_a_version_range_that_is_not_pep_440_is_refused_by_name() -> None:
    """``^1.2`` is npm's grammar, not Python's — the mistake the version-compatibility
    section used to invite by naming "the usual caret and tilde forms"."""
    with pytest.raises(pydantic.ValidationError) as excinfo:
        ModuleManifest(
            **_fields(
                dependencies=(
                    Dependency(module_id="recallatron", version_range="^1.2"),
                )
            )
        )
    assert "version_range" in str(excinfo.value)


def test_a_pep_440_version_range_is_accepted() -> None:
    manifest = ModuleManifest(
        **_fields(
            dependencies=(
                Dependency(module_id="recallatron", version_range=">=1.2,<2"),
            )
        )
    )
    assert manifest.dependencies[0].optional is False


@pytest.mark.parametrize(
    "bad_id", ["Probe", "manifest-probe", "1probe", "manifest probe"]
)
def test_a_module_id_that_is_not_a_lowercase_identifier_is_refused(bad_id: str) -> None:
    """One of the two rules the deleted ``validate()`` carried that nothing else in
    the tree re-checks. ``storage.schema_name`` is set to match, so only the
    identifier rule can be what fires."""
    with pytest.raises(pydantic.ValidationError) as excinfo:
        ModuleManifest(
            **_fields(
                module_id=bad_id,
                storage=StorageDeclaration(
                    schema_name=bad_id,
                    migrations_path="m",
                    required_extensions=(),
                ),
            )
        )
    assert "module_id" in str(excinfo.value)
    assert "lowercase identifier" in str(excinfo.value)


def test_the_reserved_core_module_id_is_refused() -> None:
    """``core`` is the segment the core itself mints references under
    (``rheo_contracts.refs.RESERVED_MODULE_SEGMENT``), so a module claiming it would
    shadow core-owned records. Read from the contract rather than spelled here, so
    the test follows the constant if it ever moves."""
    assert RESERVED_MODULE_SEGMENT == "core"
    with pytest.raises(pydantic.ValidationError) as excinfo:
        ModuleManifest(
            **_fields(
                module_id=RESERVED_MODULE_SEGMENT,
                storage=StorageDeclaration(
                    schema_name=RESERVED_MODULE_SEGMENT,
                    migrations_path="m",
                    required_extensions=(),
                ),
            )
        )
    assert "reserved" in str(excinfo.value)


def _key_spec(key: str) -> KeySpec:
    return KeySpec(
        key=key,
        type=ValueType.INT,
        scope=Scope.WORKSPACE,
        floor=None,
        explicit_per_workspace=False,
        default=30,
    )


def test_a_configuration_key_outside_the_module_namespace_is_refused() -> None:
    """Keys must start with ``<module_id>.`` so one module cannot declare, and then
    write, another module's settings."""
    with pytest.raises(pydantic.ValidationError) as excinfo:
        ModuleManifest(
            **_fields(configuration_schema=(_key_spec("somewhere_else.retention"),))
        )
    assert "configuration_schema" in str(excinfo.value)
    assert "somewhere_else.retention" in str(excinfo.value)


def test_a_configuration_key_inside_the_module_namespace_is_accepted() -> None:
    """The control: the same read, one prefix different, has to pass."""
    manifest = ModuleManifest(
        **_fields(configuration_schema=(_key_spec(f"{MODULE_ID}.retention"),))
    )
    assert manifest.configuration_schema[0].key == f"{MODULE_ID}.retention"


def test_a_storage_schema_that_is_not_the_module_id_is_refused() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        ModuleManifest(
            **_fields(
                storage=StorageDeclaration(
                    schema_name="somewhere_else",
                    migrations_path="m",
                    required_extensions=(),
                )
            )
        )
    assert "somewhere_else" in str(excinfo.value)


# --- AC 5: the manifest's home is rheo_core, and rheo_contracts does not have one -----


def test_rheo_contracts_exposes_no_module_manifest() -> None:
    """AC 5's one mechanically checkable clause. The manifest carries handlers typed
    against ``UnitOfWork``, which ``rheo_contracts`` may not import, so the type
    cannot live there — and ``tests/test_contracts.py`` pins that package's ratified
    surface without saying anything about this absence.

    The positive control is load-bearing: without it, a mistyped module name would
    make the absence look true.
    """
    assert hasattr(rheo_contracts, "OperationDeclaration")
    assert not hasattr(rheo_contracts, "ModuleManifest")


# --- web: the contribution's shape ----------------------------------------------------


class _ProbeInput(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="ignore")

    text: str


class _ProbeOutput(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    text: str


def _probe_handler(*args: object, **kwargs: object) -> object:
    return None


def _operation(name: str, safety_class: SafetyClass) -> OperationDeclaration:
    return OperationDeclaration(
        name=name,
        safety_class=safety_class,
        roles=frozenset({Role.OWNER}),
        input_model=_ProbeInput,
        output=_ProbeOutput,
        idempotency=Idempotency.NONE,
        audit=AuditSpec(subject_field=None),
    )


READ_OPERATION = f"{MODULE_ID}.note.find"
MUTATE_OPERATION = f"{MODULE_ID}.note.add"
OWNED_RECORD_TYPE = "note"

_WEB_OWNERSHIP = {
    "operations": (
        (_operation(READ_OPERATION, SafetyClass.READ), _probe_handler),
        (_operation(MUTATE_OPERATION, SafetyClass.MUTATE), _probe_handler),
    ),
    "record_types": (
        RecordType(
            name=OWNED_RECORD_TYPE,
            table="notes",
            deletable=False,
            delete_roles=frozenset(),
            exportable=False,
            audience_field=None,
        ),
    ),
}
"""The operations and record type a probe contribution may name as its own."""


def _web(**overrides: object) -> WebContribution:
    """A valid contribution exercising every tuple, before any override."""
    fields: dict[str, object] = {
        "surface": WebSurface(surface=MODULE_ID, host=MODULE_ID, path="/probe"),
        "package_name": "@rheo-stream/manifest-probe-web",
        "navigation": (NavigationEntry(id="home", label="Home", path="/"),),
        "routes": (
            WebRoute(id="home", path="/", screen="home"),
            WebRoute(id="detail", path="/notes/detail", screen="detail"),
        ),
        "record_views": (
            RecordView(
                record_type=f"{MODULE_ID}.{OWNED_RECORD_TYPE}", component="NoteView"
            ),
        ),
        "forms": (FormDeclaration(operation=MUTATE_OPERATION, component="NoteForm"),),
        "search_providers": (SearchProvider(id="find", operation=READ_OPERATION),),
    }
    fields.update(overrides)
    return WebContribution(**fields)  # type: ignore[arg-type]


def _web_manifest(web: WebContribution) -> ModuleManifest:
    return ModuleManifest(**_fields(**_WEB_OWNERSHIP, web=web))


def test_web_is_an_optional_web_contribution() -> None:
    """Widened from ``WebSurface | None``; the surface now travels inside it."""
    assert ModuleManifest.model_fields["web"].annotation == WebContribution | None
    assert ModuleManifest(**_fields()).web is None


def test_the_web_contribution_carries_the_ratified_fields_plus_surface() -> None:
    assert set(WebContribution.model_fields) == {
        "surface",
        "package_name",
        "navigation",
        "routes",
        "record_views",
        "forms",
        "search_providers",
    }
    fields = WebContribution.model_fields
    assert fields["surface"].annotation is WebSurface
    assert fields["navigation"].annotation == tuple[NavigationEntry, ...]
    assert fields["routes"].annotation == tuple[WebRoute, ...]
    assert fields["record_views"].annotation == tuple[RecordView, ...]
    assert fields["forms"].annotation == tuple[FormDeclaration, ...]
    assert fields["search_providers"].annotation == tuple[SearchProvider, ...]


def test_the_entry_types_carry_their_declared_fields() -> None:
    assert set(NavigationEntry.model_fields) == {"id", "label", "path", "roles"}
    assert set(WebRoute.model_fields) == {"id", "path", "screen"}
    assert set(RecordView.model_fields) == {"record_type", "component"}
    assert set(FormDeclaration.model_fields) == {"operation", "component"}
    assert set(SearchProvider.model_fields) == {"id", "operation"}
    roles = NavigationEntry.model_fields["roles"]
    assert roles.annotation == frozenset[Role]
    assert roles.default == frozenset({Role.OWNER, Role.MEMBER})


@pytest.mark.parametrize(
    "model",
    [
        NavigationEntry,
        WebRoute,
        RecordView,
        FormDeclaration,
        SearchProvider,
        WebContribution,
    ],
)
def test_every_web_type_is_frozen_and_forbids_extra_fields(
    model: type[pydantic.BaseModel],
) -> None:
    assert model.model_config.get("frozen") is True
    assert model.model_config.get("extra") == "forbid"


def test_an_unknown_field_on_a_web_entry_is_refused() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        NavigationEntry(id="home", label="Home", path="/", surface="elsewhere")  # type: ignore[call-arg]
    assert "surface" in _locations(excinfo.value)


def test_a_valid_contribution_is_accepted_and_hashable() -> None:
    """The control every refusal below is measured against: the same contribution,
    one field different, has to pass."""
    manifest = _web_manifest(_web())
    assert manifest.web == _web()
    assert isinstance(hash(manifest), int)
    assert manifest.web is not None
    assert manifest.web.navigation[0].roles == frozenset({Role.OWNER, Role.MEMBER})


# --- web: grammar, refused on the field ----------------------------------------------


@pytest.mark.parametrize(
    "path", ["search", "/Search", "/a/", "/a//b", "/item/[id]", "/item/:id", "/a b"]
)
def test_a_route_path_outside_the_exact_path_grammar_is_refused(path: str) -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        WebRoute(id="bad", path=path, screen="bad")
    assert "path" in _locations(excinfo.value)
    assert "query string" in str(excinfo.value)


@pytest.mark.parametrize("path", ["/", "/search", "/notes/detail", "/a-1/b2"])
def test_an_exact_route_path_is_accepted(path: str) -> None:
    assert WebRoute(id="ok", path=path, screen="ok").path == path


@pytest.mark.parametrize(
    "package_name", ["recallatron-web", "@other/probe-web", "@rheo-stream/"]
)
def test_a_package_name_outside_the_scope_is_refused(package_name: str) -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        _web(package_name=package_name)
    assert "package_name" in _locations(excinfo.value)
    assert "@rheo-stream/" in str(excinfo.value)


@pytest.mark.parametrize("identifier", ["browse-screen", "1view", "browse]", "a.b", ""])
def test_a_screen_or_component_that_is_not_a_ts_identifier_is_refused(
    identifier: str,
) -> None:
    with pytest.raises(pydantic.ValidationError) as screen:
        WebRoute(id="bad", path="/", screen=identifier)
    with pytest.raises(pydantic.ValidationError) as component:
        RecordView(record_type=f"{MODULE_ID}.note", component=identifier)
    with pytest.raises(pydantic.ValidationError) as form:
        FormDeclaration(operation=MUTATE_OPERATION, component=identifier)
    assert "screen" in _locations(screen.value)
    assert "component" in _locations(component.value)
    assert "component" in _locations(form.value)
    assert "TypeScript identifier" in str(screen.value)


@pytest.mark.parametrize("identifier", ["browse", "NoteView", "_private", "v2"])
def test_a_ts_identifier_is_accepted(identifier: str) -> None:
    assert WebRoute(id="ok", path="/", screen=identifier).screen == identifier


@pytest.mark.parametrize("bad_id", ["Memory", "a_b", "-a", "a-", ""])
def test_an_id_that_is_not_a_slug_is_refused(bad_id: str) -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        NavigationEntry(id=bad_id, label="Home", path="/")
    assert "id" in _locations(excinfo.value)


@pytest.mark.parametrize("label", ["", "x" * 41])
def test_a_navigation_label_outside_one_to_forty_characters_is_refused(
    label: str,
) -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        NavigationEntry(id="home", label=label, path="/")
    assert "label" in _locations(excinfo.value)


def test_a_forty_character_label_is_accepted() -> None:
    assert len(NavigationEntry(id="home", label="x" * 40, path="/").label) == 40


# --- web: ownership, refused by _check_invariants ------------------------------------

_HOME = WebRoute(id="home", path="/", screen="home")

WEB_REFUSALS: dict[str, tuple[dict[str, object], str]] = {
    "duplicate navigation id": (
        {
            "navigation": (
                NavigationEntry(id="home", label="Home", path="/"),
                NavigationEntry(id="home", label="Again", path="/"),
            )
        },
        "web.navigation: id 'home' is declared twice",
    ),
    "duplicate route id": (
        {"routes": (_HOME, WebRoute(id="home", path="/other", screen="other"))},
        "web.routes: id 'home' is declared twice",
    ),
    "duplicate route path": (
        {"routes": (_HOME, WebRoute(id="again", path="/", screen="again"))},
        "web.routes: path '/' is declared twice",
    ),
    "duplicate record view": (
        {
            "record_views": (
                RecordView(record_type=f"{MODULE_ID}.note", component="NoteView"),
                RecordView(record_type=f"{MODULE_ID}.note", component="OtherView"),
            )
        },
        f"web.record_views: record type '{MODULE_ID}.note' is declared twice",
    ),
    "duplicate form": (
        {
            "forms": (
                FormDeclaration(operation=MUTATE_OPERATION, component="NoteForm"),
                FormDeclaration(operation=MUTATE_OPERATION, component="OtherForm"),
            )
        },
        f"web.forms: operation '{MUTATE_OPERATION}' is declared twice",
    ),
    "duplicate search provider id": (
        {
            "search_providers": (
                SearchProvider(id="find", operation=READ_OPERATION),
                SearchProvider(id="find", operation=READ_OPERATION),
            )
        },
        "web.search_providers: id 'find' is declared twice",
    ),
    "dangling navigation path": (
        {"navigation": (NavigationEntry(id="lost", label="Lost", path="/nowhere"),)},
        "web.navigation: entry 'lost' points at '/nowhere'",
    ),
    "search provider naming another module's operation": (
        {
            "search_providers": (
                SearchProvider(id="find", operation="other_probe.note.find"),
            )
        },
        "web.search_providers: provider 'find' names 'other_probe.note.find', "
        f"which is not one of module '{MODULE_ID}''s own operations",
    ),
    "search provider naming a mutate operation": (
        {"search_providers": (SearchProvider(id="find", operation=MUTATE_OPERATION),)},
        f"names '{MUTATE_OPERATION}', which is 'mutate'-class",
    ),
    "form naming another module's operation": (
        {
            "forms": (
                FormDeclaration(operation="other_probe.note.add", component="NoteForm"),
            )
        },
        "web.forms: form 'NoteForm' names 'other_probe.note.add'",
    ),
    "record view of an undeclared type": (
        {
            "record_views": (
                RecordView(record_type=f"{MODULE_ID}.ghost", component="GhostView"),
            )
        },
        f"web.record_views: record type '{MODULE_ID}.ghost' is not",
    ),
    "record view of another module's type": (
        {
            "record_views": (
                RecordView(record_type="other_probe.note", component="NoteView"),
            )
        },
        "web.record_views: record type 'other_probe.note' is not",
    ),
}
"""Each rule ``_check_invariants`` adds for ``web``, broken one at a time against the
valid :func:`_web` and named by the message only that rule produces."""


@pytest.mark.parametrize("case", sorted(WEB_REFUSALS))
def test_each_web_ownership_rule_refuses_by_name(case: str) -> None:
    overrides, message = WEB_REFUSALS[case]
    with pytest.raises(pydantic.ValidationError) as excinfo:
        _web_manifest(_web(**overrides))
    assert message in str(excinfo.value)


def test_a_form_may_name_a_non_read_operation() -> None:
    """Only a search provider is held to ``READ``; a form exists to write."""
    manifest = _web_manifest(
        _web(forms=(FormDeclaration(operation=MUTATE_OPERATION, component="F"),))
    )
    assert manifest.web is not None
    assert manifest.web.forms[0].operation == MUTATE_OPERATION


def test_the_web_rules_read_the_manifest_s_own_declarations() -> None:
    """The same contribution on a manifest that declares no operations and no record
    types is refused: ownership is read off this manifest, not assumed."""
    with pytest.raises(pydantic.ValidationError) as excinfo:
        ModuleManifest(**_fields(web=_web()))
    assert "not one of module" in str(excinfo.value)


# --- the packaging runtime dependency -------------------------------------------------


def test_packaging_is_a_declared_dependency_of_rheo_core() -> None:
    """``Dependency.version_range`` parses through ``packaging.specifiers``, and
    nothing else in the suite would go red if the declaration were missing:
    ``packaging`` already resolves transitively, so the import works today and would
    break on the next lock regeneration instead. ``alembic`` is the positive control,
    so a mis-pathed or mis-keyed read cannot report success.
    """
    project = tomllib.loads(_CORE_PYPROJECT.read_text(encoding="utf-8"))["project"]
    declared = {Requirement(entry).name for entry in project["dependencies"]}
    assert "alembic" in declared
    assert "packaging" in declared
