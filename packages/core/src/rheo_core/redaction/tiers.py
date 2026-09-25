"""The three redaction tiers, the field marker, and the walks that read it.

Two declaration forms, one per kind of thing that carries fields, and they do not
overlap:

- **A record type's fields** are tiered in the owning manifest's ``sensitivity`` map
  (record type name -> field name -> tier), which is what ``module-contract.md``'s
  ``sensitivity`` row ratifies. The context builder reads it through
  :mod:`rheo_core.redaction.registry`.
- **A pydantic model's fields** (an event's ``data`` model, an operation's output
  model) are tiered with :class:`Tiered` in the field's ``Annotated`` metadata::

      email: Annotated[str, Tiered(SensitivityTier.RESTRICTED)]

  The manifest guard reads it off an event model and refuses a restricted one; the
  tool facade reads it off an output model and drops what the policy does not allow.

**Why a model is not tiered by the manifest map's field names.** The obvious shortcut,
"a model field named like a restricted record field is restricted", refuses events
the design names on purpose: ``leads.contact_permission.withdrawn`` carries
``party_ref, purpose, channel``, and a contact-permission record is ``restricted``
whole, so the shortcut would refuse the one event whose job is to say a permission
changed. A field's tier is a statement about that field, so it is written on it.

**A model that tiers any field tiers all of them.** :func:`model_field_tiers` reports
whether a class declared any marker at all, and the output renderer treats an
unmarked field on such a class as withheld. That is the same fail-closed rule the
default record renderer applies to a record type with a tier map, stated once for
both forms: an author who started declaring tiers and missed a field loses the field,
not the secret. A class with no marker anywhere is untiered, which is every model in
release one and why Recallatron's tools answer exactly as before.
"""

import dataclasses
import types
import typing
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import BaseModel
from pydantic.fields import FieldInfo


