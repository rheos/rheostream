"""Rendering for a model: records for the context builder, results for the tool facade.

**The record half.** A record type that is tiered (it has a ``sensitivity`` entry, or
declares its own ``render_for_model``) also declares ``load_for_model``, a
:class:`RecordLoader` that reads the record's fields under the caller's context. The
context builder then renders those fields with the module's :class:`ModelRenderer`
when it declares one, and with :func:`render_record` otherwise. ``render_for_model``
has the ratified shape, ``(record, tier_policy)``; the loader is the part the ratified
text left implicit, because a resolver answers a record *head* (a reference, a label,
readability) and never the fields a tier map is about.

**The default record renderer fails closed on an undeclared field.** A record type
with a tier map has declared that its fields are tiered, so a field the loader returns
and the map does not name has no tier anyone chose. The design is silent on it;
treating it as ``restricted`` (omitted) is the safe reading, and the one that makes a
loader gaining a column a non-event for the redaction contract rather than a leak.

**The output half.** :func:`render_output` turns an operation's output model into the
JSON a tool returns: fields whose :class:`~rheo_core.redaction.tiers.Tiered` marker
the policy does not allow are removed, an unmarked field on a class that marks any is
removed (see :mod:`rheo_core.redaction.tiers`), and every string left is passed
through :func:`~rheo_core.redaction.masking.mask_for_model`. The walk follows the
model *instance* rather than its annotations, so a union field reaches the class the
value actually is.
"""

import dataclasses
import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol

from pydantic import BaseModel, RootModel
from pydantic_core import to_jsonable_python
from rheo_contracts import RecordRef, RecordRefMalformed, WorkspaceContext

from rheo_core.redaction.masking import mask_for_model
from rheo_core.redaction.policy import TierPolicy
from rheo_core.redaction.tiers import (
    SensitivityTier,
    UnresolvedModel,
    carries_tiers,
    class_carries_tiers,
    most_restrictive,
    structured_fields,
    structured_kind,
    type_tiers,
)
from rheo_core.storage.backend import UnitOfWork


class RecordLoader(Protocol):
    """``(ctx, uow, ref) -> fields | None``: one record's fields as ``ctx`` may read
    them, or ``None`` when it may not (or the record is gone).

    Runs inside the context builder's unit of work, after the resolver has already
    answered ``readable`` and ``live`` for the same reference; ``None`` here is the
    loader's own second answer and the builder skips the reference on it.
    """

    def __call__(
        self, ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef, /
    ) -> Mapping[str, object] | None: ...


class ModelRenderer(Protocol):
    """``(record, tier_policy) -> text | None``: a module's own rendering of one
    record's fields for a model, or ``None`` to send nothing for it.

    The builder still masks the returned text (secret references always, contact
    values unless the policy allows them), so a module renderer can widen nothing the
    mask covers; what it can do that the default cannot is release its own contact
    point fields when :attr:`TierPolicy.contact_points_allowed` is true.
    """

    def __call__(
        self, record: Mapping[str, object], policy: TierPolicy, /
    ) -> str | None: ...


@dataclass(frozen=True, slots=True)
class RenderedRecord:
    """Text for one context item, and the highest tier of what went into it."""

    text: str
    tier: SensitivityTier


def _scalar_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return str(value)


def render_record(
    record: Mapping[str, object],
    tiers: Mapping[str, SensitivityTier],
    policy: TierPolicy,
) -> RenderedRecord | None:
    """The default ``render_for_model``: the allowed fields, one ``name: value`` line
    each, in the loader's order; ``None`` when no field is allowed.

    A ``None`` value is left out rather than printed, since it tells a model nothing
    and would still spend context bytes.
    """
    lines: list[str] = []
    included: list[SensitivityTier] = []
    for name, value in record.items():
        tier = tiers.get(name)
        if tier is None or not policy.allows(tier) or value is None:
            continue
        lines.append(f"{name}: {mask_for_model(_scalar_text(value), policy)}")
        included.append(tier)
    if not lines:
        return None
    return RenderedRecord(text="\n".join(lines), tier=most_restrictive(*included))


WITHHELD_REASON: Final = (
    "the result is a record of a type this workspace excludes from models "
    "(<module>.redaction.exclude_types)"
)


@dataclass(frozen=True, slots=True)
class RenderedResult:
    """A tool result as it may leave for a model.

    ``value`` is the JSON-ready rendering. ``withheld`` is true when the result itself
    is a record of an excluded type (its own ``ref`` names one), in which case
    ``value`` is ``None`` and the transport says so rather than sending anything.
    """

    value: object
    withheld: bool = False
    reason: str | None = None


class _Withheld:
    """Sentinel: drop this value from the rendering."""


_DROP: Final = _Withheld()


