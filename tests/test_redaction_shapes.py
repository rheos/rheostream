"""Output shapes, guard shapes and exclusion shapes under the redaction contract.

Issue #130's second review ran five probes against the renderer, the event guard, the
exclusion rule and the masks; each probe is a parametrised case here, so the shapes it
found leaking (a frozen tiered model in a set, a ``TypedDict``, a ``NamedTuple``, a
tiered model with a ``@model_serializer``, a ``@field_serializer`` reshaping a list, an
unmarked ``ref`` on a tiered class) stay closed, and the shapes it found safe stay safe.

Every output case holds the same restricted marker value, and the assertion is only
that it never appears in the rendering: what else is kept is each shape's own business
and is pinned in ``test_redaction.py`` where it matters.
"""

import dataclasses
import json
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import (
    Annotated,
    Generic,
    Literal,
    NamedTuple,
    Optional,
    TypedDict,
    TypeVar,
    Union,
)
from uuid import UUID

import pytest
from harness import isolate_rheo_environment
from harness.redaction import AREA, EXCHANGE, LINE, PROBE_PHONE, phone
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    RootModel,
    computed_field,
    field_serializer,
    model_serializer,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass
from rheo_contracts import (
    ContextPurpose,
    Idempotency,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
)
from rheo_core.operations import OperationRegistry
from rheo_core.operations.refusals import RegistrationRefused
from rheo_core.redaction.masking import (
    EMAIL_MASK,
    PHONE_MASK,
    SECRET_MASK,
    mask_contact_values,
    mask_secret_references,
    mask_tokens_in,
)
from rheo_core.redaction.policy import TierPolicy, exclude_types_spec
from rheo_core.redaction.render import render_output
from rheo_core.redaction.tiers import (
    SensitivityTier,
    Tiered,
    restricted_fields,
    unrenderable_paths,
)
from rheo_core.settings import TEST_HARNESS_ORIGIN, register, resolve

R = Tiered(SensitivityTier.RESTRICTED)
P = Tiered(SensitivityTier.PUBLIC)
SECRET = "TOPSECRET-VALUE"
WORKSPACE = UUID("018f0000-0000-7000-8000-0000000001b0")
EXCL_MODULE = "shape_probe"
EXCLUDED_REF = f"{EXCL_MODULE}.note:0192f0a0-0000-7000-8000-000000000000"


@pytest.fixture(autouse=True)
def data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "data"
    isolate_rheo_environment(monkeypatch, root)
    register(exclude_types_spec(EXCL_MODULE), origin=EXCL_MODULE)
    monkeypatch.setenv(f"RHEO__{EXCL_MODULE}__redaction__exclude_types", "note")
    return root


def _policy(
    purpose: ContextPurpose = ContextPurpose.INTERNAL_ANALYSIS,
) -> TierPolicy:
    return TierPolicy.from_settings(purpose, resolve())


# --- probe_render / probe_render2: nothing restricted leaves, in any shape


class Inner(BaseModel):
    title: Annotated[str, P]
    secret: Annotated[str, R]


class FrozenInner(BaseModel):
    model_config = ConfigDict(frozen=True)
    title: Annotated[str, P]
    secret: Annotated[str, R]


def _inner() -> Inner:
    return Inner(title="t", secret=SECRET)


def _frozen() -> FrozenInner:
    return FrozenInner(title="t", secret=SECRET)


class NestedAnnotated(BaseModel):
    x: Annotated[Annotated[str, R], Field(description="d")]


class DictOfLists(BaseModel):
    d: dict[str, list[Inner]]


class TupleOf(BaseModel):
    t: tuple[Inner, ...]


class SequenceOf(BaseModel):
    s: Sequence[Inner]


class MappingOf(BaseModel):
    m: Mapping[str, Inner]


class FrozenSetOf(BaseModel):
    fs: frozenset[FrozenInner]


class SetOf(BaseModel):
    s: set[FrozenInner]


class Aliased(BaseModel):
    email: Annotated[str, R] = Field(alias="contact")
    name: Annotated[str, P]


