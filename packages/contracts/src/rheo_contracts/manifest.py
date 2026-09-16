"""The operation-declaration subset of the module contract that the registry needs.

Source of truth: ``docs/architecture/module-contract.md`` § Operations, tools, events.

Not here, and deliberately:

- ``ModuleManifest`` itself is phase 2, together with module install/enable lifecycle.

``ToolDeclaration`` arrives here in C8 (run 0b2), with the MCP facade
(``apps/mcp/src/rheo_app_mcp/tools.py``) as its first reader.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from rheo_contracts.context import Role
from rheo_contracts.safety import SafetyClass

RESERVED_INPUT_FIELDS = frozenset(
    {
        "workspace_id",
        "workspace",
        "actor_id",
        "actor",
        "tenant_id",
        "database",
        "schema",
        "connection_string",
        "dsn",
        "sql",
        "table_name",
        "statement",
    }
)
"""Input-model field names registration refuses, verbatim from the module contract.

The last three make criterion 20's "accepts no SQL, table name, or query fragment" a
mechanical check over the registered input models. This is the single list the operation
registry and criterion 6's test share; do not restate it anywhere else.
"""


class Idempotency(StrEnum):
    """How a repeated call is made to mean one thing.

    ``KEYED`` is absent on purpose: ``module-contract.md`` says it "arrives with phase
    five", and release one has no keyed idempotency and no stored-result table.

    ``NATURAL`` names a unique index that makes a repeat the same row. The registry is
    meant to assert that index is present in the module's schema **at module install**,
    which is phase 2 — so the 0b1 registry accepts ``NONE`` and ``NATURAL`` and performs
    no index validation. Do not go looking for that check; it is not written yet.
    """

    NONE = "none"
    NATURAL = "natural"


class AuditSpec(BaseModel):
    """What an operation's audit row records about its subject.

    Exactly one field. ``subject_field`` is the name of the input-model field carrying
    the subject :class:`~rheo_contracts.refs.RecordRef`, and ``None`` when the operation
    acts on no record (``core.settings.set`` and ``core.settings.set_member`` are the
    release-one examples). Grounded in ``intake-and-events.md`` (the audit row's
    ``subject_ref text null`` comes "From the operation's ``AuditSpec``") and
    ``confirmation-and-safety.md`` (``AuditSpec(subject = the receipt)``).

    This shape is frozen for 0c2's audit dispatcher. It is declared and stored only: no
    code in run 0b writes an audit row.
    """

    model_config = ConfigDict(frozen=True)

    subject_field: str | None


class OperationDeclaration(BaseModel):
    """What a module tells the registry about one operation.

    The subset of ``module-contract.md`` § Operations that the release-one registry
    needs. ``long_running`` **is** declared here, by run 0c2: it is what
    ``rheo_core.operations.dispatch`` reads to decide whether to mint a
    ``core.operation`` row before it opens the work transaction. ``guards`` is still
    not declared here; it belongs to the dispatcher work that follows.

    **The handler is not a field, and that is a named deviation.** The ratified contract
    types it ``Callable[[WorkspaceContext, UnitOfWork, input], output]``, but
    ``UnitOfWork`` lives in ``rheo_core.storage`` and ``rheo_contracts`` may import only
    stdlib, ``pydantic`` and ``rheo_contracts`` — the boundary the contracts import scan
    enforces. So the handler is passed alongside the declaration at registration,
    ``OperationRegistry.register(decl, handler, *, origin)`` in
    ``rheo_core.operations``, where ``UnitOfWork`` is importable and mypy strict checks
    the signature. Typing it ``Callable[..., BaseModel]`` here was the alternative, and
    was rejected: it keeps the ratified field list at the cost of the one type check
    that matters.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    safety_class: SafetyClass
    roles: frozenset[Role] = frozenset({Role.OWNER, Role.MEMBER})
    input_model: type[BaseModel]
    output: type[BaseModel]
    idempotency: Idempotency
    audit: AuditSpec | None = None
    """Required non-``None`` above ``READ``.

    The refusal that enforces it — "class above ``READ`` with ``audit = None``:
    refused, naming the operation" (``module-contract.md`` § Registration rules) — is
    checked at registration for every non-``READ`` declaration, by run 0c2's own
    registry rule (criterion 14). A ``READ`` declaration may still carry ``None``.
    """

    long_running: bool = False
    """Whether dispatching this operation mints a ``core.operation`` record.

    Default ``False``, so every declaration shipped before run 0c2 keeps exactly
    today's behaviour: no record is minted and the operation envelope's
    ``operation_id`` stays null for it.

    ``True`` has **one** meaning: the work runs as a job, the dispatcher mints a
    ``pending`` record and returns the id immediately, and the worker that finishes
    the job terminalises the record. **The dispatcher never writes ``succeeded``** —
    reporting success is the worker's alone, because only the worker knows the work
    was done rather than merely scheduled.

    It does write ``failed``, and only on the paths where no worker will ever see the
    record: the mint commits in a transaction of its own, so a dispatch that then
    refuses, raises, returns the wrong output type or queues no job at all would
    otherwise leave a committed ``pending`` row that nothing can move.
    ``rheo_core.operations.dispatch``'s own module docstring enumerates those exits.
    """


class ToolDeclaration(BaseModel):
    """What a module tells the MCP facade about one callable tool (C8, run 0b2).

    Four fields: the three the façade's ``list_tools``/``call_tool`` seam has always
    read, and ``safety_class``, added by run 0c3 together with the streamable-HTTP
    transport and the tool registration path that can refuse a declaration missing
    it. Guards and streamed output shape are still not declared here; each is a
    shape with no reader yet, which is the same reasoning that kept this whole type
    out of C1.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    """The tool's own name, as a caller of ``call_tool`` names it — distinct
    from ``operation``, which is the registry operation it dispatches to."""

    safety_class: SafetyClass
    """Required, with no default, exactly as :attr:`OperationDeclaration.safety_class`
    is. ``rheo_core.tokens.sets.ToolRegistry.register`` refuses a declaration that
    carries none, naming the tool — which is only reachable through
    ``model_construct``, since pydantic itself refuses an ordinary construction.

    The ratified contract (``module-contract.md`` § Operations, tools, events) also
    says this class "must equal the operation's, or registration fails". That second
    rule is **not** checked here or at tool registration: a tool may be registered
    before its operation is, and ``harness_get_note``'s operation is registered only
    under ``profile = test``, so the comparison has nothing to read in the general
    case. Stated rather than silently dropped."""

    operation: str
    """The registered operation name this tool calls (``dispatch(ctx, operation,
    arguments)``); not itself validated against the registry at declaration
    time — a declared tool naming an operation that never registers is simply
    absent from ``rheo_core.tokens.sets.agent_default``'s live intersection."""

    input_model: type[BaseModel]
    """Not used to validate a call in this run: the operation's own declared
    input model is what ``dispatch`` validates against. Carried here for the
    façade's own tool-listing metadata, and for a future transport to describe
    the tool's shape to a client."""
