"""The three named package sets (B7) and the MCP facade's tool registry.

Each set is a rule evaluated against the registry **at issuance** (``issue.py``
calls these at the moment a token is minted), never an enumerated list stored
anywhere, so a set tracks the registry live:

- ``read_only``: every registered operation of ``SafetyClass.READ``.
- ``agent_default``: every operation named by a tool in :data:`TOOL_REGISTRY`,
  intersected with the operation registry's actually registered operation names --
  a declared tool naming an operation that failed to register does not silently
  leak into the set.
- ``cli_full``: every registered operation except :data:`NON_TOKEN_ISSUABLE`.

:class:`ToolRegistry` is also the MCP facade's tool table
(``apps/mcp/src/rheo_app_mcp/tools.py`` lists exactly what this registry holds --
one declaration site, read from both directions, so the façade and this module's
policy can never drift apart). :data:`CORE_TOOLS` names the two declarations
:func:`register_core_tools` feeds it: ``workspace_status`` (production) and
``harness_get_note`` (test profile only), mirroring how
``operations/core_ops.py`` pairs :data:`~rheo_core.operations.core_ops.
CORE_OPERATIONS` with ``register_core_operations()``.

**Declared is not registered, and this module now distinguishes them.** Before run
0c3 the two declarations *were* the live set: a module-level tuple that nothing
registered and therefore nothing could refuse. Now a declaration reaches the live
set only by surviving :meth:`ToolRegistry.register`, which refuses a tool carrying
no safety class and a tool whose input model would accept one of the reserved
field names. Both refusals name the tool.

The test-profile gate is still not a runtime check *here* -- ``agent_default``'s
intersection with the operation registry's live names is what excludes
``harness.note.get`` outside a test run, because that operation is only ever
actually registered by ``tests/harness/registry.py``'s own ``register_harness()``
(itself gated to ``profile = test`` by the registry's origin check). Registering
both tools unconditionally and letting operation-registry presence do the
filtering is "gating its inclusion the same way the harness gates its own
registrations": the harness's registration attempt is what is profile-gated, not
a second check duplicated here.

This module defines its own tiny input models for both tools rather than
importing ``rheo_core.operations.core_ops`` or ``tests/harness/registry.py``'s:
the latter would make a shipped package depend on test-only code that is never
installed. Duplicating two small, stable shapes is cheaper than that.

**Everything from ``rheo_core.operations`` is imported lazily, inside the function
that needs it, never at module level** -- ``REGISTRY`` in the three set
evaluators below, and ``reserved_input_fields``/``RegistrationRefused`` in
:meth:`ToolRegistry.register`. ``core_ops.py`` imports ``rheo_core.tokens.issue``
(which imports this module for :data:`PACKAGE_SETS`), and ``rheo_core.
operations``'s own ``__init__.py`` imports ``core_ops.py`` as its first
statement -- so a module-level ``from rheo_core.operations.registry import
REGISTRY`` here would close a real cycle the moment anything imports this
module (or ``tokens.issue``) before ``rheo_core.operations`` has been touched
at all: ``operations.registry`` -> (via the package ``__init__``) ->
``core_ops.py`` -> ``tokens.issue`` -> this module -> ``operations.registry``
(still mid-init). Deferring the import to call time -- well after every module
involved has finished loading, in any realistic caller -- avoids it
regardless of import order. An import at module level would not fail here and
now; it would fail for whichever consumer first happens to import this module
before ``rheo_core.operations``, which is why the rule is absolute rather than
case-by-case.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict
from rheo_contracts import SafetyClass, ToolDeclaration

from rheo_core.settings import CORE_ORIGIN
from rheo_core.tokens.policy import NON_TOKEN_ISSUABLE

_WORKSPACE_STATUS_OPERATION: Final = "core.workspace.status"
"""Duplicated from ``operations/core_ops.py``'s ``WORKSPACE_STATUS`` -- see the
module docstring's cycle note. The two are proven equal by
``test_tokens.py``'s own ``agent_default`` assertion, not by a shared import."""

