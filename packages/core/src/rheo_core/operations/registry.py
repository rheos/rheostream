"""The service registry's authorization half: registration and ``authorize``.

``OperationRegistry.register(decl, handler, *, origin)`` takes the handler as a
separate argument because ``rheo_contracts`` may not import ``UnitOfWork`` (the
contracts import scan, B14); here it is importable and mypy strict checks the
``(ctx, uow, input) -> output`` shape. Registration validates:

- the name is ``<module_id>.<noun>.<verb>`` and its prefix is ``core`` for the core
  origin or the registering origin's module id otherwise (a module cannot register a
  ``core.*`` operation, and the core cannot register a module's); ``harness`` is
  reserved for the ``test_harness`` origin, which is accepted only when the resolved
  ``profile`` is ``test`` (:func:`check_origin`, shared with the resolver registry);
- ``safety_class`` is present (the declaration model makes it required; a
  ``model_construct``-ed declaration without one is refused naming the operation —
  the *startup-fails-naming-it* wiring and the production-profile assertion of
  criterion 18 are 0c3's);
- a declaration whose ``safety_class`` is **not** ``READ`` carries an ``AuditSpec``;
  one that does not is refused naming the operation (criterion 14's declaration
  layer). This is the first of three places a missing audit path is fatal — the other
  two are ``operations/audit_paths.py`` at startup and ``dispatch.py`` at the call —
  and it is the only one that can refuse before a process is even running;
- the input model declares **none** of ``RESERVED_INPUT_FIELDS`` (C1's single list in
  ``rheo_contracts.manifest``; not restated here), by field name or by alias, and
  does not set ``extra = "allow"`` (which would carry a reserved key into
  ``model_extra`` for a handler to read) — criterion 6's model-argument channel.

``authorize(ctx, name)`` checks, in this order: ``operation_unknown``;
``operation_not_permitted`` when the context's operation set is not
``ALL_OPERATIONS`` and lacks the name; ``module_disabled`` when the owning module is
not in ``ctx.enabled_modules`` (``core`` is always enabled); ``role_not_permitted``
when ``ctx.role`` is not in the declaration's roles.

The registry writes no audit row and mints no operation record; see ``dispatch.py``.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from pydantic import AliasChoices, BaseModel
from rheo_contracts import (
    ALL_OPERATIONS,
    RESERVED_INPUT_FIELDS,
    OperationDeclaration,
    SafetyClass,
    WorkspaceContext,
    is_reserved_module,
)

from rheo_core.boundary.context import CONTEXT_REQUIRED, Refusal
from rheo_core.operations.refusals import (
    MODULE_DISABLED,
    OPERATION_NOT_PERMITTED,
    OPERATION_UNKNOWN,
    ROLE_NOT_PERMITTED,
    RegistrationRefused,
)
from rheo_core.settings import CORE_ORIGIN, TEST_HARNESS_ORIGIN, current_profile
from rheo_core.storage.backend import UnitOfWork

Handler = Callable[[WorkspaceContext, UnitOfWork, Any], BaseModel]
"""``(ctx, uow, input) -> output``. The input is typed ``Any`` at the storage slot
because the declaration carries the input model as a runtime ``type[BaseModel]``;
each handler's own annotation is what mypy checks at the registration call."""

CORE_MODULE_ID: Final = "core"
HARNESS_MODULE_ID: Final = "harness"

_ORIGIN_MODULE_IDS: Final[dict[str, str]] = {
    CORE_ORIGIN: CORE_MODULE_ID,
    TEST_HARNESS_ORIGIN: HARNESS_MODULE_ID,
}
_SEGMENT: Final = r"[a-z][a-z0-9_]*"
_OPERATION_NAME: Final = re.compile(rf"{_SEGMENT}\.{_SEGMENT}\.{_SEGMENT}")


def module_id_for_origin(origin: str) -> str:
    """The module id an origin may register under: ``core`` for the core origin,
    ``harness`` for the test harness, otherwise the origin is itself a module id."""
    if not isinstance(origin, str) or not origin:
        raise ValueError("a registration needs a non-empty origin")
    return _ORIGIN_MODULE_IDS.get(origin, origin)


def check_origin_profile(origin: str) -> None:
    """Refuse the test-harness origin outside ``profile = test``."""
    if origin == TEST_HARNESS_ORIGIN:
        profile = current_profile()
        if profile != "test":
            raise RegistrationRefused(
                origin,
                f"origin {origin!r} is accepted only under profile = test (resolved "
                f"profile is {profile!r})",
            )


def check_origin(origin: str, module_id: str, *, name: str) -> None:
    """The one origin rule both registries (operations and record resolvers) apply.

    The module id must be the registering origin's; ``harness`` is reserved for the
    ``test_harness`` origin and ``core`` for the core origin, whatever module id an
    origin string claims for itself (so ``origin = "harness"`` cannot slip past the
    profile gate by naming the module directly); and the ``test_harness`` origin is
    accepted only under ``profile = test``. Criterion 18's production-profile
    assertion (0c3) leans on this gate.
    """
    expected = module_id_for_origin(origin)
    if module_id != expected:
        raise RegistrationRefused(
            name,
            f"prefix {module_id!r} is not the registering origin's module id "
            f"{expected!r}",
        )
    if module_id == HARNESS_MODULE_ID and origin != TEST_HARNESS_ORIGIN:
        raise RegistrationRefused(
            name,
            f"module id {HARNESS_MODULE_ID!r} is reserved for origin "
            f"{TEST_HARNESS_ORIGIN!r}",
        )
    if is_reserved_module(module_id) and origin != CORE_ORIGIN:
        raise RegistrationRefused(
            name, f"module id {module_id!r} is reserved for origin {CORE_ORIGIN!r}"
        )
    check_origin_profile(origin)


