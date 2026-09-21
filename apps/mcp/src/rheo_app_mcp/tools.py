"""``list_tools``/``call_tool``: the façade's tool surface over the core facade.

Tools come from ``rheo_core.tokens.sets.TOOL_REGISTRY`` -- the one declaration
site, read from both directions (that module's own ``agent_default`` policy,
and this module's tool listing) -- so this file does not redeclare
``workspace_status``/``harness_get_note`` a second time.

**What used to be here and is not any more.** This file carried its own
``_visible``: a listing filter that checked the operation set and derived a
module id from the *operation name*. Both halves were one check short of what a
tool call needs. The module derived from an operation name is the module that
owns the operation, not the module that registered the tool -- so a tool
registered by a module and naming a ``core`` operation stayed listed in a
workspace where its own module was disabled -- and ``call_tool`` passed the raw
argument mapping into ``dispatch``, which validates the operation's wider input
model and knows nothing of the narrower schema this surface advertised.

Both checks now live in ``rheo_core.operations.tool_facade``, called from here
rather than reimplemented here, so the MCP surface and any other caller of the
same facade cannot answer the two questions differently. The pair below is what
is left: the names ``transport.py`` imports, and the ``consumers`` argument the
composition root threads through them.

This is still the seam criterion 7 needs: ``dispatch`` reached from a tool call,
refusing a cross-workspace reference exactly as the ``api`` surface does.
``transport.py`` wraps this pair in the streamable-HTTP wire protocol; nothing
of the protocol leaks into this file, and this pair stays directly callable
from a test with no server at all.
"""

from collections.abc import Mapping

from rheo_contracts import ToolDeclaration, WorkspaceContext
from rheo_core.operations import REGISTRY, OperationOutcome, OperationRegistry
from rheo_core.operations.tool_facade import (
    ConsumerRegistry,
    call_registered_tool,
    visible_tools,
)
from rheo_core.tokens.sets import TOOL_REGISTRY, ToolRegistry


def list_tools(
    ctx: WorkspaceContext,
    *,
    tools: ToolRegistry = TOOL_REGISTRY,
    registry: OperationRegistry = REGISTRY,
) -> tuple[ToolDeclaration, ...]:
    """The registered tools this context may call right now.

    The two registry arguments default to the process-wide tables every deployment
    reads and exist for the same reason ``dispatch``'s own ``registry=`` does: a
    test that loaded a module into local registries drives this surface against
    what it loaded, instead of a second listing path being written for it.
    """
    return visible_tools(ctx, tools=tools, registry=registry)


def call_tool(
    ctx: WorkspaceContext,
    name: str,
    arguments: Mapping[str, object],
    *,
    consumers: ConsumerRegistry | None,
    tools: ToolRegistry = TOOL_REGISTRY,
    registry: OperationRegistry = REGISTRY,
) -> OperationOutcome:
    """Dispatch ``name`` (a tool name, not an operation name) for ``ctx``.

    A name outside :func:`list_tools`'s result for this context -- whether it
    was never registered at all, is registered by a module this workspace has
    disabled, delegates to an operation that is not registered, is outside this
    token's scope, or is not permitted to this role -- is ``not_found`` in every
    case: the facade never distinguishes "no such tool" from "not available to
    you", the same non-disclosure principle ``rheo_core.tokens.presentation``
    applies to a malformed token versus one that merely does not exist.

    ``consumers`` is keyword-only with **no default**, so every call site states
    which :class:`~rheo_core.events.consumers.ConsumerRegistry` a publishing
    handler will reach. This package cannot import ``apps/core``'s one -- that
    distribution depends on this one, and an import back would be a cycle and an
    undeclared dependency -- so the composition root passes it down through
    ``transport.build_mcp_app``.
    """
    return call_registered_tool(
        ctx, name, arguments, consumers=consumers, tools=tools, registry=registry
    )
