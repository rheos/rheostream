"""``list_tools``/``call_tool``: the facade's tool surface over the live tool
registry.

Tools come from ``rheo_core.tokens.sets.TOOL_REGISTRY`` -- the one declaration
site, read from both directions (that module's own ``agent_default`` policy,
and this module's tool listing) -- so this file does not redeclare
``workspace_status``/``harness_get_note`` a second time. The registry is read at
call time and holds only what survived ``register``, so a tool this process
never registered is absent from every listing rather than listed and then
refused.

This is the seam criterion 7 needs: ``dispatch`` reached from a tool call,
refusing a cross-workspace reference exactly as the ``api`` surface does.
``transport.py`` wraps this pair in the streamable-HTTP wire protocol; nothing
of the protocol leaks into this file, and this pair stays directly callable
from a test with no server at all.
"""

from collections.abc import Mapping
from typing import Final

from rheo_contracts import ALL_OPERATIONS, ToolDeclaration, WorkspaceContext
from rheo_core.operations import (
    CORE_MODULE_ID,
    OperationError,
    OperationOutcome,
    dispatch,
)
from rheo_core.tokens.sets import TOOL_REGISTRY

NOT_FOUND: Final = "not_found"
"""Deliberately a local literal, not an import of
``rheo_core.refs.resolver.NOT_FOUND`` -- ``rheo_core.refs`` is not on this
package's allowlist (``tests/test_mcp_boundary.py``). The two values are the
identical string by coincidence of naming, not by a shared import;
duplicating a one-word literal is cheaper than widening the boundary."""


def _visible(ctx: WorkspaceContext, operation: str) -> bool:
    """Whether ``operation`` is in ``ctx.operation_set`` and its module is
    enabled -- the two of ``authorize``'s checks that decide whether a tool is
    worth listing at all (the role check is per-call, at ``dispatch``)."""
    permitted = ctx.operation_set
    if permitted is not ALL_OPERATIONS and (
        not isinstance(permitted, frozenset) or operation not in permitted
    ):
        return False
    module_id = operation.split(".", 1)[0]
    return module_id == CORE_MODULE_ID or module_id in ctx.enabled_modules


def list_tools(ctx: WorkspaceContext) -> tuple[ToolDeclaration, ...]:
    """The registered tools this context may call right now."""
    return tuple(
        tool for tool in TOOL_REGISTRY.declarations() if _visible(ctx, tool.operation)
    )


def call_tool(
    ctx: WorkspaceContext, name: str, arguments: Mapping[str, object]
) -> OperationOutcome:
    """Dispatch ``name`` (a tool name, not an operation name) for ``ctx``.

    A name outside :func:`list_tools`'s result for this context -- whether it
    was never registered at all, or is registered but not currently visible
    to this context -- is ``not_found`` in both cases: the facade never
    distinguishes "no such tool" from "not available to you", the same
    non-disclosure principle ``rheo_core.tokens.presentation`` applies to a
    malformed token versus one that merely does not exist.
    """
    for tool in list_tools(ctx):
        if tool.name == name:
            return dispatch(ctx, tool.operation, arguments)
    detail = f"{name!r} is not a tool available here"
    return OperationOutcome(NOT_FOUND, error=OperationError(NOT_FOUND, detail))
