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
policy can never drift apart). :data:`CORE_TOOLS` names the declarations
:func:`register_core_tools` feeds it: the four production core tools
(``workspace_status``, ``operations_get``, ``operations_list``, ``audit_list``;
``runtime-and-mcp.md`` § Release-one tool set) and ``harness_get_note`` (test
profile only), mirroring how
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

This module defines its own tiny input models for every tool rather than
importing ``rheo_core.operations.core_ops``, ``operation_ops``, ``audit.operations``
or ``tests/harness/registry.py``'s: the first three would close the import cycle
below, and the last would make a shipped package depend on test-only code that is
never installed. Duplicating a few small, stable shapes is cheaper than either. The
mirrors are held to their originals by ``tests/test_core_tools.py``, which compares
each tool's published JSON schema with its operation's and checks each tool's safety
class against the operation's declaration.

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
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from rheo_contracts import SafetyClass, ToolDeclaration

from rheo_core.settings import CORE_ORIGIN
from rheo_core.tokens.policy import NON_TOKEN_ISSUABLE

_WORKSPACE_STATUS_OPERATION: Final = "core.workspace.status"
"""Duplicated from ``operations/core_ops.py``'s ``WORKSPACE_STATUS`` -- see the
module docstring's cycle note. The two are proven equal by
``test_tokens.py``'s own ``agent_default`` assertion, not by a shared import."""

_OPERATION_GET_OPERATION: Final = "core.operation.get"
_OPERATION_LIST_OPERATION: Final = "core.operation.list"
_AUDIT_LIST_OPERATION: Final = "core.audit.list"
"""Duplicated from ``operations/operation_ops.py``'s ``OPERATION_GET`` /
``OPERATION_LIST`` and ``audit/operations.py``'s ``AUDIT_LIST``, for the cycle
reason above; ``tests/test_core_tools.py`` proves each names a registered
operation."""

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


class _OperationRefToolInput(BaseModel):
    """Mirrors ``operations/operation_ops.py``'s ``OperationRef``: which record."""

    model_config = ConfigDict(extra="ignore")

    operation_id: UUID


class _OperationListToolInput(BaseModel):
    """Mirrors ``operations/operation_ops.py``'s ``OperationListInput``, bound and
    all: the ``ge``/``le`` pair is what keeps a model's ``-1`` an ``input_invalid``
    refusal at the tool rather than a driver error at the query."""

    model_config = ConfigDict(extra="ignore")

    limit: int = Field(default=50, ge=1, le=500)


class _AuditListToolInput(BaseModel):
    """``audit/operations.py``'s ``AuditListInput`` **narrowed to ``limit``**.

    The operation's two opt-ins, ``include_tool_telemetry`` and
    ``include_deletions``, are deliberately not declared, so the façade refuses
    them as unknown arguments (``tool_facade._validated``) and the operation runs
    with both at their ``False`` defaults. Each opens an owner/operator-only
    collection (§ A11's tool telemetry, § A8's deletion ledger with the exact
    closure counters every other surface withholds). The operation refuses both to
    every token kind but ``cli`` on every surface as well
    (``tokens.policy.PERSON_HELD_TOKEN_KINDS``), because this narrowing
    binds only the tool call; since issue #130 the ``api`` surface also refuses
    ``mcp`` tokens outright, and the operation's rule stays as the second layer. A
    person with the role still reaches both through a session or a ``cli`` token; a
    model reads the audit records alone, which are metadata (actor, operation name,
    subject reference, request digest, outcome) and carry no payload.
    """

    model_config = ConfigDict(extra="ignore")

    limit: int = Field(default=50, ge=1, le=500)


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

OPERATIONS_GET_TOOL: Final[ToolDeclaration] = ToolDeclaration(
    name="operations_get",
    safety_class=SafetyClass.READ,
    operation=_OPERATION_GET_OPERATION,
    input_model=_OperationRefToolInput,
    description=(
        "Reads one operation record of this workspace by its operation_id: its "
        "state, outcome and terminal check. Use it to poll a long-running call or "
        "a call held at approval_required. An id from another workspace, or one "
        "that does not exist, is not_found."
    ),
)

OPERATIONS_LIST_TOOL: Final[ToolDeclaration] = ToolDeclaration(
    name="operations_list",
    safety_class=SafetyClass.READ,
    operation=_OPERATION_LIST_OPERATION,
    input_model=_OperationListToolInput,
    description=(
        "Lists this workspace's most recently created operation records, newest "
        "first (limit 1 to 500, default 50)."
    ),
)

AUDIT_LIST_TOOL: Final[ToolDeclaration] = ToolDeclaration(
    name="audit_list",
    safety_class=SafetyClass.READ,
    operation=_AUDIT_LIST_OPERATION,
    input_model=_AuditListToolInput,
    description=(
        "Lists this workspace's most recent audit records, newest first (limit 1 "
        "to 500, default 50). Owner and operator only. Records are metadata: who "
        "acted, which operation, on which record, and the outcome; never a payload."
    ),
)

HARNESS_GET_NOTE_TOOL: Final[ToolDeclaration] = ToolDeclaration(
    name="harness_get_note",
    safety_class=SafetyClass.READ,
    operation=_HARNESS_NOTE_GET_OPERATION,
    input_model=_HarnessNoteRefToolInput,
)

