"""The tool-origin facade: what a context may discover, and what a call may do.

``apps/mcp/src/rheo_app_mcp/tools.py`` used to answer both questions itself, and
answered each of them one check short.

**Discovery was gated on the operation's module, never on the tool's.** The old
``_visible`` derived a module id from the *operation name*
(``operation.split(".", 1)[0]``), so a tool registered by a module but naming a
``core`` operation stayed listed in a workspace where its own module was disabled
— ``recallatron_forget`` names the core deletion operation on purpose
(``module-contract.md``: a module's tool may name a core operation when its input
restricts the reference to the module's own record types), so the one tool whose
origin most needed checking was the one the check could not see.
:class:`~rheo_core.tokens.sets.RegisteredTool` has carried ``origin`` since 0c3,
and that class's own docstring names this as the residual gap; this module closes
it by reading the origin the registry already stored rather than by building a
second ownership table beside it.

**And a call was dispatched from the raw argument mapping.** ``dispatch`` validates
against the *operation's* input model, which is the wider of the two: a tool that
advertises a narrow schema to a client restricted nothing about a crafted call.
:func:`call_registered_tool` validates against the tool's own declared
``input_model`` first, refusing every argument name that model does not declare,
and dispatches the validated dump — so ``recallatron_forget`` handed a reference
to somebody else's record answers ``input_invalid`` before any approval work is
created, rather than reaching the deletion coordinator to be refused there.

**Four checks, and both entry points make all four.** In order: the tool's
registered origin is ``core`` or a module enabled in this workspace; the operation
it delegates to is registered *now*; that operation is in this context's operation
set; and this context's role is one the operation permits. A cached listing is not
authority — every one of them is re-read on the call, against the same registries,
so a token whose scope narrowed or a role that changed between a listing and a call
is refused at the second even though the first said yes.

**One state for every one of them, and it is ``not_found``.** The facade never
distinguishes "no such tool" from "not available to you" — the non-disclosure
principle ``rheo_core.tokens.presentation`` applies to a malformed token versus one
that merely does not exist. Which of the four checks failed is exactly the
deployment metadata a caller is not entitled to enumerate.

**And it is where tool telemetry is emitted** (§ A11), because it is the one place
that sees a call's whole shape: the declaration it resolved, the input it validated,
the outcome ``dispatch`` returned, and the monotonic interval between. The scalars
this module derives — and only scalars — go to
``rheo_core.audit.tool_telemetry.record_tool_call``, which writes them in its own
short transaction and swallows every failure of its own, so the outcome returned from
here is the operation's own however the sink fares. ``apps/mcp`` therefore reaches
telemetry through this façade and still imports no storage package.
"""

import time
from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import BaseModel, ValidationError
from rheo_contracts import (
    ALL_OPERATIONS,
    SafetyClass,
    ToolDeclaration,
    WorkspaceContext,
)

from rheo_core.audit.tool_telemetry import (
    TELEMETRY_ERROR,
    TELEMETRY_MODE_CURRENT,
    TELEMETRY_MODE_HISTORY,
    TELEMETRY_MODE_NONE,
    TELEMETRY_REFUSED,
    TELEMETRY_SUCCESS,
    TelemetryMode,
    TelemetryOutcome,
    record_tool_call,
)

# Re-exported (``as`` rather than a bare import) so a caller outside ``rheo_core``
# can spell this module's one keyword-only argument. ``apps/mcp``'s import allowlist
# (``tests/test_mcp_boundary.py``) carries ``rheo_core.boundary``,
# ``rheo_core.operations`` and ``rheo_core.tokens`` and not ``rheo_core.events``, and
# the scan is a static AST walk that a ``TYPE_CHECKING`` guard does not hide from.
# That allowlist exists to keep storage and database drivers out of the façade; a
# registry of in-process event callbacks is neither, and the alternative — annotating
# the façade's own pass-through as ``object`` — would disable type checking on the one
# argument whose whole purpose is that exactly one instance travels. Named here so the
# widening is a decision on the record rather than an import nobody reviewed.
from rheo_core.events.consumers import ConsumerRegistry as ConsumerRegistry
from rheo_core.operations.dispatch import (
    OperationError,
    OperationOutcome,
    _describe_validation_error,
    dispatch,
)
from rheo_core.operations.records import PENDING
from rheo_core.operations.refusals import FAILED, INPUT_INVALID, SUCCEEDED
from rheo_core.operations.registry import (
    CORE_MODULE_ID,
    REGISTRY,
    OperationRegistry,
    RegisteredOperation,
    _alias_names,
    module_id_for_origin,
)
from rheo_core.tokens.sets import TOOL_REGISTRY, RegisteredTool, ToolRegistry

