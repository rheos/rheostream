"""The ``WorkspaceContext`` type every boundary resolves, and the values it holds.

``WorkspaceContext`` is constructed in exactly one place:
``packages/core/src/rheo_core/boundary/``. A repository-wide AST scan enforces that, so
nothing outside the boundary package — tests included — hand-builds a context.

Field list and semantics come from ``docs/architecture/overview.md`` § The workspace
context. Nine fields, no more: adding one here changes every boundary at once.
"""

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, InstanceOf

from rheo_contracts.purposes import ContextPurpose


class Role(StrEnum):
    """Who the actor is to this workspace (``control.membership``, or the boundary)."""

    OWNER = "owner"
    MEMBER = "member"
    OPERATOR = "operator"
    SERVICE = "service"


class ActorKind(StrEnum):
    """What kind of principal is acting. ``system`` carries no id."""

    ACCOUNT = "account"
    TOKEN = "token"
    OPERATOR = "operator"
    SYSTEM = "system"
    CONNECTION = "connection"


class Entry(StrEnum):
    """Which boundary built the context.

    A closed set: a new entry point is a boundary change, not an enum member added for
    a test's convenience.
    """

    WEB = "web"
    API = "api"
    MCP = "mcp"
    CLI = "cli"
    JOB = "job"
    INTAKE = "intake"
    CHANNEL = "channel"


class AudienceKind(StrEnum):
    """Who may see an operation's outputs.

    Exactly the four kinds ratified in ``overview.md`` § The workspace context. The
    memory module maps this onto a memory's audience, so a fifth kind is a documented
    architecture change, never a local convenience.
    """

    SESSION = "session"
    TOKEN = "token"
    JOB = "job"
    CHANNEL = "channel"


class Actor(BaseModel):
    """The acting principal. ``id`` is null for a ``system`` actor and only then."""

    model_config = ConfigDict(frozen=True)

    kind: ActorKind
    id: UUID | None


class Audience(BaseModel):
    """Who may see the outputs of work done under this context."""

    model_config = ConfigDict(frozen=True)

    kind: AudienceKind
    id: UUID | None


class AuthenticatedPrincipal(BaseModel):
    """The verified account and bound purpose behind a context, or the absence of
    either. Constructed only where WorkspaceContext itself is."""

    model_config = ConfigDict(frozen=True)

    account_id: UUID | None
    bound_purpose: ContextPurpose | None


_ALL_OPERATIONS_CREATED = False


class AllOperations:
    """Sentinel for "every operation", used instead of a magic ``"all"`` string.

    Exactly one instance exists, exported as ``ALL_OPERATIONS``, so the downstream
    authorization test is an identity check (``ctx.operation_set is ALL_OPERATIONS``)
    that no supplied string can satisfy. A second construction raises.

    ``__copy__``/``__deepcopy__`` return the singleton and ``__reduce__`` returns the
    module-global name: without them, copying or pickling a context would route through
    ``__new__`` and hit the second-construction guard, failing far from the cause.
    """

    def __new__(cls) -> Self:
        global _ALL_OPERATIONS_CREATED
        if _ALL_OPERATIONS_CREATED:
            raise RuntimeError(
                "AllOperations is a singleton; use the exported ALL_OPERATIONS."
            )
        _ALL_OPERATIONS_CREATED = True
        return super().__new__(cls)

    def __repr__(self) -> str:
        return "ALL_OPERATIONS"

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        return self

    def __reduce__(self) -> str:
        # A string return tells pickle to resolve this module global by name, so
        # unpickling reuses the singleton instead of calling ``__new__``.
        return "ALL_OPERATIONS"


ALL_OPERATIONS = AllOperations()


class WorkspaceContext(BaseModel):
    """The resolved answer to "who is acting, in which workspace, through what".

    Every entry point resolves one before any service is called (FR-1, guardrail 2), and
    no service accepts a workspace or actor any other way (FR-2, FR-23). Frozen: a
    context is a decision already made, not a bag a handler may edit mid-call.
    """

    model_config = ConfigDict(frozen=True)

    workspace_id: UUID
    actor: Actor
    role: Role
    entry: Entry

    audience: Audience | None = None
    """Who may see the outputs, or ``None`` when nobody in particular may.

    Optional deliberately. ``overview.md`` says of the ``job`` kind that "a scheduled
    job has none", so the ratified design already contemplates a context with no
    audience, and every consumer must already handle that case. The operator context
    therefore uses ``None`` rather than a ``session`` audience with a null id: the
    latter would make an operator context and a real web-session context differ only by
    that null, and would let later code branching on ``audience.kind == session`` treat
    operator output as session output.
    """

    # ``InstanceOf[AllOperations]`` rather than a bare ``AllOperations``: pydantic
    # cannot build a schema for an arbitrary class and raises
    # PydanticSchemaGenerationError at class-definition time, which would be an
    # import-time crash of rheo_contracts. The documented first choice,
    # ``__get_pydantic_core_schema__``, needs ``pydantic_core``, whose root module the
    # contracts import scan (B14) rejects. ``InstanceOf`` gives the same strict
    # is-instance validation without loosening the whole model with
    # ``arbitrary_types_allowed``, and preserves the identity ALL_OPERATIONS relies on.
    operation_set: frozenset[str] | InstanceOf[AllOperations]
    enabled_modules: frozenset[str]
    request_id: UUID

    principal: AuthenticatedPrincipal
    """The server-resolved account and bound purpose behind this context.

    Required with no default, so every construction site states what it verified
    rather than inheriting an absence. ``AuthenticatedPrincipal(account_id=None,
    bound_purpose=None)`` is the operator's honest answer and is spelled out there;
    a default here would make "nobody checked" and "there is nobody" the same value.
    """
