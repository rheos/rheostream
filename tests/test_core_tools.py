"""The release-one core MCP tools (issue #127): what each names and how it is shaped.

``rheo_core.tokens.sets`` declares its tools' operation names and input models as
local copies, to stay out of the ``rheo_core.operations`` import cycle its module
docstring describes. A copy is only safe while something holds it to its original,
and this file is that something: each tool's operation is registered, its safety
class equals the operation's (``module-contract.md``: "must equal the operation's"),
none names a non-token-issuable operation, and its published input schema is the
operation's own, or, for ``audit_list``, the operation's narrowed to ``limit``.

The roles come from the operation declarations and are compared with the tool
table in ``docs/architecture/runtime-and-mcp.md``; the listing filter that applies
them is exercised end to end in ``tests/postgres/test_mcp_mount.py``.
"""

from typing import Any

import pytest
from rheo_contracts import Role, SafetyClass
from rheo_core.audit.operations import AuditListInput
from rheo_core.operations import (
    AUDIT_LIST,
    OPERATION_GET,
    OPERATION_LIST,
    REGISTRY,
    register_core_operations,
)
from rheo_core.operations.core_ops import WORKSPACE_STATUS
from rheo_core.tokens.policy import NON_TOKEN_ISSUABLE
from rheo_core.tokens.sets import (
    AUDIT_LIST_TOOL,
    CORE_TOOLS,
    OPERATIONS_GET_TOOL,
    OPERATIONS_LIST_TOOL,
    TOOL_REGISTRY,
    WORKSPACE_STATUS_TOOL,
    agent_default,
    register_core_tools,
)

PRODUCTION_CORE_TOOLS = {
    "workspace_status": (WORKSPACE_STATUS, {Role.OWNER, Role.MEMBER, Role.OPERATOR}),
    "operations_get": (OPERATION_GET, {Role.OWNER, Role.MEMBER, Role.OPERATOR}),
    "operations_list": (OPERATION_LIST, {Role.OWNER, Role.MEMBER, Role.OPERATOR}),
    "audit_list": (AUDIT_LIST, {Role.OWNER, Role.OPERATOR}),
}
"""Tool -> (operation, roles), from ``runtime-and-mcp.md`` § Release-one tool set.
Typed out on purpose: this is the document's table, and the assertions below are
that the code agrees with it."""


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_core_tools()


def _schema(model: Any) -> dict[str, Any]:
    """A model's JSON schema minus its ``title`` and ``description``: the class
    name and the docstring, which differ between an original and its local mirror
    by construction. What is left is what a client may send."""
    schema = dict(model.model_json_schema())
    schema.pop("title", None)
    schema.pop("description", None)
    return schema


def test_the_four_production_core_tools_are_registered_under_their_names() -> None:
    declared = {tool.name for tool in CORE_TOOLS}
    assert set(PRODUCTION_CORE_TOOLS) <= declared
    assert set(PRODUCTION_CORE_TOOLS) <= TOOL_REGISTRY.names()
    for name, (operation, _) in PRODUCTION_CORE_TOOLS.items():
        registered = TOOL_REGISTRY.lookup(name)
        assert registered is not None, name
        assert registered.declaration.operation == operation, name


@pytest.mark.parametrize("name", sorted(PRODUCTION_CORE_TOOLS))
def test_each_tool_matches_its_operations_class_and_roles(name: str) -> None:
    operation, roles = PRODUCTION_CORE_TOOLS[name]
    tool = TOOL_REGISTRY.lookup(name)
    registered = REGISTRY.lookup(operation)
    assert tool is not None and registered is not None, name
    assert tool.declaration.safety_class is registered.declaration.safety_class
    assert tool.declaration.safety_class is SafetyClass.READ
    assert set(registered.declaration.roles) == roles, name
    assert operation not in NON_TOKEN_ISSUABLE, name


@pytest.mark.parametrize(
    "tool",
    [WORKSPACE_STATUS_TOOL, OPERATIONS_GET_TOOL, OPERATIONS_LIST_TOOL],
    ids=lambda tool: tool.name,
)
def test_mirrored_input_models_publish_the_operations_own_schema(tool: Any) -> None:
    """Field for field and bound for bound: a mirror that dropped ``limit``'s
    ``le=500`` would let a model ask for more than the operation allows, and the
    failure would surface as the operation's refusal rather than the tool's."""
    registered = REGISTRY.lookup(tool.operation)
    assert registered is not None
    assert _schema(tool.input_model) == _schema(registered.declaration.input_model)


def test_audit_list_publishes_limit_only_of_the_operations_schema() -> None:
    """``audit_list`` is the operation's schema narrowed to ``limit``: the bound is
    the operation's, and the two owner/operator-only opt-ins are not offered."""
    tool_schema = _schema(AUDIT_LIST_TOOL.input_model)
    operation_schema = _schema(AuditListInput)
    assert set(tool_schema["properties"]) == {"limit"}
    assert tool_schema["properties"]["limit"] == operation_schema["properties"]["limit"]
    assert {"include_tool_telemetry", "include_deletions"} <= set(
        operation_schema["properties"]
    )


def test_agent_default_carries_the_three_new_operations() -> None:
    """``agent_default`` is every operation a registered tool names, so registering
    the tools is what puts the operations in a new token's snapshot."""
    assert {WORKSPACE_STATUS, OPERATION_GET, OPERATION_LIST, AUDIT_LIST} <= (
        agent_default()
    )