__all__ = [
    "ConsumerRegistry",
    "TOOL_NOT_FOUND",
    "call_registered_tool",
    "visible_tools",
]

TOOL_NOT_FOUND: Final = "not_found"
"""The single answer to every reason a tool is not callable here.

The same string ``rheo_core.refs.resolver.NOT_FOUND`` holds, and deliberately not an
import of it: that constant is the record resolver's answer about a *record*, and a
tool that is not available is not a missing record. Two names for one word, held
together by nothing, is the honest description — and cheaper than a shared import
that would make the façade's disclosure rule depend on the resolver's.
"""

_SUCCESS_STATES: Final = frozenset({SUCCEEDED, PENDING})
_ERROR_STATES: Final = frozenset({FAILED})
"""The two dispatch states that are not a refusal, split for
:func:`_telemetry_outcome`. Everything else — the authorization states,
``input_invalid``, ``approval_required``, a domain refusal a handler raised — is
``refused``, which is why the third set is written as a fallthrough rather than as a
literal that a new refusal state would silently fall out of."""


def _delegate(
    declaration: ToolDeclaration, registry: OperationRegistry
) -> RegisteredOperation | None:
    """The operation this tool names, **as the registry holds it right now**.

    ``None`` when nothing is registered under that name: a declared tool whose
    operation never registered, or whose module was unloaded, delegates to nothing
    and is not callable. This is the check ``tokens/sets.py:agent_default`` makes
    against the same registry when it intersects declared tool operations with live
    operation names, made here for the same reason — a declaration is not a
    registration.
    """
    return registry.lookup(declaration.operation)


def _enabled(ctx: WorkspaceContext, module_id: str) -> bool:
    """``core`` is always enabled; everything else has to be enabled here."""
    return module_id == CORE_MODULE_ID or module_id in ctx.enabled_modules


def _available(
    ctx: WorkspaceContext, tool: RegisteredTool, registry: OperationRegistry
) -> bool:
    """The four checks, in the order the module docstring gives.

    ``module_id_for_origin`` is the operation registry's own mapping from a
    registration origin to the module id it may register under, not a second copy of
    it: the core origin is ``core``, the test-harness origin is ``harness``, and any
    other origin is itself a module id. A tool's origin is read through it so that a
    tool and an operation registered by the same origin are judged enabled or
    disabled together.

    The role check is here rather than only at ``dispatch`` because the ratified
    contract puts it in both places ("discovery and each call must check tool origin,
    delegated operation registration/enabled state, role and current operation set").
    Listing a tool the caller's role cannot use would advertise a capability the very
    next call refuses.
    """
    if not _enabled(ctx, module_id_for_origin(tool.origin)):
        return False
    operation = _delegate(tool.declaration, registry)
    if operation is None:
        return False
    permitted = ctx.operation_set
    if permitted is not ALL_OPERATIONS and (
        not isinstance(permitted, frozenset) or operation.name not in permitted
    ):
        return False
    if not _enabled(ctx, operation.module_id):
        return False
    return ctx.role in operation.declaration.roles


def visible_tools(
    ctx: WorkspaceContext,
    *,
    tools: ToolRegistry = TOOL_REGISTRY,
    registry: OperationRegistry = REGISTRY,
) -> tuple[ToolDeclaration, ...]:
    """The registered tools this context may call **at this moment**.

    Read from the live registries on every call rather than cached anywhere: a tool
    whose module was disabled, whose operation was unregistered, or whose operation
    left this context's scope is absent from the next listing without anything having
    to invalidate a previous one.
    """
    return tuple(
        tool.declaration
        for tool in _registered(tools)
        if _available(ctx, tool, registry)
    )


