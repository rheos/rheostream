"""Finance's reader READ operations as core operations and tools (ADR-0028).

Three parts:

- ``output_view``: the registered output of a finance output model. Core reads only
  its own ``Tiered`` marker, so a finance model is untiered to core and would answer
  its whole dump. The view is derived from finance's own marks: a restricted field is
  left out of it, a public or internal one carries core's marker of the same tier,
  and a field with no finance tier refuses the import (fail closed). No validator or
  serializer is copied, and ``model_validate`` with ``from_attributes`` reads only the
  view's fields off the finance result, so a restricted value is never copied.
- ``read_handler``: runs one spec's handler on core's unit of work under the
  workspace's layout, as ``finance_reader`` (``SET LOCAL ROLE``, reset before it
  returns, since core goes on using the transaction), and returns the view. A finance
  failure leaves as an ``OperationRefused`` whose state and detail are one fixed code
  (``wiring.errors``); core's own refusals and anything else pass unchanged, which
  core answers with the class name alone.
- ``OPERATIONS`` and ``TOOLS``: every READ spec on the reader connection that is not
  owner-only, declared for the owner alone. The manifest sets ``explicit_only``, so
  only a ``module_read:finance`` token reaches them.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal, Union

from pydantic import BaseModel, ConfigDict, create_model
from pydantic_core import PydanticUndefined
from rheo_contracts import (
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    ToolDeclaration,
    WorkspaceContext,
)
from rheo_core.modules.readiness import ModuleNotReady
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.redaction.tiers import SensitivityTier
from rheo_core.redaction.tiers import Tiered as CoreTiered
from rheo_core.storage.backend import StorageRefusal
from rheo_core.storage.backend import UnitOfWork as CoreUnitOfWork

from rheo_finance import catalogue, db
from rheo_finance.context import UnitOfWork
from rheo_finance.contracts import Role as FinanceRole
from rheo_finance.contracts import SafetyClass as FinanceSafetyClass
from rheo_finance.contracts import Tiered
from rheo_finance.operations import OperationSpec
from rheo_finance.tier import Tier
from rheo_finance.wiring.core import handler_context, layout_for
from rheo_finance.wiring.errors import matching_code

OUTPUT_INVALID: Final = "finance_output_invalid"

# Finance's tier to core's, for the tiers a view keeps. RESTRICTED has no entry: a
# restricted field is never in a view.
CORE_TIERS: Final[Mapping[Tier, SensitivityTier]] = {
    Tier.PUBLIC: SensitivityTier.PUBLIC,
    Tier.INTERNAL: SensitivityTier.INTERNAL,
}

# Core's own refusals: core answers them itself, with text that is safe to show.
PASS_THROUGH: Final[tuple[type[BaseException], ...]] = (
    ModuleNotReady,
    StorageRefusal,
)

VIEW_CONFIG: Final = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

_VIEWS: dict[type[BaseModel], type[BaseModel]] = {}
_BUILDING: set[type[BaseModel]] = set()


def _is_model(annotation: object) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def finance_tier(annotation: object) -> Tier | None:
    """The strictest finance ``Tiered`` anywhere in ``annotation``'s spelling.

    The walk core's ``annotation_tier`` makes: ``Annotated`` metadata, union arms and
    container arguments, stopping at a model, whose marks tier its own fields.
    """
    found: list[Tier] = []

    def walk(current: object) -> None:
        if _is_model(current):
            return
        if typing.get_origin(current) is Annotated:
            base, *metadata = typing.get_args(current)
            found.extend(item.tier for item in metadata if isinstance(item, Tiered))
            walk(base)
            return
        for argument in typing.get_args(current):
            walk(argument)

    walk(annotation)
    if not found:
        return None
    if Tier.RESTRICTED in found:
        return Tier.RESTRICTED
    return Tier.INTERNAL if Tier.INTERNAL in found else Tier.PUBLIC


def _holds_model(annotation: object) -> bool:
    if _is_model(annotation):
        return True
    return any(_holds_model(argument) for argument in typing.get_args(annotation))


def _rebuilt(annotation: object, where: str) -> object:
    """``annotation`` with finance's markers as core's and each model as its view.

    Keeps ``list[...]``, unions, ``Literal[...]`` and scalars; any other generic that
    holds a model refuses, since the view could not be kept in step with it. Metadata
    other than a tier is dropped: a view validates nothing and reshapes nothing.
    """
    if _is_model(annotation):
        assert isinstance(annotation, type) and issubclass(annotation, BaseModel)
        return output_view(annotation)
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        base, *metadata = typing.get_args(annotation)
        marks = [
            CoreTiered(CORE_TIERS[item.tier])
            for item in metadata
            if isinstance(item, Tiered)
        ]
        inner = _rebuilt(base, where)
        return Annotated[(inner, *marks)] if marks else inner
    if origin is Literal or (
        origin is None and not isinstance(annotation, types.UnionType)
    ):
        return annotation
    if origin is Union or isinstance(annotation, types.UnionType):
        arms = tuple(_rebuilt(arm, where) for arm in typing.get_args(annotation))
        return Union[arms]  # noqa: UP007 - built from a tuple of arms at run time
    if origin is list:
        (item,) = typing.get_args(annotation)
        return list[_rebuilt(item, where)]  # type: ignore[misc]
    if _holds_model(annotation):
        raise ValueError(f"{where} holds a model in a {origin!r}, which no view keeps")
    return annotation


def _full_annotation(annotation: object, metadata: list[Any]) -> object:
    return Annotated[(annotation, *metadata)] if metadata else annotation


def output_view(model: type[BaseModel]) -> type[BaseModel]:
    """The registered view of finance output ``model`` (module docstring), cached."""
    cached = _VIEWS.get(model)
    if cached is not None:
        return cached
    if model in _BUILDING:
        raise ValueError(f"{model.__qualname__} is recursive, which no view keeps")
    _BUILDING.add(model)
    try:
        if not model.__pydantic_complete__:
            model.model_rebuild(raise_errors=True)
        fields: dict[str, Any] = {}
        for name, info in model.model_fields.items():
            where = f"{model.__qualname__}.{name}"
            tier = finance_tier(_full_annotation(info.annotation, info.metadata))
            if tier is None:
                raise ValueError(f"{where} has no tier")
            if tier is Tier.RESTRICTED:
                continue
            # The field's own marks were lifted into ``metadata``; the field's tier
            # replaces them, and marks inside the spelling are rebuilt as core's.
            annotation = Annotated[
                (_rebuilt(info.annotation, where), CoreTiered(CORE_TIERS[tier]))
            ]
            if info.default is not PydanticUndefined:
                fields[name] = (annotation, info.default)
            elif info.default_factory is not None:
                raise ValueError(f"{where} has a default factory, which no view keeps")
            else:
                fields[name] = (annotation, ...)
        module = model.__module__.rsplit(".", 1)[-1]
        view: type[BaseModel] = create_model(
            f"{module}_{model.__name__}",
            __config__=VIEW_CONFIG,
            __module__=__name__,
            **fields,
        )
    finally:
        _BUILDING.discard(model)
    _VIEWS[model] = view
    return view


def read_handler(spec: OperationSpec) -> Handler:
    """Core's handler for ``spec``, a READ spec on the reader connection."""
    view = output_view(spec.output_model)

    def handle(
        ctx: WorkspaceContext, uow: CoreUnitOfWork, data: BaseModel
    ) -> BaseModel:
        code: str | None = None
        token: db.LayoutToken | None = None
        try:
            token = db.use_layout(db.check_layout(layout_for(ctx)))
            finance_ctx = handler_context(FinanceRole.OWNER)
            connection = uow.connection
            with db.acting_as(connection, db.READER_ROLE):
                out = spec.handler(finance_ctx, UnitOfWork(connection), data)
        except PASS_THROUGH:
            raise
        except Exception as exc:
            code = matching_code(exc)
            if code is None:
                raise
        finally:
            if token is not None:
                db.release_layout(token)
        if code is not None:
            # Outside the except block, so nothing is chained.
            raise OperationRefused(code, code)
        if not isinstance(out, spec.output_model):
            raise OperationRefused(OUTPUT_INVALID, OUTPUT_INVALID)
        return view.model_validate(out)

    return handle


def _registered(spec: OperationSpec) -> bool:
    return (
        spec.safety_class is FinanceSafetyClass.READ
        and spec.connection == "reader"
        and not spec.owner_only
    )


READ_SPECS: Final[tuple[OperationSpec, ...]] = tuple(
    sorted(
        (spec for spec in catalogue.load_all().values() if _registered(spec)),
        key=lambda spec: spec.name,
    )
)

OWNER: Final = frozenset({Role.OWNER})

OPERATIONS: Final[tuple[tuple[OperationDeclaration, Handler], ...]] = tuple(
    (
        OperationDeclaration(
            name=spec.name,
            safety_class=SafetyClass.READ,
            roles=OWNER,
            input_model=spec.input_model,
            output=output_view(spec.output_model),
            idempotency=Idempotency.NONE,
            audit=None,
        ),
        read_handler(spec),
    )
    for spec in READ_SPECS
)

TOOLS: Final[tuple[ToolDeclaration, ...]] = tuple(
    ToolDeclaration(
        name=spec.tool_name,
        safety_class=SafetyClass.READ,
        operation=spec.name,
        input_model=spec.input_model,
        description=spec.description,
    )
    for spec in READ_SPECS
    if spec.tool_name is not None
)