_HARNESS_NOTE_GET_OPERATION: Final = "harness.note.get"
"""Duplicated from ``tests/harness/registry.py``'s ``NOTE_GET`` -- that file's own
module docstring cross-references this pairing. Same reasoning as
``_WORKSPACE_STATUS_OPERATION`` above, plus the production/test boundary:
``rheo_core`` (shipped) must not import ``tests/harness`` (never installed)."""


class _WorkspaceStatusToolInput(BaseModel):
    """Mirrors ``operations/core_ops.py``'s ``WorkspaceStatusInput``: no fields,
    the workspace comes from the context. Declared locally -- see module
    docstring's cycle note."""

    model_config = ConfigDict(extra="ignore")


class _HarnessNoteRefToolInput(BaseModel):
    """Mirrors ``tests/harness/registry.py``'s ``NoteRefInput`` (``ref: str``).
    Declared locally -- see module docstring's boundary note."""

    model_config = ConfigDict(extra="ignore")

    ref: str


WORKSPACE_STATUS_TOOL: Final[ToolDeclaration] = ToolDeclaration(
    name="workspace_status",
    safety_class=SafetyClass.READ,
    operation=_WORKSPACE_STATUS_OPERATION,
    input_model=_WorkspaceStatusToolInput,
)

HARNESS_GET_NOTE_TOOL: Final[ToolDeclaration] = ToolDeclaration(
    name="harness_get_note",
    safety_class=SafetyClass.READ,
    operation=_HARNESS_NOTE_GET_OPERATION,
    input_model=_HarnessNoteRefToolInput,
)

CORE_TOOLS: Final[tuple[ToolDeclaration, ...]] = (
    WORKSPACE_STATUS_TOOL,
    HARNESS_GET_NOTE_TOOL,
)
"""The declarations :func:`register_core_tools` offers the registry.

Declared, not live: what the façade lists and what ``agent_default`` reads is
:data:`TOOL_REGISTRY`, which holds only what survived registration. The pairing
mirrors ``operations/core_ops.py``'s ``CORE_OPERATIONS`` /
``register_core_operations()``, so a reader who knows one recognises the other."""


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """A tool declaration bound to the origin that registered it.

    Thinner than ``RegisteredOperation``, which also carries a handler and an
    owning module id: a tool has no handler of its own (``module-contract.md``:
    "it cannot reach storage except through the operation"), and its module is a
    property of the operation it names rather than of the tool.
    """

    declaration: ToolDeclaration
    origin: str

    @property
    def name(self) -> str:
        return self.declaration.name