class SerializesToText(BaseModel):
    email: Annotated[str, R]
    name: Annotated[str, P]

    @model_serializer
    def _text(self) -> str:
        return f"{self.name}:{self.email}"


class HoldsTextSerializer(BaseModel):
    inner: SerializesToText


class RenamesOnSerialize(BaseModel):
    email: Annotated[str, R]
    name: Annotated[str, P]

    @model_serializer
    def _renamed(self) -> dict[str, str]:
        return {"name": self.name, "info": self.email}


class ComputedLeak(BaseModel):
    email: Annotated[str, R]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def shown(self) -> str:
        return self.email


class ListToDict(BaseModel):
    items: list[Inner]

    @field_serializer("items")
    def _as_dict(self, value: list[Inner]) -> dict[str, object]:
        return {str(index): item.model_dump() for index, item in enumerate(value)}


class ListToList(BaseModel):
    items: list[Inner]

    @field_serializer("items")
    def _reversed(self, value: list[Inner]) -> list[object]:
        return [item.model_dump() for item in reversed(value)]


class PlainSerialized(BaseModel):
    inner: Annotated[Inner, PlainSerializer(lambda v: v.model_dump(), return_type=dict)]


T = TypeVar("T")


class Wrapper(BaseModel, Generic[T]):  # noqa: UP046 - pydantic generic spelling
    item: T


class RootOfInner(RootModel[list[Inner]]):
    pass


class HoldsRoot(BaseModel):
    r: RootOfInner


class Colour(Enum):
    RED = "r"


class EnumLiteral(BaseModel):
    e: Annotated[Colour, R]
    literal: Annotated[Literal["x"], R]
    p: Annotated[str, P]


@dataclasses.dataclass
class StdlibData:
    title: Annotated[str, P]
    secret: Annotated[str, R]


class HoldsStdlibData(BaseModel):
    dc: StdlibData


@pydantic_dataclass
class PydanticData:
    t: Annotated[str, P]
    email: Annotated[str, R]


class HoldsPydanticData(BaseModel):
    x: PydanticData


class Typed(TypedDict):
    t: Annotated[str, P]
    email: Annotated[str, R]


class HoldsTyped(BaseModel):
    x: Typed


class Pair(NamedTuple):
    t: Annotated[str, P]
    email: Annotated[str, R]


class HoldsPair(BaseModel):
    x: Pair


class UnionArm(BaseModel):
    u: Union[int, Inner]  # noqa: UP007 - the spelling under test


class IntKeys(BaseModel):
    d: dict[int, Inner]


class TieredExtra(BaseModel):
    model_config = ConfigDict(extra="allow")
    p: Annotated[str, P]


class ObjectField(BaseModel):
    x: object


class ObjectList(BaseModel):
    x: list[object]


class ObjectDict(BaseModel):
    d: dict[str, object]


class MixedTuple(BaseModel):
    x: tuple[Inner, int]


class Subclass(Inner):
    pass


class HoldsInner(BaseModel):
    x: Inner


class AnnotatedList(BaseModel):
    x: Annotated[list[str], R]


class OptionalList(BaseModel):
    x: list[Annotated[str, R]] | None


class OptionalDict(BaseModel):
    x: Optional[dict[str, Annotated[str, R]]]  # noqa: UP045 - the spelling under test


class RestrictedKey(BaseModel):
    x: dict[Annotated[str, R], int]


