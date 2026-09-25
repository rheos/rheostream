"""The redaction contract's static half: what a manifest may declare (issue #130).

``intake-and-events.md``: "a manifest whose event model declares a field of a
``restricted`` tier fails registration". The rule is checked in the manifest's own
validator, so a module that breaks it cannot be constructed at all, and therefore
cannot be loaded, registered or composed into a web build either. Each case builds a
probe manifest through ``tests/harness/modules.py``'s ``manifest()`` builder, varying
only what the case is about, and asserts the refusal names what it refused.

The last two cases load :data:`~harness.modules.REDACTION_MANIFEST` through the
shipped loader and read back what it registered: a model rendering for its tiered
record type only, and the core-declared ``<module>.redaction.exclude_types`` key.
"""

from typing import Annotated, Optional, Union

import pydantic
import pytest
from harness.modules import (
    PROBE_ENTRY_POINTS,
    REDACTION_ID,
    REDACTION_TYPE,
    REDACTION_UNTIERED_TYPE,
    UNCHECKED_ID,
    loaded_probe_modules,
    manifest,
    publish,
)
from pydantic import BaseModel
from rheo_core.modules import (
    EventDeclaration,
    ManifestInvalid,
    RecordType,
    SensitivityTier,
    Tiered,
    load_modules,
    loaded_manifests,
)
from rheo_core.modules import loader as loader_module
from rheo_core.operations import OperationRegistry
from rheo_core.redaction.policy import exclude_types_key
from rheo_core.refs.resolver import ResolverRegistry
from rheo_core.settings import Floor, KeySpec, Scope, ValueType
from rheo_core.tokens.sets import ToolRegistry

PROBE = "sens_probe"


def _load(*args: object) -> None:
    return None


def _render(*args: object) -> str:
    return ""


def _record(name: str = "contact", **overrides: object) -> RecordType:
    fields: dict[str, object] = {
        "name": name,
        "table": f"{PROBE}.{name}",
        "deletable": False,
        "delete_roles": frozenset(),
        "exportable": False,
        "audience_field": None,
    }
    fields.update(overrides)
    return RecordType(**fields)


def _refused(**overrides: object) -> str:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        manifest(PROBE, **overrides)
    return str(excinfo.value)


# --- events: a restricted field fails registration ------------------------------------


class RefsOnly(BaseModel):
    party_ref: str
    purpose: str


class WithRestricted(BaseModel):
    party_ref: str
    email: Annotated[str, Tiered(SensitivityTier.RESTRICTED)]


class Inner(BaseModel):
    value: Annotated[str, Tiered(SensitivityTier.RESTRICTED)]


class WithNested(BaseModel):
    party_ref: str
    points: list[Inner]


class WithInternal(BaseModel):
    party_ref: str
    stage: Annotated[str, Tiered(SensitivityTier.INTERNAL)]


def _event(data: type[BaseModel]) -> EventDeclaration:
    return EventDeclaration(type=f"{PROBE}.party.changed", schema_version=1, data=data)


def test_an_event_carrying_references_only_registers() -> None:
    declared = manifest(PROBE, events=(_event(RefsOnly),))
    assert declared.events[0].data is RefsOnly


def test_a_restricted_event_field_fails_registration() -> None:
    detail = _refused(events=(_event(WithRestricted),))
    assert f"{PROBE}.party.changed" in detail
    assert "'email'" in detail
    assert "restricted" in detail


def test_a_restricted_field_in_a_nested_event_model_fails_too() -> None:
    detail = _refused(events=(_event(WithNested),))
    assert "'points.value'" in detail


def test_an_internal_event_field_is_not_refused_by_this_rule() -> None:
    """The ratified rule names ``restricted``. Whether ``internal`` (party names)
    should be refused too, given "never a name" in the same section, is recorded as
    an open decision rather than decided here."""
    manifest(PROBE, events=(_event(WithInternal),))


# --- the sensitivity map and the record-type pair -------------------------------------


def test_a_tiered_record_type_with_a_loader_is_accepted() -> None:
    declared = manifest(
        PROBE,
        record_types=(_record(load_for_model=_load),),
        sensitivity={"contact": {"value": SensitivityTier.RESTRICTED}},
    )
    assert declared.record_types[0].load_for_model is _load


def test_a_sensitivity_key_must_be_one_of_the_manifest_s_own_record_types() -> None:
    detail = _refused(
        record_types=(_record(load_for_model=_load),),
        sensitivity={
            "contact": {"value": SensitivityTier.RESTRICTED},
            "elsewhere": {"body": SensitivityTier.PUBLIC},
        },
    )
    assert "'elsewhere'" in detail


def test_an_empty_tier_map_is_refused() -> None:
    detail = _refused(
        record_types=(_record(load_for_model=_load),), sensitivity={"contact": {}}
    )
    assert "empty tier map" in detail


def test_a_tiered_type_without_a_loader_is_refused() -> None:
    detail = _refused(
        record_types=(_record(),),
        sensitivity={"contact": {"value": SensitivityTier.RESTRICTED}},
    )
    assert "load_for_model" in detail