def _registered(tools: ToolRegistry) -> tuple[RegisteredTool, ...]:
    """Every registered tool, with its origin, in registration order.

    ``declarations()`` drops the origin, which is the whole of what this module
    needs, so the registry is walked by name instead.
    """
    return tuple(
        registered
        for name in sorted(tools.names())
        if (registered := tools.lookup(name)) is not None
    )


def _refused(state: str, detail: str) -> OperationOutcome:
    return OperationOutcome(state, error=OperationError(state, detail))


def _validated(
    declaration: ToolDeclaration, arguments: Mapping[str, object]
) -> BaseModel | OperationOutcome:
    """``arguments`` as the tool's own input model, or the refusal that stops it.

    **Unknown argument names are refused here rather than left to the model's own
    ``extra`` setting**, and that is the point of the check. ``ToolRegistry.register``
    refuses ``extra = "allow"`` but accepts ``extra = "ignore"``, which silently
    drops an argument a client believed it had sent; and the operation's model — the
    only one ``dispatch`` sees — may well declare a field this tool deliberately does
    not. Forbidding what the *tool* does not declare is what makes the advertised
    schema a restriction rather than only a description.

    Names are collected through the operation registry's own ``_alias_names``, the
    same function ``reserved_input_fields`` is built on, so "what a payload key could
    bind to" has one definition in this tree rather than two that agree today.
    """
    model = declaration.input_model
    unknown = sorted(set(arguments) - _alias_names(model))
    if unknown:
        return _refused(
            INPUT_INVALID,
            f"{declaration.name} does not accept argument(s) {unknown}",
        )
    try:
        return model.model_validate(dict(arguments))
    except ValidationError as exc:
        return _refused(INPUT_INVALID, _describe_validation_error(exc))


_LIFECYCLE_FIELD: Final = "include_invalidated"
"""The declared field § A6's lifecycle mode is selected by.

Named here, in the façade, because the *mode* is what § A11 asks a telemetry row to
carry and this is the only layer that sees both the declaration and the validated
input. It is the ratified public vocabulary (AC 5 names ``include_invalidated``
directly), not one module's private spelling, and a tool that does not declare it
simply has no mode — no core code branches on which module declared it.
"""

_QUERY_FIELD: Final = "query"
"""The declared field a ``READ`` tool's query length is measured from.

Measured, never read: only ``len()`` of it ever leaves this module, and only for a
``READ``. § A11: "query length is optional for READ; query text is **not stored**."
"""


def _telemetry_mode(validated: BaseModel | None) -> TelemetryMode:
    """``history``/``current`` for a tool that declares the lifecycle selector.

    :data:`TELEMETRY_MODE_NONE` for every other tool, and for a call refused before
    validation — there is no validated input to read a mode from, and reading one from
    the raw arguments would put a caller-supplied value into the row.
    """
    if validated is None:
        return TELEMETRY_MODE_NONE
    selector = getattr(validated, _LIFECYCLE_FIELD, None)
    if not isinstance(selector, bool):
        return TELEMETRY_MODE_NONE
    return TELEMETRY_MODE_HISTORY if selector else TELEMETRY_MODE_CURRENT


def _telemetry_query_length(
    declaration: ToolDeclaration, validated: BaseModel | None
) -> int | None:
    """The length of a ``READ`` tool's query, or ``None``.

    ``None`` for every non-``READ`` class, for a ``READ`` tool that declares no query,
    and for a call refused before validation.
    """
    if validated is None or declaration.safety_class is not SafetyClass.READ:
        return None
    query = getattr(validated, _QUERY_FIELD, None)
    return len(query) if isinstance(query, str) else None


def _telemetry_argument_names(
    declaration: ToolDeclaration, arguments: Mapping[str, object]
) -> tuple[str, ...]:
    """The sorted **declared** argument names a write call supplied.

    Empty for a ``READ``: § A11 gives argument names to writes only, and a read's
    query length already says what there is to say about its input.

    Intersected with the declaration's own names rather than taken from the mapping,
    and that is a privacy boundary rather than tidiness: a refusal is emitted too, and
    on the ``input_invalid`` path the mapping may still hold a key the model never
    declared — a caller-chosen string, which is content this table may not carry.
    Values are never read at all, at any class.
    """
    if declaration.safety_class is SafetyClass.READ:
        return ()
    return tuple(sorted(set(arguments) & _alias_names(declaration.input_model)))