CASES: dict[str, BaseModel] = {
    "nested-annotated": NestedAnnotated(x=SECRET),
    "dict-of-lists": DictOfLists(d={"k": [_inner()]}),
    "tuple": TupleOf(t=(_inner(),)),
    "sequence": SequenceOf(s=[_inner()]),
    "mapping": MappingOf(m={"k": _inner()}),
    "frozenset-of-frozen-model": FrozenSetOf(fs=frozenset([_frozen()])),
    "set-of-frozen-model": SetOf(s={_frozen()}),
    "alias": Aliased(contact=SECRET, name="n"),  # type: ignore[call-arg]
    "model-serializer-to-text": SerializesToText(email=SECRET, name="n"),
    "nested-model-serializer-to-text": HoldsTextSerializer(
        inner=SerializesToText(email=SECRET, name="n")
    ),
    "model-serializer-renamed": RenamesOnSerialize(email=SECRET, name="n"),
    "computed-field": ComputedLeak(email=SECRET),
    "field-serializer-list-to-dict": ListToDict(items=[_inner()]),
    "field-serializer-list-to-list": ListToList(items=[_inner()]),
    "plain-serializer": PlainSerialized(inner=_inner()),
    "generic": Wrapper[Inner](item=_inner()),
    "nested-root-model": HoldsRoot(r=RootOfInner([_inner()])),
    "enum-and-literal": EnumLiteral(e=Colour.RED, literal="x", p="ok"),
    "stdlib-dataclass": HoldsStdlibData(dc=StdlibData("t", SECRET)),
    "pydantic-dataclass": HoldsPydanticData(x=PydanticData("a", SECRET)),
    "typed-dict": HoldsTyped(x={"t": "a", "email": SECRET}),
    "named-tuple": HoldsPair(x=Pair("a", SECRET)),
    "union-arm": UnionArm(u=_inner()),
    "int-keys": IntKeys(d={1: _inner()}),
    "extra-on-tiered": TieredExtra(p="x", leaked=SECRET),  # type: ignore[call-arg]
    "object-field": ObjectField(x=_inner()),
    "object-list": ObjectList(x=[_inner()]),
    "object-dict": ObjectDict(d={"a": _inner()}),
    "mixed-tuple": MixedTuple(x=(_inner(), 1)),
    "subclass-instance": HoldsInner(x=Subclass(title="t", secret=SECRET)),
    "annotated-list": AnnotatedList(x=[SECRET]),
    "optional-list": OptionalList(x=[SECRET]),
    "optional-dict": OptionalDict(x={"k": SECRET}),
    "restricted-key": RestrictedKey(x={SECRET: 1}),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_no_output_shape_sends_a_restricted_value(name: str) -> None:
    rendered = render_output(CASES[name], _policy())
    assert SECRET not in json.dumps(rendered.value, default=str), rendered


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("frozenset-of-frozen-model", {"fs": [{"title": "t"}]}),
        ("set-of-frozen-model", {"s": [{"title": "t"}]}),
        ("typed-dict", {"x": {"t": "a"}}),
        ("pydantic-dataclass", {"x": {"t": "a"}}),
        ("named-tuple", {}),
        ("field-serializer-list-to-dict", {}),
        ("nested-model-serializer-to-text", {}),
    ],
)
def test_the_review_s_leaks_are_rendered_or_withheld(
    name: str, expected: object
) -> None:
    """Sets are rendered member by member; a ``TypedDict`` field by field; a shape the
    walk cannot pair (a positional tiered tuple, a reshaping serializer, a model that
    dumps to text) is withheld whole rather than masked and sent."""
    assert render_output(CASES[name], _policy()).value == expected


def test_a_tiered_model_that_dumps_to_text_is_withheld_whole() -> None:
    rendered = render_output(CASES["model-serializer-to-text"], _policy())
    assert rendered.withheld is True
    assert rendered.value is None


# --- registration refuses what the walk cannot pair


class RefInput(BaseModel):
    ref: str


def _declaration(output: type[BaseModel]) -> OperationDeclaration:
    return OperationDeclaration(
        name="harness.shape.get",
        safety_class=SafetyClass.READ,
        roles=frozenset({Role.OWNER}),
        input_model=RefInput,
        output=output,
        idempotency=Idempotency.NONE,
        audit=None,
    )


def _handler(*args: object) -> None:
    return None