def test_a_type_with_its_own_renderer_needs_a_loader_too() -> None:
    detail = _refused(record_types=(_record(render_for_model=_render),))
    assert "load_for_model" in detail
    manifest(
        PROBE, record_types=(_record(render_for_model=_render, load_for_model=_load),)
    )


def test_a_loader_on_an_untiered_type_is_refused() -> None:
    detail = _refused(record_types=(_record(load_for_model=_load),))
    assert "nothing reads it" in detail


def test_a_non_callable_renderer_is_refused() -> None:
    with pytest.raises(pydantic.ValidationError):
        _record(load_for_model=_load, render_for_model="not callable")


def test_the_redaction_key_segment_is_the_core_s() -> None:
    detail = _refused(
        configuration_schema=(
            KeySpec(
                key=f"{PROBE}.redaction.exclude_types",
                type=ValueType.STR_LIST,
                scope=Scope.WORKSPACE,
                floor=Floor.UNION,
                explicit_per_workspace=False,
                default=(),
            ),
        )
    )
    assert f"{PROBE}.redaction." in detail


# --- what the loader registers --------------------------------------------------------


def test_loading_registers_a_rendering_for_the_tiered_type_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with loaded_probe_modules(monkeypatch, REDACTION_ID) as surfaces:
        rendering = surfaces.renderings.lookup(REDACTION_ID, REDACTION_TYPE)
        assert rendering is not None
        assert dict(rendering.tiers) == {
            "label": SensitivityTier.PUBLIC,
            "value": SensitivityTier.RESTRICTED,
        }
        assert rendering.render is None
        assert surfaces.renderings.lookup(REDACTION_ID, REDACTION_UNTIERED_TYPE) is None
        assert surfaces.renderings.record_types() == {
            f"{REDACTION_ID}.{REDACTION_TYPE}"
        }


def test_loading_declares_the_module_s_exclude_types_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with loaded_probe_modules(monkeypatch, REDACTION_ID):
        registry = loader_module.SETTINGS_REGISTRY
        spec = registry.get(exclude_types_key(REDACTION_ID))
        assert (spec.type, spec.scope, spec.floor, spec.default) == (
            ValueType.STR_LIST,
            Scope.WORKSPACE,
            Floor.UNION,
            (),
        )
        assert registry.origin_of(spec.key) == REDACTION_ID


# --- the review's spellings, forward references and model_construct -------------------

R = Tiered(SensitivityTier.RESTRICTED)


class PipeNone(BaseModel):
    party_ref: str
    contact: Annotated[str, R] | None = None


class OptionalArm(BaseModel):
    party_ref: str
    contact: Optional[Annotated[str, R]] = None  # noqa: UP045 - the spelling under test


class UnionArm(BaseModel):
    party_ref: str
    contact: Union[int, Annotated[str, R]]  # noqa: UP007 - the spelling under test


class ListElement(BaseModel):
    party_ref: str
    contacts: list[Annotated[str, R]]


class InnerOptional(BaseModel):
    party_ref: str
    contact: Annotated[str | None, R] = None


class OptionalNested(BaseModel):
    party_ref: str
    point: Optional[Inner]  # noqa: UP045 - the spelling under test


class DictNested(BaseModel):
    party_ref: str
    points: dict[str, Inner]


@pytest.mark.parametrize(
    ("data", "path"),
    [
        (PipeNone, "contact"),
        (OptionalArm, "contact"),
        (UnionArm, "contact"),
        (ListElement, "contacts"),
        (InnerOptional, "contact"),
        (OptionalNested, "point.value"),
        (DictNested, "points.value"),
    ],
    ids=[
        "pipe-none",
        "optional",
        "union-arm",
        "list-element",
        "inner-optional",
        "optional-nested",
        "dict-nested",
    ],
)
def test_a_restricted_marker_in_any_spelling_fails_registration(
    data: type[BaseModel], path: str
) -> None:
    detail = _refused(events=(_event(data),))
    assert f"'{path}'" in detail


class Holder(BaseModel):
    party_ref: str
    later: "NotYetDefined"  # type: ignore[name-defined]  # noqa: F821


def test_an_unresolved_forward_reference_fails_registration() -> None:
    """A field pydantic cannot type is one the guard cannot read; passing it would
    answer "no restricted field" about a field nobody looked at."""
    detail = _refused(events=(_event(Holder),))
    assert "unresolved forward references" in detail
    assert "Holder" in detail


def test_the_loader_rechecks_a_manifest_that_skipped_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``model_construct`` skips the manifest's validator; the loader's own pass over
    the set refuses it before anything registers."""
    registry = OperationRegistry()
    publish(monkeypatch, PROBE_ENTRY_POINTS[UNCHECKED_ID])
    with pytest.raises(ManifestInvalid) as excinfo:
        load_modules(
            registry=registry,
            resolvers=ResolverRegistry(),
            tools=ToolRegistry(),
            allow=frozenset({UNCHECKED_ID}),
        )
    assert excinfo.value.module_id == UNCHECKED_ID
    assert "'email'" in excinfo.value.detail
    assert UNCHECKED_ID not in loaded_manifests()