def _json_key(key: object) -> str:
    """The key pydantic's JSON dump writes for a mapping key ``key``."""
    if isinstance(key, str):
        return key
    if isinstance(key, Enum):
        return str(key.value)
    if isinstance(key, bool):
        return "true" if key else "false"
    return str(key)


class _OutputWalk:
    """One tool result's walk: the instance and its JSON dump, side by side.

    The instance says what each value *is* (a tiered model, a dataclass, a reference);
    the dump is what would be sent. Wherever the two cannot be paired (a set, whose
    dump order is arbitrary; a serializer that changed a value's shape; a mapping key
    that does not dump to what the walk expects), the fallback is the dump itself with
    every string masked and every excluded reference dropped: never the dump raw.
    """

    def __init__(self, policy: TierPolicy) -> None:
        self.policy = policy

    def _excluded(self, value: object) -> bool:
        ref: RecordRef | None
        if isinstance(value, RecordRef):
            ref = value
        elif isinstance(value, str) and ":" in value and "." in value:
            try:
                ref = RecordRef.parse(value)
            except (RecordRefMalformed, ValueError, TypeError):
                ref = None
        else:
            ref = None
        if ref is None:
            return False
        return ref.record_type in self.policy.excluded_types(ref.module)

    def json(self, dumped: object) -> object:
        """The fallback: ``dumped`` with strings masked and excluded refs dropped."""
        if isinstance(dumped, str):
            if self._excluded(dumped):
                return _DROP
            return mask_for_model(dumped, self.policy)
        if isinstance(dumped, dict):
            rendered: dict[str, object] = {}
            for key, item in dumped.items():
                value = self.json(item)
                if value is _DROP or self._excluded(key):
                    continue
                rendered[mask_for_model(str(key), self.policy)] = value
            return rendered
        if isinstance(dumped, list):
            items = [self.json(item) for item in dumped]
            return [item for item in items if item is not _DROP]
        return dumped

    def value(self, value: object, dumped: object, annotation: object = None) -> object:
        """``value`` rendered, paired with its dump and, where known, its annotation.

        **Fail closed where pairing fails.** When the instance and its dump cannot be
        walked side by side (a serializer reshaped it, a model dumped to a string, a
        tiered ``NamedTuple`` dumped positionally), the fallback is the masked dump
        only when nothing tiered could be inside; if the annotation or the instance
        carries any tier, the value is withheld whole. The second review found the
        old fallback masking such a value and sending its restricted fields.
        """
        if isinstance(value, RecordRef):
            return _DROP if self._excluded(value) else self.json(dumped)
        if isinstance(value, RootModel):
            root = type(value).model_fields["root"]
            return self.value(value.root, dumped, root.annotation)
        if isinstance(value, BaseModel) and isinstance(dumped, dict):
            return self.fields(value, type(value), dumped, getattr)
        if (
            dataclasses.is_dataclass(value)
            and not isinstance(value, type)
            and isinstance(dumped, dict)
        ):
            return self.fields(value, type(value), dumped, getattr)
        typed_dict = _typed_dict_of(annotation)
        if typed_dict is not None and isinstance(value, Mapping):
            if isinstance(dumped, dict):
                return self.fields(value, typed_dict, dumped, _mapping_get)
            return self.unpaired(value, dumped, annotation)
        if isinstance(value, tuple) and hasattr(value, "_fields"):
            if class_carries_tiers(type(value)):
                return _DROP
        if isinstance(value, Mapping) and isinstance(dumped, dict):
            return self.mapping(value, dumped, _argument(annotation, 1))
        if (
            isinstance(value, list | tuple)
            and isinstance(dumped, list)
            and len(value) == len(dumped)
        ):
            items = [
                self.value(item, part, _element(annotation, index))
                for index, (item, part) in enumerate(zip(value, dumped, strict=True))
            ]
            return [item for item in items if item is not _DROP]
        if isinstance(value, set | frozenset):
            # A set's dump order is arbitrary, so its members are rendered one by one
            # against their own dumps rather than paired by position.
            element = _argument(annotation, 0)
            rendered = [
                self.value(item, to_jsonable_python(item), element) for item in value
            ]
            return [item for item in rendered if item is not _DROP]
        if isinstance(dumped, str) and not isinstance(value, BaseModel):
            if self._excluded(dumped):
                return _DROP
            return mask_for_model(dumped, self.policy)
        return self.unpaired(value, dumped, annotation)

    def unpaired(self, value: object, dumped: object, annotation: object) -> object:
        if carries_tiers(annotation) or _instance_carries(value):
            return _DROP
        return self.json(dumped)

    def mapping(
        self,
        value: Mapping[object, object],
        dumped: dict[str, object],
        item_annotation: object,
    ) -> object:
        # A mapping's keys are data, not field names, so they are masked like values,
        # and an entry whose key does not dump to what the walk expects is withheld
        # rather than passed through unpaired (a UUID or int key, before this rule).
        rendered: dict[str, object] = {}
        for key, item in value.items():
            json_key = _json_key(key)
            if json_key not in dumped or self._excluded(key):
                continue
            part = self.value(item, dumped[json_key], item_annotation)
            if part is _DROP:
                continue
            rendered[mask_for_model(json_key, self.policy)] = part
        return rendered

    def fields(
        self,
        value: object,
        cls: type,
        dumped: dict[str, object],
        get: Callable[[object, str, object], object],
    ) -> object:
        # The object's own reference first, before tiering can drop it: an unmarked or
        # ``internal`` ``ref`` on a tiered class is removed by the tier loop, and the
        # "this object is an excluded record" rule has to see it before that happens.
        if self._excluded(get(value, "ref", None)) or self._excluded(dumped.get("ref")):
            return _DROP
        try:
            annotations = structured_fields(cls)
            tiers = type_tiers(cls)
        except UnresolvedModel:
            return _DROP
        rendered: dict[str, object] = {}
        for key, item in dumped.items():
            if tiers:
                tier = tiers.get(key)
                if tier is None or not self.policy.allows(tier):
                    continue
            part = self.value(get(value, key, item), item, annotations.get(key))
            if part is _DROP:
                if key == "ref":
                    return _DROP
                continue
            rendered[key] = part
        return rendered