@pytest.mark.parametrize(
    "output",
    [
        SerializesToText,
        HoldsTextSerializer,
        RenamesOnSerialize,
        ListToDict,
        ListToList,
        PlainSerialized,
        HoldsPair,
    ],
    ids=lambda model: model.__name__,
)
def test_registration_refuses_an_output_the_walk_cannot_pair(
    output: type[BaseModel],
) -> None:
    assert unrenderable_paths(output)
    with pytest.raises(RegistrationRefused) as excinfo:
        OperationRegistry().register(
            _declaration(output), _handler, origin=TEST_HARNESS_ORIGIN
        )
    assert "cannot pair" in str(excinfo.value)


class UntieredSerialized(BaseModel):
    note: str

    @model_serializer
    def _text(self) -> str:
        return self.note


@pytest.mark.parametrize(
    "output",
    [UntieredSerialized, HoldsTyped, SetOf, HoldsPydanticData, DictOfLists],
    ids=lambda model: model.__name__,
)
def test_registration_accepts_an_output_the_walk_can_render(
    output: type[BaseModel],
) -> None:
    """An untiered model may serialize however it likes, and every tiered shape the
    walk pairs is accepted."""
    assert unrenderable_paths(output) == ()
    OperationRegistry().register(
        _declaration(output), _handler, origin=TEST_HARNESS_ORIGIN
    )


# --- probe_guard: the event guard walks what the output walk walks


class EventInner(BaseModel):
    email: Annotated[str, R]


@dataclasses.dataclass
class EventStdlib:
    email: Annotated[str, R]


@pydantic_dataclass
class EventPydantic:
    email: Annotated[str, R]


class EventTyped(TypedDict):
    email: Annotated[str, R]


class EventPair(NamedTuple):
    email: Annotated[str, R]


class GuardStdlib(BaseModel):
    d: EventStdlib


class GuardPydantic(BaseModel):
    d: EventPydantic


class GuardTyped(BaseModel):
    d: EventTyped


class GuardPair(BaseModel):
    d: EventPair


class GuardRoot(RootModel[list[EventInner]]):
    pass


class GuardGeneric(BaseModel):
    w: Wrapper[EventInner]


class GuardAnnotatedOptional(BaseModel):
    x: Annotated[EventInner, "meta"] | None


class GuardDictValue(BaseModel):
    x: dict[str, EventInner]