CORE_TOOLS: Final[tuple[ToolDeclaration, ...]] = (
    WORKSPACE_STATUS_TOOL,
    OPERATIONS_GET_TOOL,
    OPERATIONS_LIST_TOOL,
    AUDIT_LIST_TOOL,
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

    Shaped like ``OperationRegistry`` in its *interface*: ``register(decl, *,
    origin=...)`` validates and records, ``declarations()``/``names()`` read the live
    set back, and a repeat of the identical declaration under the identical origin is
    a no-op so startup and a test fixture can both call :func:`register_core_tools`.

    **The validation is not the same set of rules, and saying which is the point.**
    ``OperationRegistry.register`` applies five checks a tool registration does not
    reach the same way. What is carried across, what is not, and why:

    - **Carried: the safety-class presence check, the reserved-input-field check, the
      ``extra = "allow"`` check, and the test-harness profile gate**
      (:func:`~rheo_core.operations.registry.check_origin_profile`, so the
      ``test_harness`` origin is refused outside ``profile = test`` here exactly as it
      is there).
    - **Carried, and specific to tools: a tool may not name a non-token-issuable
      operation.** An operation registration has no equivalent, because an operation
      may legitimately be one of the six; a *tool* naming one would put it in
      ``agent_default`` and hand the façade a lever R3 item 6 exists to remove ("a
      model cannot approve what it proposed"). ``tokens/issue.py`` strips the six
      again at mint time, so this is the outer of two layers rather than the only
      one — but it is the layer that keeps ``issue.py``'s own docstring true where it
      says a named package set "never contained them in the first place, by
      ``sets.py``'s own construction".
    - **Not carried: the name-grammar and module-id-prefix rules**
      (``check_origin``'s first two clauses). They cannot be applied verbatim and
      applying them approximately would be worse than not applying them.
      ``OperationRegistry`` derives a module id from a dotted operation name
      (``<module_id>.<noun>.<verb>``); a **tool** name is
      ``<module_id>_<verb>[_<noun>]`` (``module-contract.md``), not dotted, and the
      core's own tools do not carry a ``core_`` prefix at all -- ``workspace_status``
      is one. Deriving the module id from the tool's *operation* instead is the
      obvious alternative and is wrong for a different reason:
      ``module-contract.md:133`` expressly permits a module's tool to name a core
      operation ("the deletion tools do"), so that check would refuse something the
      ratified contract allows.

    **So the residual gap, stated rather than left for a reader to find: nothing here
    binds a tool's name to the origin that registered it.** A module may register a
    tool under any name not already taken. Closing that needs a ratified rule about
    tool-name prefixes that ``module-contract.md`` does not currently give -- the
    grammar is stated but no registration rule enforces it, and the core's own tools
    are the counterexample to the obvious reading.
    """

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, decl: ToolDeclaration, *, origin: str) -> RegisteredTool:
        """Validate and record ``decl``, or raise ``RegistrationRefused`` naming it.

        Five refusals. Four are the operation registry's own checks, reached through
        the operation registry's own functions rather than copies of them; the fifth
        is the one rule that is specific to tools. See the class docstring for the
        two ``OperationRegistry`` rules that deliberately do **not** apply.

        - **No declared safety class** -- the same ``isinstance`` test, for the same
          reason: pydantic refuses an ordinary construction without one, so only a
          ``model_construct``-ed declaration can arrive here missing it, and that is
          precisely the shape a module could smuggle in.
        - **The ``test_harness`` origin outside ``profile = test``** --
          :func:`~rheo_core.operations.registry.check_origin_profile`, the same
          function, so the gate criterion 18 leans on cannot be stepped around by
          registering a tool instead of an operation.
        - **A reserved input field** -- :func:`~rheo_core.operations.registry.
          reserved_input_fields`, the *same function* the operation registry calls,
          over the same single ``RESERVED_INPUT_FIELDS`` list in
          ``rheo_contracts.manifest``. Calling it rather than copying it is what
          makes criterion 20 one check with two call sites instead of two checks
          that can drift; it is also what lets a single edit disable both, which is
          how the criterion's mutation is demonstrated.
        - **``extra = "allow"``** -- the companion the operation registry pairs with
          the field-name check, and needed for the same reason: a model that declares
          no reserved field and accepts every extra key carries a reserved name into
          ``model_extra`` regardless, so the field-name check alone is a check on
          spelling rather than on what the model accepts.
        - **A non-token-issuable operation** -- tool-specific; see the class
          docstring.
        """
        from rheo_core.operations.refusals import (  # deferred, see module docstring
            RegistrationRefused,
        )
        from rheo_core.operations.registry import (  # deferred, see module docstring
            check_origin_profile,
            reserved_input_fields,
        )

        if not isinstance(decl, ToolDeclaration):
            raise TypeError("register() takes a ToolDeclaration")
        name = decl.name
        if not isinstance(name, str) or not name:
            raise RegistrationRefused(str(name), "a tool needs a non-empty name")
        if not isinstance(origin, str) or not origin:
            raise RegistrationRefused(name, "a registration needs a non-empty origin")
        check_origin_profile(origin)
        if not isinstance(getattr(decl, "safety_class", None), SafetyClass):
            raise RegistrationRefused(name, "declares no safety class")
        if decl.operation in NON_TOKEN_ISSUABLE:
            raise RegistrationRefused(
                name,
                f"names non-token-issuable operation {decl.operation!r}; a tool "
                "naming one would carry it into the agent_default set, and no "
                "token of any kind may hold it",
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
