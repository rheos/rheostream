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


def annotation_tier(annotation: object) -> SensitivityTier | None:
    """The strictest :class:`Tiered` marker anywhere in ``annotation``'s spelling.

    Walks ``Annotated`` metadata, union arms (``X | None``, ``Optional[X]``,
    ``Union[...]``) and container arguments (``list[...]``, ``dict[..., ...]``,
    ``tuple[...]``), so ``Annotated[str, Tiered(R)] | None`` and
    ``list[Annotated[str, Tiered(R)]]`` tier the field exactly as
    ``Annotated[str, Tiered(R)]`` does: a restricted element makes the field that
    holds it restricted. It stops at a ``BaseModel`` class, whose markers tier *its*
    fields, not the field holding it (:func:`restricted_fields` walks into it).
    """
    found: list[SensitivityTier] = []

    def walk(current: object) -> None:
        if isinstance(current, type) and issubclass(current, BaseModel):
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
    marked = [item.tier for item in info.metadata if isinstance(item, Tiered)]
    nested = annotation_tier(info.annotation)
    if nested is not None:
        marked.append(nested)
    if not marked:
        return None
    return most_restrictive(*marked)


def model_field_tiers(model: type[BaseModel]) -> Mapping[str, SensitivityTier]:
    """Field name -> declared tier, for the fields of ``model`` that carry a marker.

    Empty for an untiered class; see the module docstring for what a non-empty answer
    means for the fields it leaves out.
    """
    tiers: dict[str, SensitivityTier] = {}
    for name, info in model.model_fields.items():
        tier = field_tier(info)
        if tier is not None:
            tiers[name] = tier
    return tiers


def _nested_models(annotation: object) -> Iterator[type[BaseModel]]:
    """Every ``BaseModel`` subclass reachable through ``annotation``'s type arguments.

    ``list[Item]``, ``Item | None``, ``tuple[Item, ...]``, ``dict[str, Item]`` and an
    ``Annotated`` wrapper all reach ``Item``. A plain type that is not a model reaches
    nothing.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    origin = typing.get_origin(annotation)
    if origin is None and not isinstance(annotation, types.UnionType):
        return
    for argument in typing.get_args(annotation):
        yield from _nested_models(argument)


class UnresolvedModel(ValueError):
    """A model whose field types pydantic has not resolved (a forward reference that
    names nothing yet), so no walk can see what its fields hold."""


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


def restricted_fields(model: type[BaseModel]) -> tuple[str, ...]:
    """Dotted paths of every ``restricted``-marked field in ``model``, nested included.

    The manifest guard's reader. Nested models are walked because an event whose
    ``data`` holds a sub-model carrying a contact value carries that value exactly as
    much as one holding it at the top level. A class reached twice (a recursive model,
    or one sub-model used by two fields) is walked once.

    **Raises :class:`UnresolvedModel`** for a model, top-level or nested, whose forward
    references do not resolve: a field pydantic cannot type is a field this walk cannot
    see, and passing it would be the guard answering "no restricted field" about a
    field it never read. A field typed ``Any`` or ``dict`` is a different limit and not
    refused: it carries no marker to find, which ``intake-and-events.md`` records.
    """
    found: list[str] = []
    seen: set[type[BaseModel]] = set()

    def walk(current: type[BaseModel], prefix: str) -> None:
        if current in seen:
            return
        seen.add(current)
        _complete(current)
        for name, info in current.model_fields.items():
            path = f"{prefix}{name}"
            if field_tier(info) is SensitivityTier.RESTRICTED:
                found.append(path)
            for nested in _nested_models(info.annotation):
                walk(nested, f"{path}.")

    walk(model, "")
    return tuple(found)