@pytest.mark.parametrize(
    ("data", "path"),
    [
        (GuardStdlib, "d.email"),
        (GuardPydantic, "d.email"),
        (GuardTyped, "d.email"),
        (GuardPair, "d.email"),
        (GuardRoot, "root.email"),
        (GuardGeneric, "w.item.email"),
        (GuardAnnotatedOptional, "x.email"),
        (GuardDictValue, "x.email"),
    ],
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_the_event_guard_finds_a_restricted_field_in_every_nested_kind(
    data: type[BaseModel], path: str
) -> None:
    assert restricted_fields(data) == (path,)


# --- probe_excl: an excluded record is dropped before tiering can hide its ref


class UnmarkedRef(BaseModel):
    ref: str
    title: Annotated[str, P]


class InternalRef(BaseModel):
    ref: Annotated[str, Tiered(SensitivityTier.INTERNAL)]
    title: Annotated[str, P]


class OtherName(BaseModel):
    note_ref: str
    title: str


class TypedRef(BaseModel):
    ref: RecordRef
    title: str


class RefKeyed(BaseModel):
    by_ref: dict[str, str]


def test_an_unmarked_ref_on_a_tiered_class_still_withholds_the_record() -> None:
    rendered = render_output(UnmarkedRef(ref=EXCLUDED_REF, title="HIDDEN"), _policy())
    assert rendered.withheld is True


def test_an_internal_ref_under_a_purpose_without_internal_still_withholds() -> None:
    rendered = render_output(
        InternalRef(ref=EXCLUDED_REF, title="HIDDEN"),
        _policy(ContextPurpose.SHARE_WITH_REFERRAL),
    )
    assert rendered.withheld is True


def test_other_excluded_reference_shapes() -> None:
    policy = _policy()
    assert render_output(
        TypedRef(ref=RecordRef.parse(EXCLUDED_REF), title="HIDDEN"), policy
    ).withheld
    assert render_output(RefKeyed(by_ref={EXCLUDED_REF: "HIDDEN"}), policy).value == {
        "by_ref": {}
    }
    # A reference under another name is a pointer, not the record: the pointer goes,
    # the holder's own fields stay. A reference inside prose is not matched.
    assert render_output(
        OtherName(note_ref=EXCLUDED_REF, title="kept"), policy
    ).value == {"title": "kept"}


# --- probe_mask: patterns and token normalisation


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+" + phone("44", "20", "7946", "0958", sep=" "), PHONE_MASK),
        ("+" + phone("33", "1", "23", "45", "67", "89", sep=" "), PHONE_MASK),
        (f"call ({AREA}) {phone(EXCHANGE, LINE)}", f"call {PHONE_MASK}"),
        (phone(AREA, EXCHANGE, LINE, sep="."), PHONE_MASK),
        ("+1" + phone(AREA, EXCHANGE, LINE, sep=""), PHONE_MASK),
        (phone("1", AREA, EXCHANGE, LINE), PHONE_MASK),
        (phone(AREA, EXCHANGE, LINE, sep=" "), PHONE_MASK),
        ("tel:+" + phone("1", AREA, EXCHANGE, LINE), f"tel:{PHONE_MASK}"),
        ("Tel." + PROBE_PHONE, f"Tel.{PHONE_MASK}"),
        (PROBE_PHONE + "x12", PHONE_MASK),
        (PROBE_PHONE + " ext. 7", PHONE_MASK),
        (phone(AREA, EXCHANGE, LINE, sep="\u2011"), PHONE_MASK),
        ("+" + phone("1", AREA, EXCHANGE, LINE, sep="."), PHONE_MASK),
        (f"({AREA})" + phone(EXCHANGE, LINE), PHONE_MASK),
        (PROBE_PHONE + ".", f"{PHONE_MASK}."),
        ("+" + phone("86", "138", "0013", "8000", sep=" "), PHONE_MASK),
        ("+7 912 " + phone("345", "67", "89"), PHONE_MASK),
        ("pat.rivera@example.com", EMAIL_MASK),
        ("PAT@EXAMPLE.COM", EMAIL_MASK),
        ("mailto:pat@example.com", f"mailto:{EMAIL_MASK}"),
        ("pat+tag@sub.mail.example.org", EMAIL_MASK),
    ],
)
def test_the_probe_s_contact_values_mask(text: str, expected: str) -> None:
    assert mask_contact_values(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-25T10:30:00Z",
        "version 1.2.3",
        "192.0.2.1",
        "10." + phone(AREA, EXCHANGE, LINE, sep="."),
        "ISBN 978-3-16-148410-0",
        # A bare ten-digit run stays: ids and counts look the same.
        phone(AREA, EXCHANGE, LINE, sep=""),
        "recallatron.memory:0192f0a0-0000-7000-8000-000000000000",
        "uuid 123e4567-e89b-12d3-a456-426614174000",
        "pat (at) example.com",
        "pat@example",
        phone(AREA, EXCHANGE, LINE, sep=" - "),
        phone(AREA, EXCHANGE, LINE, sep="/"),
        phone(AREA, EXCHANGE, LINE, sep="_"),
        "port 8080 9090 1000",
        "secret:/vault/x",
    ],
)
def test_the_probe_s_non_contacts_are_left_alone(text: str) -> None:
    assert mask_contact_values(mask_secret_references(text)) == text


def test_secret_references_mask_in_any_case() -> None:
    assert mask_secret_references("SECRET://vault/abc") == SECRET_MASK


@pytest.mark.parametrize(
    "text",
    [
        "x [EMAIL WITHHELD]",
        "[email withheld]",
        "[email  withheld]",
        "［email withheld］",  # full-width brackets
        "[Phone Withheld]",
        "[SECRET REFERENCE WITHHELD]",
    ],
)
def test_mask_tokens_are_matched_after_normalisation(text: str) -> None:
    assert list(mask_tokens_in({"body": text}))