class SensitivityTier(StrEnum):
    """The three redaction tiers a field can carry (``runtime-and-mcp.md`` § Tiers).

    Moved here from ``rheo_core.modules.manifest``, which re-exports it, so every
    import path that named it there still does. It moved because the enforcement needs
    it below the manifest: the manifest imports this package's renderer shapes, and a
    tier defined in the manifest would close that cycle.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


_RANK: Final[Mapping[SensitivityTier, int]] = {
    SensitivityTier.PUBLIC: 0,
    SensitivityTier.INTERNAL: 1,
    SensitivityTier.RESTRICTED: 2,
}


def most_restrictive(*tiers: SensitivityTier) -> SensitivityTier:
    """The highest of ``tiers``; ``public`` when given none."""
    return max(tiers, key=_RANK.__getitem__, default=SensitivityTier.PUBLIC)


@dataclass(frozen=True, slots=True)
class Tiered:
    """``Annotated`` metadata declaring one model field's redaction tier.

    A dataclass rather than a bare ``SensitivityTier`` in the metadata, so a tier
    cannot be mistaken for some other ``StrEnum`` a library left in the same list, and
    so a reader grepping for ``Tiered(`` finds every declaration.
    """

    tier: SensitivityTier


def structured_kind(annotation: object) -> str | None:
    """What kind of field-bearing class ``annotation`` is, or ``None`` for others.

    The four shapes whose fields carry tiers, and the only four the walks descend into:
    a pydantic model (``RootModel`` and a parametrised generic model included), a
    dataclass (stdlib or pydantic), a ``TypedDict`` and a ``NamedTuple``. One function
    for the guard and the output walk alike, so neither can descend into a shape the
    other skips (the gap the second review found: the guard saw models only).
    """
    if not isinstance(annotation, type):
        return None
    if issubclass(annotation, BaseModel):
        return "model"
    if dataclasses.is_dataclass(annotation):
        return "dataclass"
    if typing.is_typeddict(annotation):
        return "typeddict"
    if issubclass(annotation, tuple) and hasattr(annotation, "_fields"):
        return "namedtuple"
    return None


def annotation_tier(annotation: object) -> SensitivityTier | None:
    """The strictest :class:`Tiered` marker anywhere in ``annotation``'s spelling.

    Walks ``Annotated`` metadata, union arms (``X | None``, ``Optional[X]``,
    ``Union[...]``) and container arguments (``list[...]``, ``dict[..., ...]``,
    ``tuple[...]``), so ``Annotated[str, Tiered(R)] | None`` and
    ``list[Annotated[str, Tiered(R)]]`` tier the field exactly as
    ``Annotated[str, Tiered(R)]`` does: a restricted element makes the field that
    holds it restricted. It stops at a field-bearing class (:func:`structured_kind`),
    whose markers tier *its* fields, not the field holding it.
    """
    found: list[SensitivityTier] = []

    def walk(current: object) -> None:
        if structured_kind(current) is not None:
            return
        if typing.get_origin(current) is typing.Annotated:
            base, *metadata = typing.get_args(current)
            found.extend(item.tier for item in metadata if isinstance(item, Tiered))
            walk(base)
            return
        if typing.get_origin(current) is None and not isinstance(
            current, types.UnionType
        ):
            return
        for argument in typing.get_args(current):
            walk(argument)

    walk(annotation)
    return most_restrictive(*found) if found else None


def field_tier(info: FieldInfo) -> SensitivityTier | None:
    """The tier ``info`` declares, or ``None`` when it declares none.

    Pydantic lifts a top-level ``Annotated`` marker into ``info.metadata`` and keeps
    the rest of the spelling on ``info.annotation``; both are read, and the strictest
    marker wins, so wrapping or nesting can only tighten.
    """
    return annotation_tier(_full_annotation(info))


def _full_annotation(info: FieldInfo) -> object:
    """``info``'s annotation with the metadata pydantic lifted off it put back."""
    if info.metadata:
        return typing.Annotated[(info.annotation, *info.metadata)]
    return info.annotation


class UnresolvedModel(ValueError):
    """A model whose field types cannot be resolved (a forward reference that names
    nothing yet), so no walk can see what its fields hold."""


def _complete(model: type[BaseModel]) -> None:
    if model.__pydantic_complete__:
        return
    try:
        model.model_rebuild(raise_errors=True)
    except Exception as exc:
        raise UnresolvedModel(
            f"model {model.__qualname__!r} has unresolved forward references ({exc}); "
            "its fields cannot be checked for a restricted tier"
        ) from exc


def structured_fields(cls: type) -> dict[str, object]:
    """Field name -> full annotation (markers included), for a field-bearing class.

    Raises :class:`UnresolvedModel` when the annotations cannot be resolved.
    """
    kind = structured_kind(cls)
    if kind == "model":
        assert issubclass(cls, BaseModel)
        _complete(cls)
        return {name: _full_annotation(info) for name, info in cls.model_fields.items()}
    try:
        hints = typing.get_type_hints(cls, include_extras=True)
    except Exception as exc:
        raise UnresolvedModel(
            f"{cls.__qualname__!r} has unresolved annotations ({exc}); its fields "
            "cannot be checked for a restricted tier"
        ) from exc
    if kind == "dataclass":
        return {
            item.name: hints.get(item.name, item.type)
            for item in dataclasses.fields(cls)
        }
    if kind == "namedtuple":
        return {name: hints.get(name, object) for name in getattr(cls, "_fields", ())}
    if kind == "typeddict":
        return dict(hints)
    return {}


def type_tiers(cls: type) -> Mapping[str, SensitivityTier]:
    """Field name -> declared tier, for the fields of ``cls`` that carry a marker.

    Empty for an untiered class; see the module docstring for what a non-empty answer
    means for the fields it leaves out.
    """
    tiers: dict[str, SensitivityTier] = {}
    for name, annotation in structured_fields(cls).items():
        tier = annotation_tier(annotation)
        if tier is not None:
            tiers[name] = tier
    return tiers


def model_field_tiers(model: type[BaseModel]) -> Mapping[str, SensitivityTier]:
    """:func:`type_tiers` for a pydantic model, the name the renderers started with."""
    return type_tiers(model)


def nested_structured(annotation: object) -> Iterator[type]:
    """Every field-bearing class reachable through ``annotation``'s type arguments.

    ``list[Item]``, ``Item | None``, ``tuple[Item, ...]``, ``dict[str, Item]``,
    ``frozenset[Item]`` and an ``Annotated`` wrapper all reach ``Item``, for all four
    kinds :func:`structured_kind` names. It stops at the class; the callers descend.
    """
    if structured_kind(annotation) is not None:
        assert isinstance(annotation, type)
        yield annotation
        return
    origin = typing.get_origin(annotation)
    if origin is None and not isinstance(annotation, types.UnionType):
        return
    for argument in typing.get_args(annotation):
        yield from nested_structured(argument)


_CARRIES: dict[type, bool] = {}


def class_carries_tiers(cls: type) -> bool:
    """Whether ``cls`` or anything its fields reach declares a tier.

    Fail closed: a class whose annotations cannot be resolved is treated as carrying
    tiers, since the renderer would otherwise send what it could not read.
    """
    cached = _CARRIES.get(cls)
    if cached is not None:
        return cached
    _CARRIES[cls] = False  # a recursive class reaching itself adds nothing new
    try:
        fields = structured_fields(cls)
    except UnresolvedModel:
        _CARRIES[cls] = True
        return True
    carries = any(carries_tiers(annotation) for annotation in fields.values())
    _CARRIES[cls] = carries
    return carries


def carries_tiers(annotation: object) -> bool:
    """Whether a value of ``annotation`` could hold a tiered field anywhere inside."""
    if annotation_tier(annotation) is not None:
        return True
    return any(class_carries_tiers(cls) for cls in nested_structured(annotation))


def restricted_fields(model: type) -> tuple[str, ...]:
    """Dotted paths of every ``restricted``-marked field in ``model``, nested included.

    The manifest guard's reader, over the same traversal the output renderer uses
    (:func:`structured_fields`, :func:`nested_structured`): models, dataclasses,
    ``TypedDict`` and ``NamedTuple`` alike. Nested classes are walked because an event
    whose ``data`` holds a sub-record carrying a contact value carries that value
    exactly as much as one holding it at the top level. A class reached twice (a
    recursive model, or one sub-model used by two fields) is walked once.

    **Raises :class:`UnresolvedModel`** for a class, top-level or nested, whose forward
    references do not resolve: a field that cannot be typed is a field this walk cannot
    see, and passing it would be the guard answering "no restricted field" about a
    field it never read. A field typed ``Any`` or ``dict`` is a different limit and not
    refused: it carries no marker to find, which ``intake-and-events.md`` records.
    """
    found: list[str] = []
    seen: set[type] = set()

    def walk(current: type, prefix: str) -> None:
        if current in seen:
            return
        seen.add(current)
        for name, annotation in structured_fields(current).items():
            path = f"{prefix}{name}"
            if annotation_tier(annotation) is SensitivityTier.RESTRICTED:
                found.append(path)
            for nested in nested_structured(annotation):
                walk(nested, f"{path}.")

    walk(model, "")
    return tuple(found)


def unrenderable_paths(model: type) -> tuple[str, ...]:
    """Where an output type holds a tiered value the output walk could not pair.

    The renderer withholds such a value at run time; this is the registration-time
    refusal of the same shapes, so a module finds out when it registers the
    operation rather than when a model is shown nothing. Three shapes:

    - a pydantic model that carries tiers and declares a ``@model_serializer``, which
      can turn the whole record into a string or a renamed mapping;
    - a field that carries tiers and has a ``@field_serializer``, a ``PlainSerializer``
      or a ``WrapSerializer``, which can reshape it (a ``list[Inner]`` into a dict);
    - a ``NamedTuple`` that carries tiers, which dumps positionally, so a withheld
      element cannot be dropped without moving the others.
    """
    found: list[str] = []
    seen: set[type] = set()

    def walk(current: type, prefix: str) -> None:
        if current in seen:
            return
        seen.add(current)
        kind = structured_kind(current)
        label = prefix.rstrip(".") or current.__qualname__
        if kind == "namedtuple" and class_carries_tiers(current):
            found.append(f"{label} (a tiered NamedTuple)")
            return
        field_serialized: set[str] = set()
        if kind == "model":
            assert issubclass(current, BaseModel)
            decorators = current.__pydantic_decorators__
            if decorators.model_serializers and class_carries_tiers(current):
                found.append(f"{label} (a model_serializer on a tiered model)")
            for decorator in decorators.field_serializers.values():
                field_serialized.update(decorator.info.fields)
        for name, annotation in structured_fields(current).items():
            path = f"{prefix}{name}"
            if carries_tiers(annotation) and (
                name in field_serialized or _has_serializer(annotation)
            ):
                found.append(f"{path} (a custom serializer on a tiered field)")
            for nested in nested_structured(annotation):
                walk(nested, f"{path}.")

    walk(model, "")
    return tuple(found)


def _has_serializer(annotation: object) -> bool:
    from pydantic.functional_serializers import PlainSerializer, WrapSerializer

    if typing.get_origin(annotation) is typing.Annotated:
        _, *metadata = typing.get_args(annotation)
        return any(
            isinstance(item, PlainSerializer | WrapSerializer) for item in metadata
        )
    return False