def _alias_names(model: type[BaseModel]) -> set[str]:
    """Every name a payload key could bind to a field of ``model``."""
    names: set[str] = set()
    for field_name, info in model.model_fields.items():
        names.add(field_name)
        if isinstance(info.alias, str):
            names.add(info.alias)
        validation_alias = info.validation_alias
        if isinstance(validation_alias, str):
            names.add(validation_alias)
        elif isinstance(validation_alias, AliasChoices):
            names.update(c for c in validation_alias.choices if isinstance(c, str))
    return names


def reserved_input_fields(model: type[BaseModel]) -> frozenset[str]:
    """The reserved names ``model`` would accept, by field name or alias."""
    return frozenset(_alias_names(model) & RESERVED_INPUT_FIELDS)


@dataclass(frozen=True, slots=True)
class RegisteredOperation:
    """A declaration bound to its handler, its origin, and its owning module id."""

    declaration: OperationDeclaration
    handler: Handler
    origin: str
    module_id: str

    @property
    def name(self) -> str:
        return self.declaration.name


@dataclass(frozen=True, slots=True)
class Authorized:
    """``authorize``'s success value: the operation the context may run."""

    operation: RegisteredOperation


class OperationRegistry:
    """The process-wide operation table. One instance is exported as ``REGISTRY``."""

    def __init__(self) -> None:
        self._operations: dict[str, RegisteredOperation] = {}

    def register(
        self, decl: OperationDeclaration, handler: Handler, *, origin: str
    ) -> RegisteredOperation:
        """Validate and record ``decl`` with ``handler``; see the module docstring.

        Registering the identical declaration, handler and origin again is a no-op
        (startup and the CLI both build the registry); anything else under a
        registered name is refused.
        """
        if not isinstance(decl, OperationDeclaration):
            raise TypeError("register() takes an OperationDeclaration")
        name = decl.name
        if not isinstance(name, str) or not _OPERATION_NAME.fullmatch(name):
            raise RegistrationRefused(
                str(name), "operation names are <module_id>.<noun>.<verb>"
            )
        module_id = name.split(".", 1)[0]
        check_origin(origin, module_id, name=name)
        if not isinstance(getattr(decl, "safety_class", None), SafetyClass):
            raise RegistrationRefused(name, "declares no safety class")
        # Criterion 14's declaration layer (AC 21), ratified verbatim at
        # ``docs/architecture/module-contract.md:115``. ``SafetyClass`` is an
        # unordered ``StrEnum``, so "above ``READ``" is spelled ``is not
        # SafetyClass.READ`` — there is no ordering to compare against and no new
        # contract surface is needed to express one.
        if decl.safety_class is not SafetyClass.READ and decl.audit is None:
            raise RegistrationRefused(
                name,
                f"declares no audit spec; safety class {decl.safety_class.value!r} is "
                "above read, and an operation above read cannot be registered without "
                "one",
            )
        input_model = decl.input_model
        if not (isinstance(input_model, type) and issubclass(input_model, BaseModel)):
            raise RegistrationRefused(name, "input_model is not a pydantic model")
        reserved = reserved_input_fields(input_model)
        if reserved:
            raise RegistrationRefused(
                name,
                f"input model declares reserved field(s) {sorted(reserved)}; "
                "workspace, actor and storage identity come from the context only",
            )
        if input_model.model_config.get("extra") == "allow":
            raise RegistrationRefused(
                name,
                "input model sets extra = 'allow', which would carry a reserved "
                "payload key into model_extra",
            )
        if not callable(handler):
            raise TypeError("the handler must be callable")
        existing = self._operations.get(name)
        if existing is not None:
            if (
                existing.declaration == decl
                and existing.handler is handler
                and existing.origin == origin
            ):
                return existing
            raise RegistrationRefused(
                name, f"already registered (origin {existing.origin!r})"
            )
        registered = RegisteredOperation(decl, handler, origin, module_id)
        self._operations[name] = registered
        return registered

    def lookup(self, name: str) -> RegisteredOperation | None:
        return self._operations.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._operations)

    def __contains__(self, name: object) -> bool:
        return name in self._operations

    def authorize(self, ctx: object, name: str) -> Authorized | Refusal:
        """Whether ``ctx`` may run ``name``, in the order the module docstring gives."""
        if not isinstance(ctx, WorkspaceContext):
            return Refusal(CONTEXT_REQUIRED, "authorize() needs a WorkspaceContext")
        operation = self._operations.get(name)
        if operation is None:
            return Refusal(OPERATION_UNKNOWN, f"{name!r} is not a registered operation")
        permitted = ctx.operation_set
        if permitted is not ALL_OPERATIONS and (
            not isinstance(permitted, frozenset) or name not in permitted
        ):
            return Refusal(
                OPERATION_NOT_PERMITTED,
                f"{name} is not in the context's operation set",
            )
        if (
            operation.module_id != CORE_MODULE_ID
            and operation.module_id not in ctx.enabled_modules
        ):
            return Refusal(
                MODULE_DISABLED,
                f"module {operation.module_id!r} is not enabled in this workspace",
            )
        if ctx.role not in operation.declaration.roles:
            return Refusal(
                ROLE_NOT_PERMITTED,
                f"{name} is not permitted to role {ctx.role.value!r}",
            )
        return Authorized(operation)


REGISTRY: Final = OperationRegistry()


def register(
    decl: OperationDeclaration, handler: Handler, *, origin: str
) -> RegisteredOperation:
    """Register on the process-wide registry; see ``OperationRegistry.register``."""
    return REGISTRY.register(decl, handler, origin=origin)


def authorize(ctx: object, name: str) -> Authorized | Refusal:
    """Authorize against the process-wide registry."""
    return REGISTRY.authorize(ctx, name)