class ToolRegistry:
    """The process-wide tool table. One instance is exported as :data:`TOOL_REGISTRY`.

    Deliberately shaped like ``OperationRegistry``: ``register(decl, *, origin=...)``
    validates and records, ``declarations()``/``names()`` read the live set back, and
    a repeat of the identical declaration under the identical origin is a no-op so
    startup and a test fixture can both call :func:`register_core_tools`.
    """

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, decl: ToolDeclaration, *, origin: str) -> RegisteredTool:
        """Validate and record ``decl``, or raise ``RegistrationRefused`` naming it.

        Two refusals, both borrowed whole from the operation registry rather than
        restated here:

        - **no declared safety class** -- the same ``isinstance`` test
          ``OperationRegistry.register`` applies, for the same reason: pydantic
          refuses an ordinary construction without one, so only a
          ``model_construct``-ed declaration can arrive here missing it, and that
          is precisely the shape a module could smuggle in.
        - **a reserved input field** -- :func:`~rheo_core.operations.registry.
          reserved_input_fields`, the *same function* the operation registry calls,
          over the same single ``RESERVED_INPUT_FIELDS`` list in
          ``rheo_contracts.manifest``. Calling it rather than copying it is what
          makes criterion 20 one check with two call sites instead of two checks
          that can drift; it is also what lets a single edit disable both, which is
          how the criterion's mutation is demonstrated.
        """
        from rheo_core.operations.refusals import (  # deferred, see module docstring
            RegistrationRefused,
        )
        from rheo_core.operations.registry import (  # deferred, see module docstring
            reserved_input_fields,
        )

        if not isinstance(decl, ToolDeclaration):
            raise TypeError("register() takes a ToolDeclaration")
        name = decl.name
        if not isinstance(name, str) or not name:
            raise RegistrationRefused(str(name), "a tool needs a non-empty name")
        if not isinstance(origin, str) or not origin:
            raise RegistrationRefused(name, "a registration needs a non-empty origin")
        if not isinstance(getattr(decl, "safety_class", None), SafetyClass):
            raise RegistrationRefused(name, "declares no safety class")
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
        existing = self._tools.get(name)
        if existing is not None:
            if existing.declaration == decl and existing.origin == origin:
                return existing
            raise RegistrationRefused(
                name, f"already registered (origin {existing.origin!r})"
            )
        registered = RegisteredTool(decl, origin)
        self._tools[name] = registered
        return registered

    def declarations(self) -> tuple[ToolDeclaration, ...]:
        """Every tool currently registered, in registration order."""
        return tuple(tool.declaration for tool in self._tools.values())

    def lookup(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


TOOL_REGISTRY: Final = ToolRegistry()


def register_core_tools(
    registry: ToolRegistry = TOOL_REGISTRY,
) -> tuple[RegisteredTool, ...]:
    """Register every declaration in :data:`CORE_TOOLS` (idempotent).

    Both go in under ``CORE_ORIGIN``, including ``harness_get_note``: the
    declaration lives in this shipped module, not in the harness, and the origin
    records who offered it. Nothing is gated on the profile here -- see the module
    docstring for why ``agent_default``'s intersection is the gate instead.

    Called by ``apps/core``'s startup, by ``tests/postgres/test_tokens.py`` and by
    ``tests/postgres/test_mcp_transport.py``. Calling it more than once in a
    process is expected, not a conflict.
    """
    return tuple(registry.register(decl, origin=CORE_ORIGIN) for decl in CORE_TOOLS)


def read_only() -> frozenset[str]:
    """Every registered operation of ``SafetyClass.READ``."""
    from rheo_core.operations.registry import REGISTRY  # deferred, see docstring

    names: set[str] = set()
    for name in REGISTRY.names():
        registered = REGISTRY.lookup(name)
        assert registered is not None  # names() and lookup() share one table
        if registered.declaration.safety_class is SafetyClass.READ:
            names.add(name)
    return frozenset(names)


def agent_default() -> frozenset[str]:
    """Every operation named by a registered tool, live-filtered by the registry.

    Reads :data:`TOOL_REGISTRY`, not :data:`CORE_TOOLS`: a declaration that never
    reached the registry -- because nothing called :func:`register_core_tools`, or
    because ``register`` refused it -- names no operation here.
    """
    from rheo_core.operations.registry import REGISTRY  # deferred, see docstring

    declared = frozenset(tool.operation for tool in TOOL_REGISTRY.declarations())
    return declared & REGISTRY.names()


def cli_full() -> frozenset[str]:
    """Every registered operation except :data:`NON_TOKEN_ISSUABLE`."""
    from rheo_core.operations.registry import REGISTRY  # deferred, see docstring

    return REGISTRY.names() - NON_TOKEN_ISSUABLE


PACKAGE_SETS: Final[dict[str, Callable[[], frozenset[str]]]] = {
    "read_only": read_only,
    "agent_default": agent_default,
    "cli_full": cli_full,
}
"""Name -> evaluator, for ``issue.py``'s named-set resolution (``rheo token issue
--set <name>``). Each callable evaluates against the process-wide ``REGISTRY``."""