def _telemetry_outcome(state: str) -> TelemetryOutcome:
    """§ A11's three-word vocabulary over ``dispatch``'s state.

    ``succeeded`` and the ``pending`` of a ``long_running`` dispatch are both a call
    the system accepted and is answering, so both are ``success``; ``failed`` (a
    handler that raised, a transaction that would not commit) is ``error``; and every
    refusal — an authorization state, ``input_invalid``, a domain refusal a handler
    raised, ``approval_required`` — is ``refused``. ``approval_required`` belongs in
    that third bucket rather than the first: nothing was done yet, which is exactly
    what makes ``recallatron_forget``'s ``result_count`` zero on it.
    """
    if state in _SUCCESS_STATES:
        return TELEMETRY_SUCCESS
    if state in _ERROR_STATES:
        return TELEMETRY_ERROR
    return TELEMETRY_REFUSED


def _telemetry_result_count(outcome: OperationOutcome) -> int:
    """Returned items, never rows touched.

    A result carrying an ``items`` collection contributes its length; any other
    result is one; no result at all is zero. § A11's "forget count means one
    acknowledgment or zero, not erased row count" falls straight out of that — the
    deletion output is a single ``{deletion_ref}`` model, and an ``approval_required``
    outcome has no result — so nothing here has to know what a deletion removed, which
    is also what stops this row disclosing a closure size that § A8 keeps off the
    caller's own output.
    """
    result = outcome.result
    if result is None:
        return 0
    items = getattr(result, "items", None)
    if isinstance(items, Sequence) and not isinstance(items, str | bytes):
        return len(items)
    return 1


def call_registered_tool(
    ctx: WorkspaceContext,
    name: str,
    arguments: Mapping[str, object],
    *,
    consumers: ConsumerRegistry | None,
    tools: ToolRegistry = TOOL_REGISTRY,
    registry: OperationRegistry = REGISTRY,
) -> OperationOutcome:
    """Dispatch the tool called ``name`` for ``ctx``, or refuse.

    ``consumers`` is keyword-only and has **no default**: it is the composition
    root's one :class:`~rheo_core.events.consumers.ConsumerRegistry`, and a caller
    that has none says so by passing ``None`` rather than by omitting the argument.
    A publishing handler reached with ``None`` refuses ``consumers_missing``, which
    is a deployment wiring gap and is meant to be loud.

    The order is availability, then input, then dispatch. Input validation runs after
    the four availability checks so that a tool this context cannot call answers
    ``not_found`` whatever it was handed — an ``input_invalid`` for an unavailable
    tool would confirm the tool exists and describe its schema.

    **Telemetry is emitted once, after the outcome, for a tool that is available
    here — and never for one that is not.** § A11 asks for a row on every outcome
    "including input refusals", and the ``input_invalid`` refusal below gets one. The
    ``not_found`` branch above deliberately does not: ``name`` on that path is an
    unvalidated string a caller chose, so a row for it would write caller-supplied
    text into ``tool_name`` — the one column this table trusts to be a registered
    name — and would also answer, in an owner-readable table, the question the
    ``not_found`` state exists to refuse. There is no tool call to instrument when no
    tool was reached.

    The clock is :func:`time.monotonic`, so a wall-clock adjustment mid-call cannot
    produce a negative duration against the DDL's non-negative check.
    """
    registered = tools.lookup(name)
    if registered is None or not _available(ctx, registered, registry):
        return _refused(TOOL_NOT_FOUND, f"{name!r} is not a tool available here")
    declaration = registered.declaration
    started = time.monotonic()
    validated = _validated(declaration, arguments)
    if isinstance(validated, OperationOutcome):
        outcome, model = validated, None
    else:
        model = validated
        outcome = dispatch(
            ctx,
            declaration.operation,
            validated.model_dump(),
            registry=registry,
            consumers=consumers,
        )
    record_tool_call(
        ctx,
        tool_name=declaration.name,
        safety_class=declaration.safety_class,
        mode=_telemetry_mode(model),
        result_count=_telemetry_result_count(outcome),
        duration_ms=int((time.monotonic() - started) * 1000),
        outcome=_telemetry_outcome(outcome.state),
        query_length=_telemetry_query_length(declaration, model),
        argument_names=_telemetry_argument_names(declaration, arguments),
    )
    return outcome
