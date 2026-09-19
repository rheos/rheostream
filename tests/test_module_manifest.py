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
from rheo_core.audit import CORE_AUDIT_SINK
from rheo_core.modules.manifest import (
    Dependency,
    ExportDeclaration,
    ModuleManifest,
    SensitivityTier,
    StorageDeclaration,
)

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
        **_fields(sensitivity={"note": {"body": SensitivityTier.RESTRICTED}})
    )
    assert manifest.sensitivity["note"]["body"] is SensitivityTier.RESTRICTED


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
