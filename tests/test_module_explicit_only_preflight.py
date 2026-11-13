"""The loader re-runs the ``explicit_only`` tool rule before anything registers (#537).

``ModuleManifest``'s validator refuses an ``explicit_only`` module whose tool names an
operation the module does not declare (``tests/test_module_manifest.py`` covers that
half). ``ModuleManifest.model_construct`` skips validators, so the loader re-runs the
rule in its preflight, beside the name-prefix and redaction re-checks; otherwise a
tool naming ``core.record.delete`` would reach every routine token holding that
operation, past the module's opt-in.

Seams under test:

- ``load_modules`` refuses an unvalidated ``explicit_only`` manifest whose tool names a
  core operation, before any operation or tool registers.
- The same manifest declared ``default`` passes the preflight and loads, so the
  refusal is the ``explicit_only`` rule and not some other check.
"""

from collections.abc import Iterator
from importlib.metadata import EntryPoint

import pydantic
import pytest
from harness.modules import install, manifest, publish
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    ToolDeclaration,
)
from rheo_core.modules import (
    ENTRY_POINT_GROUP,
    ManifestInvalid,
    ModuleManifest,
    load_modules,
    loaded_manifests,
    reset_surfaces,
)
from rheo_core.operations import OperationRegistry
from rheo_core.refs.resolver import ResolverRegistry
from rheo_core.tokens.sets import ToolRegistry

MODULE_ID = "optin_probe"
OWN_OPERATION = f"{MODULE_ID}.entry.add"
OWN_TOOL = f"{MODULE_ID}_add"
CORE_TOOL = f"{MODULE_ID}_delete"


class _ProbeInput(pydantic.BaseModel):
    note: str


class _ProbeOutput(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    text: str


def _probe_handler(*args: object, **kwargs: object) -> object:
    """Never run: nothing here dispatches an operation."""
    return None


def _unvalidated(package_sets: str) -> ModuleManifest:
    """A module with one own operation and a tool naming ``core.record.delete``,
    built with the validators skipped."""
    fields = dict(manifest(MODULE_ID))
    fields.update(
        package_sets=package_sets,
        operations=(
            (
                OperationDeclaration(
                    name=OWN_OPERATION,
                    safety_class=SafetyClass.MUTATE,
                    roles=frozenset({Role.OWNER}),
                    input_model=_ProbeInput,
                    output=_ProbeOutput,
                    idempotency=Idempotency.NONE,
                    audit=AuditSpec(subject_field=None),
                ),
                _probe_handler,
            ),
        ),
        tools=(
            ToolDeclaration(
                name=OWN_TOOL,
                safety_class=SafetyClass.MUTATE,
                operation=OWN_OPERATION,
                input_model=_ProbeInput,
            ),
            ToolDeclaration(
                name=CORE_TOOL,
                safety_class=SafetyClass.DESTRUCTIVE,
                operation="core.record.delete",
                input_model=_ProbeInput,
            ),
        ),
    )
    return ModuleManifest.model_construct(**fields)


EXPLICIT_ONLY_MANIFEST = _unvalidated("explicit_only")
DEFAULT_MANIFEST = _unvalidated("default")


@pytest.fixture(autouse=True)
def clean_loaded() -> Iterator[None]:
    reset_surfaces()
    yield
    reset_surfaces()


def _load(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    operations: OperationRegistry,
    tools: ToolRegistry,
) -> None:
    publish(
        monkeypatch,
        EntryPoint(
            name=MODULE_ID, value=f"{__name__}:{attribute}", group=ENTRY_POINT_GROUP
        ),
    )
    install(monkeypatch, MODULE_ID)
    load_modules(registry=operations, resolvers=ResolverRegistry(), tools=tools)


def test_an_unvalidated_explicit_only_tool_naming_a_core_operation_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, tools = OperationRegistry(), ToolRegistry()

    with pytest.raises(ManifestInvalid) as excinfo:
        _load(monkeypatch, "EXPLICIT_ONLY_MANIFEST", operations, tools)

    assert str(excinfo.value) == (
        f"{MODULE_ID}: tools: {CORE_TOOL!r} names 'core.record.delete'; an "
        "explicit_only module's tools must name its own operations"
    )
    assert operations.names() == frozenset()
    assert tools.names() == frozenset()
    assert loaded_manifests() == {}


def test_the_same_manifest_declared_default_passes_the_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: only ``package_sets`` differs, and this one loads."""
    operations, tools = OperationRegistry(), ToolRegistry()

    _load(monkeypatch, "DEFAULT_MANIFEST", operations, tools)

    assert OWN_OPERATION in operations.names()
    assert tools.names() == frozenset({OWN_TOOL, CORE_TOOL})
    assert set(loaded_manifests()) == {MODULE_ID}