def _mapping_get(value: object, key: str, default: object) -> object:
    assert isinstance(value, Mapping)
    return value.get(key, default)


def _unwrap(annotation: object) -> object:
    """``annotation`` without an ``Annotated`` wrapper, and without ``| None``."""
    while typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    if typing.get_origin(annotation) is typing.Union or isinstance(
        annotation, types.UnionType
    ):
        arms = [arm for arm in typing.get_args(annotation) if arm is not type(None)]
        if len(arms) == 1:
            return _unwrap(arms[0])
    return annotation


def _typed_dict_of(annotation: object) -> type | None:
    bare = _unwrap(annotation)
    return bare if structured_kind(bare) == "typeddict" else None  # type: ignore[return-value]


def _argument(annotation: object, index: int) -> object:
    """The ``index``-th type argument of a container annotation, or ``None``."""
    arguments = typing.get_args(_unwrap(annotation))
    if len(arguments) > index:
        return arguments[index]
    return None


def _element(annotation: object, position: int) -> object:
    """A sequence element's annotation: ``list[X]``/``tuple[X, ...]`` give ``X``,
    ``tuple[A, B]`` gives the one at ``position``."""
    arguments = typing.get_args(_unwrap(annotation))
    if not arguments:
        return None
    if len(arguments) == 2 and arguments[1] is Ellipsis:
        return arguments[0]
    if len(arguments) == 1:
        return arguments[0]
    return arguments[position] if position < len(arguments) else None


def _instance_carries(value: object) -> bool:
    """Whether ``value`` is, or holds, an instance of a tier-carrying class."""
    if isinstance(value, BaseModel) or (
        dataclasses.is_dataclass(value) and not isinstance(value, type)
    ):
        return class_carries_tiers(type(value))
    if isinstance(value, tuple) and hasattr(value, "_fields"):
        return class_carries_tiers(type(value))
    if isinstance(value, list | tuple | set | frozenset):
        return any(_instance_carries(item) for item in value)
    if isinstance(value, Mapping):
        return any(_instance_carries(item) for item in value.values())
    return False


UNRENDERABLE_REASON: Final = (
    "the result holds a tiered value whose serialized shape cannot be rendered "
    "under its tiers"
)


def render_output(model: BaseModel, policy: TierPolicy) -> RenderedResult:
    """``model`` as the result a tool returns to a model under ``policy``.

    Dumped with ``mode="json"`` first, so the answer is exactly what the transport
    would have sent, minus what the policy withholds. Keys are field names, the dump's
    default, which is also what the markers are keyed by. A ``RootModel`` renders its
    root, whatever JSON shape that is.

    **Excluded record types** (``<module>.redaction.exclude_types``): a string or
    ``RecordRef`` naming one is dropped, and an object whose own ``ref`` names one is
    dropped whole; a result that is itself such a record comes back ``withheld`` with
    :data:`WITHHELD_REASON`. A result the walk had to withhold whole for any other
    reason (a tiered shape it cannot pair, which operation registration refuses in the
    first place) comes back ``withheld`` with :data:`UNRENDERABLE_REASON`.
    """
    walk = _OutputWalk(policy)
    rendered = walk.value(model, model.model_dump(mode="json"))
    if rendered is _DROP:
        excluded = walk._excluded(getattr(model, "ref", None))
        return RenderedResult(
            value=None,
            withheld=True,
            reason=WITHHELD_REASON if excluded else UNRENDERABLE_REASON,
        )
    return RenderedResult(value=rendered)
