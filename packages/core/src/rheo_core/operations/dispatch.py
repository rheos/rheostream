"""``dispatch(ctx, name, payload) -> OperationOutcome``: authorize, validate, run in
one unit of work, commit.

The ``context_required`` check comes first, before any lookup: ``None`` or an object
that is not a ``WorkspaceContext`` is refused without the registry being consulted.
Then ``authorize``; then, for an operation above the read class, the audit sink of its
owning module (``audit_sink_missing`` when it has none); then the payload is validated
into the declaration's input model (``input_invalid``, naming the failing fields but
never echoing values); then, for a ``long_running`` declaration and only for one, the
operation record is minted and committed on its own; then **one** work ``UnitOfWork``
is opened through ``route(ctx)``, the handler runs, the audit row is written, and the
unit of work commits. A handler that raises
:class:`OperationRefused` (or a
``StorageRefusal``) rolls back and yields that state; any other exception rolls back,
is logged with the context's ``request_id``, and yields ``failed`` with the fixed
code ``handler_failed`` and only the exception's class name as the text — never its
message, which for a driver error carries the statement and its parameters.

The handler receives a ``HandlerUnitOfWork`` — the same connection, with ``commit``,
``rollback`` and ``__enter__`` refused ``handler_may_not_commit``. Ending the one
transaction is this function's job.

**This function is the audit seam's one caller.** Issue #45 kept the ``AuditSink``
protocol and the module-keyed registry as substrate and left the call site — and what
a missing registration means — to 0c2; this is it. For every operation whose safety
class is not ``READ`` the sink installed for the *owning module* is resolved once, a
``None`` is refused ``audit_sink_missing`` before the handler runs, and exactly one
``core.audit_record`` row is written per dispatch: on the success path inside the
operation's own transaction, so a mutation and its record commit together or neither
does, and on every reachable non-success path in a short transaction of its own.
:func:`_audit_alone`'s docstring describes that second mechanism.

**The exits that write no row, enumerated — and there are four, not three.** ``spec.md``
§ D5 names three branches as structurally outside the reachable set; a fourth exists in
the control flow that its table does not describe, and it is named here rather than
left to be rediscovered from the code.

- ``context_required`` — no ``WorkspaceContext`` yet, so no workspace to route a row
  to and nothing that could open a connection to write one.
- ``operation_unknown`` — no declaration resolved, so no ``module_id`` to find a sink
  for and no safety class, which ``audit_record.safety_class`` is NOT NULL for.
- ``open_unit_of_work`` refusing — **when no fresh connection to the workspace is
  subsequently obtained.** That qualifier is the whole of it: the branch is outside the
  set because there is nothing to write the row *with*, not because of where it sits,
  so when a ``long_running`` dispatch's :func:`_fail_operation` does reach a fresh
  connection to end the record it minted, the row is written after all. See that
  branch's own comment.
- **an ``authorize`` refusal for a non-``READ`` operation whose module has no installed
  sink** — the fourth, and the one § D5's table does not carry. The sink is resolved
  *after* ``authorize`` answers, so ``operation_not_permitted``, ``module_disabled``
  and ``role_not_permitted`` return their own state while :func:`_audit_write` finds no
  sink and logs ``audit_row_unwritable``; the caller is not told
  ``audit_sink_missing``, because it was refused for a different reason first.
  Reachable only where both earlier layers were bypassed — a hand-built registry, or a
  process that never ran ``check_audit_paths`` — and no effect lands, since the refusal
  stands either way.

A ``READ`` operation writes no row either, by design rather than by structure: audit is
required only above the read class.

**The safety class branches the *running* of an operation, from run 0c3, and for
exactly three of the six classes.** ``read``, ``draft`` and ``mutate`` still simply
run — the class branch over them is the audit row, which every class but ``read``
gets. ``destructive``, ``external`` and ``financial`` do not run at all on an
ordinary call: :func:`_hold_for_approval` writes a ``core.approval`` row ``pending``,
holds the operation record at ``approval_required``, writes the call's audit row, and
answers with both ids, and the handler is never entered
(``docs/architecture/confirmation-and-safety.md`` § The six classes). The handler runs
later, from ``core.approval.approve``'s own transaction, through
``rheo_core.approvals.gate.execute_approved`` — **not** through a second pass of this
function. Run 0v's finding F14 recorded that ``overview.md`` read as though the class
were enforced; for the three upper classes it now is.

**One operation record is minted here, and only for a ``long_running`` declaration or
a held upper-class call.**
Run 0c2 gives ``OperationDeclaration.long_running`` its first reader: when it is set,
this function writes a ``pending`` ``core.operation`` row in its **own committed
transaction** before the work transaction is opened, so the id is readable through the
supported read while the work has not run; carries that id on
``OperationOutcome.operation_id`` and into the sealed view the handler receives; and
returns ``state = "pending"``. For every declaration that is not ``long_running``
nothing is minted and ``operation_id`` stays ``None``, exactly as before.

**This function never writes ``succeeded`` for an operation record.** The worker that
finishes the job carrying the id is the only thing that does, which is the whole of
the "one terminaliser" rule: a dispatcher that reported success would be claiming the
work was done at the moment it was merely scheduled.

**It does write ``failed``, on every exit below the mint that does not commit the work
transaction**, and it must. ``_mint_operation`` commits on its own, so a rollback of
the work transaction cannot undo it: without this, a handler that raised, refused,
returned the wrong type or queued nothing would leave a committed ``pending`` record
that no worker will ever see and nothing can ever move — while the caller was told the
dispatch failed. Six exits reach :func:`_fail_operation`: a ``StorageRefusal`` opening
the work transaction, ``OperationRefused`` or ``StorageRefusal`` out of the handler,
any other exception (a commit failure included, since the commit runs inside the same
``try``), an output that is not the declared type, and a ``long_running`` handler that
queued no job at all. The three refusals **above** the mint — ``context_required``,
``authorize`` and ``input_invalid`` — return before anything is written, so none of
them leaves a record behind.

**A ``long_running`` dispatch marks its workspace due once its work transaction has
committed, so the job is discovered on the very next pass.** ``work.jobs.enqueue``
calls ``mark_work_due`` after committing, and a job queued that way was always visible
immediately; a ``long_running`` handler instead calls ``enqueue_job`` inside *this*
function's work transaction, where it has no control-plane connection to write that
mark with. Until run 0c3 this function had no post-commit hook to write it for the
handler, so such a job waited for ``record_visit``'s floor —
``work.due_reconcile_seconds``, 900 s by default — to bring the workspace back round,
and a long-running operation could sit that long before it started.
:func:`_mark_workspace_due` closes that: one hook, on the success path, for every
``long_running`` dispatch and for any other whose handler asked for it through
``HandlerUnitOfWork.request_due_mark`` (``core.work.retry`` and ``.replay``, which put
deliveries back ``pending`` without a job), calling ``events/publish.py``'s
``mark_due_after_publish`` rather than a second function of the same shape. That
placement is what makes it one mechanism instead of one per caller —
``tests/harness/registry.py``'s ``harness.note.schedule`` is the pattern a future
long-running handler is told to copy, and it inherits the fix the same way it
inherited the gap.

The mark is written **after** the commit and outside every transaction, so a crash
between the two loses it. That loss is bounded by the reconcile floor rather than
fixed, because no transaction spans a workspace database and the control database;
``mark_due_after_publish``'s own docstring carries the reasoning, and
``work.jobs.enqueue`` accepts the identical trade for the identical reason.

**No outbox event is enqueued here.** The audit half is no longer absent — see above —
but nothing in this function publishes an event.

**It does carry the registry a handler would publish through.** ``consumers`` is an
optional keyword, defaulting to ``None``, threaded onto the ``HandlerUnitOfWork`` the
handler receives and read back as ``uow.consumers``. Neither ``rheo_core.events`` nor
this package holds a process-wide ``ConsumerRegistry``, deliberately — a test builds
its own and there is no global to reset — so the one instance is the composition
root's: ``apps/worker``'s ``main.py`` and ``apps/core``'s ``startup.py`` each build
one and hand it to every dispatch they make. There is no package-level default and no
per-call fallback: a publishing handler that finds ``None`` refuses
:data:`CONSUMERS_MISSING`, because a registry built per call would make a publish's
fan-out depend on which call built it.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Final
from uuid import UUID

from pydantic import BaseModel, ValidationError
from rheo_contracts import (
    OperationDeclaration,
    RecordRef,
    SafetyClass,
    WorkspaceContext,
)

from rheo_core.audit import (
    AUDIT_FAILED,
    AUDIT_REFUSED,
    AUDIT_SUCCEEDED,
    AuditOutcome,
    AuditSink,
    sink_for,
)
from rheo_core.boundary.context import CONTEXT_REQUIRED, Refusal
from rheo_core.events.publish import mark_due_after_publish
from rheo_core.operations.records import (
    APPROVAL_REQUIRED,
    AUDIENCE_NONE,
    PENDING,
    finish_failed,
    mint,
)
from rheo_core.operations.refusals import (
    FAILED,
    HANDLER_FAILED,
    INPUT_INVALID,
    OUTPUT_INVALID,
    SUCCEEDED,
    OperationRefused,
)
from rheo_core.operations.registry import (
    REGISTRY,
    OperationRegistry,
    RegisteredOperation,
)
from rheo_core.storage.backend import HandlerUnitOfWork, StorageRefusal, UnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.work.jobs import has_job_for_operation

if TYPE_CHECKING:
    from rheo_core.events.consumers import ConsumerRegistry

logger = logging.getLogger("rheo_core.operations")

_DETAIL_LIMIT: Final = 2000

AUDIT_SINK_MISSING: Final = "audit_sink_missing"
"""The refusal state for a non-``READ`` operation whose owning module has no installed
audit sink (AC 25).

Declared here rather than in ``refusals.py`` because it is the *dispatcher's* refusal:
the registry refuses a missing ``AuditSpec`` and startup refuses a missing sink, so by
the time a call reaches this state both earlier layers have been bypassed — a registry
built by hand, or a process that never ran ``check_audit_paths``.

**One other site raises it**, added by run 0c3 and named here rather than left for a
reader to find: ``rheo_core.approvals.gate.execute_approved`` imports this constant
when an approved operation's module turns out to have no sink at execution time. That
is the same refusal about the same operation at a later instant — the hold refused it
this way before minting anything — so it is this constant rather than a second
spelling of the same state."""

CONSUMERS_MISSING: Final = "consumers_missing"
"""The refusal state a handler that publishes an event raises when the dispatch it is
running under carries no :class:`~rheo_core.events.consumers.ConsumerRegistry`.

Declared here, beside :data:`AUDIT_SINK_MISSING`, for the same reason that one is:
both are *deployment wiring gaps* rather than caller mistakes, and ``refusals.py``
holds the registry's own authorization and validation states
(``operation_unknown``, ``role_not_permitted``, ``input_invalid``, ...), which these
are not.

**Nothing in this module raises it.** ``dispatch()`` cannot: a registry is needed only
by a handler that publishes, and no core handler publishes yet, so refusing here would
refuse every dispatch by a composition root that has no publishing operation to reach.
A publishing handler reads ``uow.consumers if isinstance(uow, HandlerUnitOfWork) else
None`` — the guard ``exports/operations.py`` and ``modules/operations.py`` already use
for ``operation_id`` — and raises ``OperationRefused(CONSUMERS_MISSING, ...)`` on
``None``. The alternative, a per-call ``ConsumerRegistry()``, is ruled out: a publish's
fan-out would then depend on which call built the registry, which is a silent wrong
answer rather than a refusal."""

_APPROVAL_CLASSES: Final = frozenset(
    {SafetyClass.DESTRUCTIVE, SafetyClass.EXTERNAL, SafetyClass.FINANCIAL}
)
"""The three classes that do not run on an ordinary call.

``confirmation-and-safety.md`` § The six classes gives each of them the same
dispatcher behaviour — "requires an ``approval`` in state ``approved`` bound to this
exact call; otherwise returns ``approval_required``" — and the document is explicit
that the class-to-behaviour table "is code, not configuration": no setting moves an
operation into or out of this set. Spelled here, in the dispatcher, because applying
the class is the dispatcher's job; ``rheo_core.approvals`` carries what happens next
and does not re-decide which calls reach it."""


@dataclass(frozen=True, slots=True)
class OperationError:
    """What a non-success outcome carries: a state name and a showable text."""

    error_code: str
    error_text: str


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    """``state`` is ``succeeded`` with ``result``, ``pending`` with ``result`` and an
    ``operation_id`` for a ``long_running`` dispatch, or a refusal state / ``failed``
    with ``error``.

    ``operation_id`` is the ``core.operation`` row :func:`dispatch` minted for this
    call: for a ``long_running`` declaration, and from run 0c3 also for an
    upper-class call held at ``approval_required``. It is ``None`` for every other
    declaration — which is every *other* operation registered in release one, so the
    API envelope still carries ``operation_id: null`` for all of them. It is the last
    field rather than the second so that nothing constructing this positionally had
    its arguments shift.

    ``approval_id`` is the ``core.approval`` row a held call is waiting on, and is
    ``None`` on every other outcome. Appended after ``operation_id`` for the same
    reason ``operation_id`` was appended after ``error``, and answering criterion
    19's "an approval-required state carrying an approval identifier": the identifier
    is a field of the outcome rather than something to parse out of the error text.
    """

    state: str
    result: BaseModel | None = None
    error: OperationError | None = None
    operation_id: UUID | None = None
    approval_id: UUID | None = None

    @property
    def ok(self) -> bool:
        return self.state == SUCCEEDED


def _refused(
    state: str, detail: str | None, *, operation_id: UUID | None = None
) -> OperationOutcome:
    """A refusal outcome, carrying the minted id when there is one.

    ``operation_id`` defaults to ``None`` because most refusals happen before
    anything is minted — the context check, ``authorize``, input validation. The
    refusals raised out of the handler happen after, and a caller that was handed an
    id must be able to look that record up whatever the outcome was.
    """
    return OperationOutcome(
        state,
        error=OperationError(state, detail or state),
        operation_id=operation_id,
    )


def _describe_validation_error(exc: ValidationError) -> str:
    """The failing locations and messages, never the input values."""
    parts: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)[:_DETAIL_LIMIT]


_UNCANONICAL_REQUEST: Final = "\x00uncanonical-request"
"""The canonical form a payload gets when nothing can render one for it.

Leads with a NUL, which no real canonical form can: every other value this module
digests is the output of ``json.dumps``, and a JSON document never begins with one. So
the sentinel collides with no genuine request rather than quietly sharing a digest
with one.
"""


def _canonical(value: object) -> str:
    """The canonical JSON of ``value``: sorted keys, no whitespace, lossy values.

    ``sort_keys`` makes two payloads that differ only in key order the same request;
    ``separators`` removes the whitespace that would otherwise make the same request
    two digests. ``default=str`` renders a value the caller never serialised — a
    ``UUID``, a ``datetime`` — rather than raising over it. The digest stores no input
    (that is the whole point of the column, per ``intake-and-events.md`` § Audit), so a
    lossy rendering of an exotic value costs nothing a reader of the row could have had
    anyway.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _stringified_keys(value: object) -> object:
    """``value`` with every mapping key rendered as a string, recursively.

    ``default=`` is the hook for an unserialisable **value** and does not reach a key:
    ``sort_keys=True`` sorts a mapping's keys by comparing them, so a nested mapping
    whose keys are of mixed types — ``{1: ..., "b": ...}`` — raises ``TypeError`` out
    of that comparison before the encoder is consulted at all. Coercing the keys first
    gives the sort something it can order. Two keys that coerce to the same string
    collapse, so this is lossier than the ordinary form; it is the fallback rather than
    the rule for exactly that reason, and it is still deterministic, which is all the
    column needs.
    """
    if isinstance(value, Mapping):
        return {str(key): _stringified_keys(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringified_keys(item) for item in value]
    return value


def _request_digest(payload: Mapping[str, object] | None) -> bytes:
    """The SHA-256 of the canonical request, for ``audit_record.request_digest``.

    Computed from the **raw payload**, once, before authorization, rather than from
    the validated input model — because three of the outcomes that need a row happen
    before validation has run or could run, and a digest that existed only on the
    validated paths would be the one column those rows could not fill.

    **Total, and guarded rather than asserted to be.** This runs before ``authorize``
    and outside every ``try`` in :func:`dispatch`, so an exception raised here is not a
    refusal the caller can read — it is a ``dispatch()`` that raised, which nothing
    above it expects. A JSON body cannot produce one (its keys are always strings), but
    every in-process caller can, the internal listener and this suite included, so the
    three renderings below run in order and the last cannot fail: the ordinary
    canonical form, then the same with mapping keys coerced to strings, then a fixed
    sentinel with the reason logged. A digest is not worth raising over, and the two
    degraded forms are still deterministic — the same payload always answers the same
    digest, which is the only property the column rests on.
    """
    raw: object = {} if payload is None else dict(payload)
    try:
        canonical = _canonical(raw)
    except (TypeError, ValueError):
        try:
            canonical = _canonical(_stringified_keys(raw))
        except (TypeError, ValueError) as exc:
            # No workspace or request id on the line: this function takes the payload
            # and nothing else, and a digest helper that needed a context to log would
            # be harder to call than the column is worth. The exception's class name
            # only, for the reason the handler's own log line gives — an encoder's
            # message can carry the input it choked on.
            logger.error(
                "request_digest_uncanonical",
                extra={"exception_type": type(exc).__name__},
            )
            canonical = _UNCANONICAL_REQUEST
    return sha256(canonical.encode("utf-8")).digest()


def _subject_ref(
    declaration: OperationDeclaration, model_input: BaseModel
) -> RecordRef | None:
    """The record this call acts on, per the declaration's ``AuditSpec``.

    ``AuditSpec.subject_field`` names *an input-model field carrying a*
    :class:`~rheo_contracts.refs.RecordRef` (``manifest.py``), so a field holding
    anything else is not a subject reference and records none. **No release-one
    declaration names a subject field** — every ``AuditSpec`` in the tree is
    ``AuditSpec(subject_field=None)``, because none of the shipped mutate operations
    acts on a registered record type — so no dispatch of a shipped operation reaches
    the ``getattr`` below, and this is written as the narrowest thing that satisfies
    the contract rather than as a parser for the string form nothing produces. It is
    not unpinned, though: ``tests/postgres/test_audit_dispatch.py`` registers a
    declaration that *does* name one, on a registry of its own, and asserts the
    formatted reference comes back on the row.

    **The ``default=None`` on the ``getattr`` is not a silent fallback.**
    ``OperationRegistry.register`` refuses a declaration whose ``subject_field`` names
    no field on its own input model, so by the time a declaration reaches here the
    attribute exists; the default is what makes this function total for a declaration
    built by hand around the registry, and such a declaration records a null rather
    than raising mid-dispatch.
    """
    spec = declaration.audit
    if spec is None or spec.subject_field is None:
        return None
    value = getattr(model_input, spec.subject_field, None)
    return value if isinstance(value, RecordRef) else None


@dataclass(frozen=True, slots=True)
class _AuditWrite:
    """Everything one dispatch's audit row needs that does not vary by outcome.

    Built once per dispatch and passed down, so the six keywords the protocol takes
    are spelled here and at no other call site — the in-transaction success write and
    every :func:`_audit_alone` call alike hand it an outcome and an operation id and
    nothing else. Scoped that way rather than counted: the number of exits that write
    a row changes whenever a branch is added, and a count in this docstring would be
    false the next time one is.

    Its existence is itself the "a row is written here" condition: it is ``None``
    exactly when no row is written, and every call site reads it that way.
    """

    operation: RegisteredOperation
    sink: AuditSink
    request_digest: bytes
    subject_ref: RecordRef | None

    def record(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        outcome: AuditOutcome,
        operation_id: UUID | None,
    ) -> None:
        """Write the row through ``uow``. Does not commit; the caller owns that."""
        declaration = self.operation.declaration
        self.sink.record(
            ctx,
            uow,
            operation=declaration.name,
            safety_class=declaration.safety_class,
            subject_ref=self.subject_ref,
            request_digest=self.request_digest,
            outcome=outcome,
            operation_id=operation_id,
        )


def _audit_write(
    operation: RegisteredOperation | None,
    *,
    request_digest: bytes,
    subject_ref: RecordRef | None = None,
) -> _AuditWrite | None:
    """What writes this dispatch's row, or ``None`` when no row is written.

    Three ways to get ``None``, all structural:

    - **no declaration** — ``operation_unknown``: there is no owning module to find a
      sink for and no safety class to record, and ``audit_record.safety_class`` is
      NOT NULL;
    - **a ``READ`` declaration** — audit is required only above ``READ``, and a read
      writes no row by design;
    - **a non-``READ`` declaration whose module has no installed sink** — nothing to
      write the row *with*. **Two call sites reach this branch, not one.** The
      authorization-refusal call site reaches it for an operation that was never
      wired, and answers with the authorize refusal's own state. The ordinary call
      site reaches it too, one line before :func:`dispatch` turns the ``None`` into
      the :data:`AUDIT_SINK_MISSING` refusal — so that refusal is *preceded* by this
      branch rather than routed around it, and every ``audit_sink_missing`` answer
      carries an ``audit_row_unwritable`` log line ahead of it. Either way the log is
      the point: a missing row is otherwise indistinguishable from a path that
      correctly writes none. ``check_audit_paths`` makes both unreachable in a process
      that ran its startup.
    """
    if operation is None:
        return None
    declaration = operation.declaration
    if declaration.safety_class is SafetyClass.READ:
        return None
    sink = sink_for(operation.module_id)
    if sink is None:
        logger.error(
            "audit_row_unwritable",
            extra={
                "operation": declaration.name,
                "module_id": operation.module_id,
            },
        )
        return None
    return _AuditWrite(operation, sink, request_digest, subject_ref)


def record_operation_audit(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    operation: RegisteredOperation,
    *,
    request_digest: bytes,
    model_input: BaseModel,
    outcome: AuditOutcome,
    operation_id: UUID | None,
) -> bool:
    """Write one audit row for ``operation`` in ``uow``'s transaction; answer whether
    a row was written.

    **It takes the validated input and derives the subject itself, rather than taking
    a subject its caller derived.** The subject is read with :func:`_subject_ref` —
    the same function :func:`dispatch` uses below — so the row an ordinary call writes
    and the row an approved execution writes cannot name a subject by two different
    rules. This parameter was a ready-made ``subject_ref`` in the first version of
    this function, and the approvals caller filled it from the
    ``core.approval.subject_ref`` **column**: a second stored copy of the subject that
    the payload digest does not cover, so tampering with that one column alone
    produced a ``succeeded`` audit row naming a record the operation never touched.
    Deriving from the verified input removes the second source rather than comparing
    against it, which is why there is no comparison anywhere for this.

    **The one audit call site outside this module's own dispatch path, and it is still
    inside this module.** ``rheo_core.approvals.gate.execute_approved`` runs an
    approved operation's handler from ``core.approval.approve``'s transaction rather
    than from :func:`dispatch`, so the row for *that* execution cannot be written by
    the block below — and writing it there would spell the sink protocol's six
    keywords a second time, which :class:`_AuditWrite` exists to prevent, and would put
    a second ``.record(`` caller in the tree, which
    ``tests/test_audit_sink.py::test_exactly_one_module_in_the_tree_calls_a_sink``
    exists to prevent. This function is how that path writes its row without doing
    either: it builds the same :class:`_AuditWrite` and calls the same method.

    ``False`` means no row was written, which for an operation above the read class
    means its module has no installed sink — :func:`_audit_write` has already logged
    ``audit_row_unwritable`` — and the caller decides what that means. (It also
    answers ``False`` for a ``READ`` declaration, which writes no row by design; no
    caller can reach that branch, since the only caller runs approved upper-class
    operations.)
    """
    audit = _audit_write(
        operation,
        request_digest=request_digest,
        subject_ref=_subject_ref(operation.declaration, model_input),
    )
    if audit is None:
        return False
    audit.record(ctx, uow, outcome=outcome, operation_id=operation_id)
    return True


def _audit_alone(
    ctx: WorkspaceContext,
    audit: _AuditWrite | None,
    *,
    outcome: AuditOutcome,
    operation_id: UUID | None = None,
) -> None:
    """Write one audit row in a short transaction of its own, and commit it.

    **The one mechanism behind every non-success shape**, which read differently in
    the control flow and are the same write:

    - a refusal or failure raised *after* the work transaction was opened — the row
      cannot live in that transaction, because it is rolled back and
      ``audit_record.outcome`` admits ``failed`` and ``refused`` precisely for rows
      that outlive one;
    - a refusal reached *before* the work transaction was ever opened — the four
      reachable ones are ``operation_not_permitted``, ``module_disabled``,
      ``role_not_permitted`` (all from ``authorize``, all after the declaration
      resolved) and ``input_invalid``. There is no transaction to roll back from, so
      the fresh connection is opened for the row alone;
    - a refusal because the work transaction could not be opened **at all**, on a
      dispatch that had already minted and committed an operation record. That branch
      is outside the set while nothing proves the workspace reachable, and inside it
      the moment ``_fail_operation`` reaches a fresh connection to end the record —
      which is the only thing that distinguishes the two, and is why the branch reads
      that call's answer rather than guessing.

    ``_finish_alone`` in ``work/loop.py`` is the shape both copy, and for the same
    reason: one terminal write, its own transaction, committed unconditionally.

    **Its own unit of work, and it may fail.** ``open_unit_of_work`` can refuse — the
    workspace may have become unreachable between the refusal and now — and this
    function logs rather than raising, because ``dispatch()`` answers its caller with
    an outcome and never raises, exactly as ``_fail_operation`` one screen up decides
    for the operation record. The success path is not protected by any such log: it
    writes inside the operation's transaction, so a failure there fails the operation,
    which is the property AC 23 rests on.
    """
    if audit is None:
        return
    try:
        with open_unit_of_work(ctx) as uow:
            audit.record(ctx, uow, outcome=outcome, operation_id=operation_id)
            uow.commit()
    except Exception:
        logger.error(
            "audit_row_not_written",
            extra={
                "operation": audit.operation.name,
                "outcome": outcome,
                "workspace_id": str(ctx.workspace_id),
                "request_id": str(ctx.request_id),
            },
        )


def _mint_operation(ctx: WorkspaceContext, declaration: OperationDeclaration) -> UUID:
    """Write this call's ``core.operation`` row and **commit it**, returning its id.

    **Its own transaction, and it must be.** AC 15 is that the id is readable through
    the supported read *before the operation completes*; a row written inside the work
    transaction is invisible to every other connection until that transaction commits,
    which is after the work has run. So this opens a unit of work of its own, writes
    one ``pending`` row, commits, and closes — and only then does :func:`dispatch` open
    the one the handler runs in. Two committed transactions per ``long_running``
    dispatch is the cost, and is why minting is gated on the declaration rather than
    done for every call.

    **The first of the three clock-read call sites in this module** — counted rather
    than asserted, because this sentence has now been wrong twice. ``dispatch()`` takes
    no instant and its callers are HTTP listeners with none to give it, so wall time
    enters the system here, the way it already does in ``storage/work_index.py``. The
    three, by the line that makes each read unavoidable:

    - **here**, for the ``pending`` row's ``created_at``;
    - :func:`_fail_operation`'s, for a minted record's terminal instant. It is a
      *separate* read and not this value passed forward, for the same reason as the
      third: it runs after the handler, whose duration is unbounded;
    - :func:`_mark_workspace_due`'s, stamping the moment the work became
      *discoverable* — after the work transaction committed, and again an unbounded
      handler after this row was written.

    **Two of the three postdate an unbounded handler, which is why none of them can be
    derived from another.** What takes its instant from a caller is everything in
    *other* modules that touches these rows — the worker's terminal write and the
    terminal check — so the discipline the repositories keep is unaffected; the claim
    is about them and is scoped to them, because this module's own terminal write
    (``_fail_operation``) is exactly the counter-example a wider claim would have
    falsified. It was uncounted before run 0c3's hook was added and stayed uncounted
    when this sentence was rewritten to add one: a stale number replaced by a different
    stale number.

    **Verify by grepping ``datetime.now(`` in this file rather than by reading the list
    above — and expect FOUR hits for three call sites.** The fourth is this sentence,
    which contains the pattern it is telling you to search for. Said here because a
    reader who finds four for a claim of three concludes the count is wrong a third
    time, which is the exact reaction this paragraph exists to prevent; the count that
    matters is of call sites, and a text search cannot tell one from a mention of one.

    A refusal out of ``open_unit_of_work`` propagates to the caller, which folds it
    into the same refusal outcome an unroutable workspace already produces. Nothing is
    minted for a workspace the call could not have reached anyway.
    """
    with open_unit_of_work(ctx) as uow:
        operation_id = mint(
            uow.connection,
            name=declaration.name,
            safety_class=declaration.safety_class.value,
            actor_kind=ctx.actor.kind.value,
            actor_id=ctx.actor.id,
            entry=ctx.entry.value,
            audience_kind=(
                AUDIENCE_NONE if ctx.audience is None else ctx.audience.kind.value
            ),
            audience_id=None if ctx.audience is None else ctx.audience.id,
            now=datetime.now(UTC),
        )
        uow.commit()
    return operation_id


def _mark_workspace_due(ctx: WorkspaceContext) -> None:
    """Tell the control plane this workspace has work, after the commit that queued it.

    **The post-commit hook a ``long_running`` dispatch needs, and there is one of it.**
    The handler enqueued its job inside this function's work transaction, against a
    workspace connection; the due-work index lives in the control database, and no
    transaction spans the two. So the mark cannot be part of the handler's write and
    has to happen here, once, after the commit — rather than in each handler, which
    could not reach the control plane anyway.

    ``events.publish.mark_due_after_publish`` is reused rather than reimplemented. It
    was written for the outbox and is exactly this shape — one control-plane
    transaction, opened and committed on its own — and a second function beside it
    would be two things to keep true about one rule.

    **Guarded, because an already-successful dispatch must not fail on this.** The work
    is committed and the caller is about to be handed a ``pending`` outcome carrying a
    real operation id. A control-plane hiccup here would turn that into an unhandled
    exception for a call that succeeded, which is strictly worse than the loss it would
    be reporting: a missing mark costs the job up to one reconcile interval
    (``work.due_reconcile_seconds``, 900 s) and nothing else, which is the same bounded,
    already-accepted loss ``mark_due_after_publish``'s own docstring describes for a
    crash in this exact window. Logged for the same reason :func:`_audit_alone` and
    :func:`_fail_operation` log rather than raise.

    **It can block, and the ceiling is 30 seconds — named here because this is the
    line a reader reaches it from.** ``mark_due_after_publish`` opens a transaction on
    ``PostgresBackend.control_engine``, which is built ``pool_size =
    storage.pool_max_connections`` (5 on the defaults) with ``max_overflow = 0``
    (``storage/postgres.py``). No ``pool_timeout`` is passed, so SQLAlchemy's
    ``QueuePool`` default of 30.0 s applies: with all five control connections checked
    out, this call waits up to that long before raising ``TimeoutError``, which the
    ``except`` below then swallows into a log line. The dispatch has already committed
    at that point, so the risk is **latency on an answered call**, never a lost effect
    — but half a minute is worth knowing about on a hot path.

    **Not fixed with a shorter timeout here, deliberately.** ``pool_timeout`` is set
    when the engine is created, not at checkout, so lowering it would change the
    waiting behaviour of *every* control-plane caller — ``work.jobs.enqueue``'s own
    mark, ``record_visit``, the worker's index read — to suit this one call site. That
    is a control-plane tuning decision with its own settings key, not a line this
    function gets to make on everyone's behalf. Contention is on the enqueue side under
    load, which ``storage/work_index.py``'s module docstring already states.
    """
    try:
        mark_due_after_publish(ctx.workspace_id, at=datetime.now(UTC))
    except Exception:
        logger.error(
            "work_due_mark_not_written",
            extra={
                "workspace_id": str(ctx.workspace_id),
                "request_id": str(ctx.request_id),
            },
        )


def _fail_operation(
    ctx: WorkspaceContext,
    operation_id: UUID | None,
    *,
    error_code: str,
    error_text: str,
) -> bool:
    """End a minted record ``failed``, in a short transaction of its own; answer
    whether a fresh unit of work was obtained.

    **Why the dispatcher writes a terminal state at all**, when the rule is that the
    worker is the terminaliser: the worker only ever sees a record whose job exists,
    and every exit that reaches here is one where no job does. :func:`_mint_operation`
    committed on its own, so the work transaction's rollback leaves the record behind;
    the caller has been told the dispatch failed; and nothing downstream will ever look
    at that row again. ``pending`` for ever is the one answer criterion 13 forbids,
    because the caller cannot tell it from work still in flight. **This never writes
    ``succeeded``** — that stays the worker's alone, and is why the record's success
    path still has exactly one writer.

    ``operation_id`` is ``None`` for every declaration that is not ``long_running``,
    which is every operation release one registers, so the common case returns without
    touching the database and the call sites need no ``if`` of their own.

    **Its own unit of work, and it may fail.** The work transaction has already been
    rolled back before any call site reaches here, so this opens a fresh one rather
    than reusing a dead connection. If the workspace has become unreachable between the
    mint and now, the record stays ``pending`` and the line is logged: ``dispatch()``
    answers its caller rather than raising, exactly as it does for a handler that
    failed, and a bounded loss on an unreachable database is the same trade
    ``events/publish.py``'s ``mark_due_after_publish`` documents one layer out.

    **The return value is evidence about the connection, not about the write.** ``True``
    means a unit of work was obtained here and the statement ran; ``False`` means either
    that there was no record to end (``operation_id`` is ``None``, so nothing was
    attempted) or that opening one was refused. The one caller that reads it is the
    ``open_unit_of_work`` refusal branch in :func:`dispatch`, which needs to know
    whether a *fresh* connection to this workspace is obtainable right now before it
    decides whether the audit row can be written. A zero rowcount still answers
    ``True``: the connection was obtained, which is the question.
    """
    if operation_id is None:
        return False
    try:
        with open_unit_of_work(ctx) as uow:
            applied = finish_failed(
                uow.connection,
                operation_id=operation_id,
                now=datetime.now(UTC),
                error_code=error_code,
                error_text=error_text[:_DETAIL_LIMIT],
            )
            uow.commit()
    except Exception:
        logger.error(
            "operation_record_left_pending",
            extra={
                "operation_id": str(operation_id),
                "workspace_id": str(ctx.workspace_id),
                "request_id": str(ctx.request_id),
            },
        )
        return False
    if not applied:
        # The write is predicated on the record still being open, so a zero rowcount
        # means something else terminalised it between the mint and here. Nothing in
        # release one can: no job carries this id on any path that reaches this
        # function. Logged rather than raised, because the record is terminal either
        # way and the outcome the caller is about to receive is unaffected.
        logger.warning(
            "operation record %s was already terminal when its dispatch failed; "
            "the record is left as it was",
            operation_id,
        )
    return True


def _hold_for_approval(
    ctx: WorkspaceContext,
    declaration: OperationDeclaration,
    model_input: BaseModel,
    audit: _AuditWrite,
) -> OperationOutcome:
    """Hold an upper-class call and answer ``approval_required`` with both ids.

    **The handler is not entered on this path, and that is the whole of the
    behaviour.** Nothing is recorded at any destination, no audit row claims success,
    and the operation record is written in the one state that says why nothing
    happened.

    The audit row's outcome is ``AUDIT_REFUSED``, not a fourth member. The
    ``audit_record_outcome`` check constraint holds exactly ``succeeded``, ``failed``
    and ``refused`` (``storage/work_tables.py``), and a fourth would need a migration
    that widens a frozen constraint, a widened ``AuditOutcome`` literal and a changed
    generated OpenAPI enum — for a row whose meaning ``refused`` already carries: the
    dispatcher declined to run the handler. The row's ``operation_id`` is what leads
    a reader to the record that says *why* it declined.

    ``hold_for_approval`` is imported here rather than at module level because
    ``rheo_core.approvals`` imports this package's submodules at *its* module level
    (``approvals/gate.py``'s docstring carries the direction and the reason), so a
    module-level import here would close the cycle. The same deferral, for the same
    reason, as ``core_ops.py``'s import of ``rheo_core.tokens.issue``.
    """
    from rheo_core.approvals.gate import hold_for_approval  # deferred: see docstring

    def record_audit(uow: UnitOfWork, operation_id: UUID) -> None:
        audit.record(ctx, uow, outcome=AUDIT_REFUSED, operation_id=operation_id)

    try:
        held = hold_for_approval(
            ctx,
            declaration=declaration,
            model_input=model_input,
            subject_ref=audit.subject_ref,
            now=datetime.now(UTC),
            record_audit=record_audit,
        )
    except StorageRefusal as refusal:
        # The hold opens the only unit of work on this path, so an unroutable
        # workspace refuses here exactly as it does for the mint one screen down,
        # and nothing was written to audit it with.
        return _refused(refusal.state, refusal.detail)
    if isinstance(held, Refusal):
        # The payload bound, refused before the hold opened anything — so the row is
        # written on a fresh connection, like every other pre-transaction refusal.
        _audit_alone(ctx, audit, outcome=AUDIT_REFUSED)
        return _refused(held.state, held.detail)
    return OperationOutcome(
        APPROVAL_REQUIRED,
        error=OperationError(
            APPROVAL_REQUIRED,
            f"{declaration.name} is {declaration.safety_class.value} class and "
            f"requires an approval; approval {held.approval_id} is pending",
        ),
        operation_id=held.operation_id,
        approval_id=held.approval_id,
    )


def _output_invalid(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    operation_id: UUID | None,
    audit: _AuditWrite | None,
    *,
    detail: str,
) -> OperationOutcome:
    """Roll the work back, end any minted record, audit the failure, and answer
    ``output_invalid``.

    Two branches reach it — a handler whose return value is not the declared output
    type, and a ``long_running`` handler that queued no job — and they are one
    function rather than two copies because the ordering is the part that has to be
    right at both: roll back first, so the two writes below each open their own
    transaction against a connection this one is no longer holding.

    The audit row's outcome is ``failed`` rather than ``refused``, matching the
    outcome this function returns: the caller asked for something the system was
    willing to do and the operation did not deliver it, which is a failure, not a
    refusal to act.

    The outcome's ``state`` stays ``failed`` with ``output_invalid`` as the code,
    which is the shape this branch has answered since 0b1 and which the API envelope's
    status map already knows.
    """
    _rollback(uow)
    _fail_operation(ctx, operation_id, error_code=OUTPUT_INVALID, error_text=detail)
    _audit_alone(ctx, audit, outcome=AUDIT_FAILED, operation_id=operation_id)
    return OperationOutcome(
        FAILED,
        error=OperationError(OUTPUT_INVALID, detail),
        operation_id=operation_id,
    )


def _rollback(uow: UnitOfWork) -> None:
    # A commit that failed has already closed the transaction; the ``with`` exit
    # rolls back anything still open, so a closed unit of work is not an error here.
    try:
        uow.rollback()
    except StorageRefusal:
        pass


def dispatch(
    ctx: object,
    name: str,
    payload: Mapping[str, object] | None = None,
    *,
    registry: OperationRegistry = REGISTRY,
    consumers: "ConsumerRegistry | None" = None,
) -> OperationOutcome:
    """Run ``name`` for ``ctx`` with ``payload``; see the module docstring.

    ``consumers`` is the composition root's one
    :class:`~rheo_core.events.consumers.ConsumerRegistry`, carried through to the
    :class:`~rheo_core.storage.backend.HandlerUnitOfWork` the handler receives. It
    defaults to ``None`` and there is **no package-level default registry**: a
    process that dispatches a publishing operation without wiring one has a
    deployment bug, and the handler refuses :data:`CONSUMERS_MISSING` rather than
    fanning out to a registry nobody subscribed to.
    """
    if not isinstance(ctx, WorkspaceContext):
        # Structurally outside the audit set, the first of three: there is no
        # ``WorkspaceContext`` yet, so there is no workspace to route a row to and
        # nothing that could open a connection to write one.
        return _refused(
            CONTEXT_REQUIRED,
            "dispatch() needs a WorkspaceContext from a boundary factory",
        )
    request_digest = _request_digest(payload)
    authorized = registry.authorize(ctx, name)
    if isinstance(authorized, Refusal):
        # ``operation_unknown`` is the second structural exclusion — ``lookup``
        # answers ``None`` for exactly that refusal, so ``_audit_write`` answers
        # ``None`` and no row is written. The other three refusals ``authorize``
        # returns resolved a declaration first, so each gets its own row, written on
        # a fresh connection because no work transaction was ever opened — **whenever
        # the owning module has a sink.** When it has none ``_audit_write`` answers
        # ``None`` here too and no row is written, and the caller is told the
        # authorize refusal rather than ``audit_sink_missing``, because the sink is
        # resolved only after this block. That is the fourth no-row exit the module
        # docstring enumerates, and the reason the claim above is scoped rather than
        # stated over all three.
        _audit_alone(
            ctx,
            _audit_write(registry.lookup(name), request_digest=request_digest),
            outcome=AUDIT_REFUSED,
        )
        return _refused(authorized.state, authorized.detail)
    operation = authorized.operation
    declaration = operation.declaration
    # Resolved **before** input validation and before the mint, so that every path
    # below this line is one a row can be written for. Refusing later would mean
    # owing an ``input_invalid`` row that nothing could write, and minting first
    # would leave a committed ``pending`` record for a call that is about to be
    # refused. ``None`` for a ``READ`` operation, which writes no row at all.
    audit = _audit_write(operation, request_digest=request_digest)
    if audit is None and declaration.safety_class is not SafetyClass.READ:
        return _refused(
            AUDIT_SINK_MISSING,
            f"no audit sink is installed for module {operation.module_id!r}, which "
            f"owns {name}; an operation above the read class does not run without "
            "one",
        )
    try:
        model_input = declaration.input_model.model_validate(
            {} if payload is None else payload
        )
    except ValidationError as exc:
        _audit_alone(ctx, audit, outcome=AUDIT_REFUSED)
        return _refused(INPUT_INVALID, _describe_validation_error(exc))
    # The subject can only be read off a *validated* model, so it joins the row here
    # rather than above. Every row written before this line therefore carries a null
    # ``subject_ref``, which is what the column is nullable for.
    if audit is not None:
        audit = replace(audit, subject_ref=_subject_ref(declaration, model_input))
    if declaration.safety_class in _APPROVAL_CLASSES:
        # The one class branch over *running* an operation, and it is placed here for
        # two reasons: the input has been validated, so the payload digest is taken
        # over the input as it will execute; and nothing has been minted yet, so the
        # hold owns the whole of what this call writes and writes it in one
        # transaction. ``audit`` cannot be ``None`` here — an upper-class declaration
        # is not ``READ``, so the refusal above has already answered
        # ``audit_sink_missing`` — and the assertion is the narrowing mypy needs, not
        # a case the branch handles.
        assert audit is not None
        return _hold_for_approval(ctx, declaration, model_input, audit)
    # Minted here and nowhere else: after authorization and validation have both
    # passed, and before the work transaction is opened, so the id is readable while
    # the work is still pending. ``None`` for every declaration that is not
    # ``long_running``.
    #
    # **The three refusals above this line — and only those three — leave no record
    # behind**, because they return before anything is written. Every exit below it
    # can, which is why each of them calls ``_fail_operation``; the claim is scoped to
    # the branches above rather than asserted over the function, so a later exit added
    # below cannot quietly falsify it.
    operation_id: UUID | None = None
    try:
        if declaration.long_running:
            operation_id = _mint_operation(ctx, declaration)
    except StorageRefusal as refusal:
        # Nothing was minted: this refusal came out of the mint's own unit of work,
        # which is ``open_unit_of_work`` refusing — the third structural exclusion,
        # reached one call site earlier. A row would need a connection to the same
        # workspace database that just refused this one, so there is none to write.
        return _refused(refusal.state, refusal.detail)
    try:
        uow = open_unit_of_work(ctx)
    except StorageRefusal as refusal:
        # § D5's third structural exclusion at its own site — and the one place the
        # enumeration is narrower than D5 states it. The unit of work is unobtainable,
        # and that is the refusal.
        #
        # The mint has already committed and the work cannot start, so the record is
        # orphaned unless it is ended here — and the caller is handed the id either
        # way, because a refusal that dropped it would leave a row nobody can name.
        # ``_fail_operation`` opens a unit of work of its own and may be refused for
        # the same reason this was; it logs, the record stays ``pending``, and it
        # answers whether it got one.
        #
        # **That answer is what decides the audit row, and it is the whole reason this
        # branch is not simply excluded.** When nothing was minted, ``_fail_operation``
        # attempts nothing, answers ``False``, and the exclusion holds exactly as § D5
        # states it: no second connection is ever opened, so there is nothing to write
        # the row with. When a record *was* minted and ``_fail_operation`` reached a
        # fresh connection, a second open to this workspace demonstrably does **not**
        # meet the identical failure — it just succeeded — and without the row the tree
        # would hold a committed ``failed`` operation record naming an actor, a
        # workspace and a time, with no audit record naming it, which is the one
        # outcome criterion 14 exists to forbid. So the row is written on the
        # connection's own evidence rather than on a prediction about it, and the code
        # and this comment cannot drift apart: the same call answers both.
        reachable = _fail_operation(
            ctx, operation_id, error_code=refusal.state, error_text=refusal.detail
        )
        if reachable:
            _audit_alone(ctx, audit, outcome=AUDIT_REFUSED, operation_id=operation_id)
        return _refused(refusal.state, refusal.detail, operation_id=operation_id)
    # Deliberately: authorize, the audit-sink resolution, the mint for a long-running
    # declaration, one unit of work, the handler, the output-type check, the audit
    # row, commit. The audit row is the **last** thing written before the commit and
    # the only thing between the handler and it; no outbox event is enqueued here.
    #
    # Every call that reaches this line is ``read``, ``draft`` or ``mutate``, and for
    # those three there is still no class branch on the running: they all simply run,
    # and the only class branch over them is the audit row, which every class but
    # ``read`` gets. The three upper classes returned above, at
    # ``_APPROVAL_CLASSES``, without entering a handler at all.
    with uow:
        try:
            # The handler gets the sealed view, carrying the minted id so a
            # long-running handler can stamp it on the job it enqueues and the
            # caller's own consumer registry so a publishing handler reaches the one
            # every other caller reaches; ending the transaction is this function's
            # job, on the real ``uow``.
            view = HandlerUnitOfWork(
                uow, operation_id=operation_id, consumers=consumers
            )
            output = operation.handler(ctx, view, model_input)
            if not isinstance(output, declaration.output):
                return _output_invalid(
                    ctx,
                    uow,
                    operation_id,
                    audit,
                    detail=(
                        f"{name} returned {type(output).__name__}, not "
                        f"{declaration.output.__name__}"
                    ),
                )
            if operation_id is not None and not has_job_for_operation(
                uow.connection, operation_id=operation_id
            ):
                # A ``long_running`` declaration is a promise that the work is queued
                # and that the record will be terminalised by the worker that runs it.
                # A handler that returned a well-formed output without enqueuing
                # anything has kept the shape of that promise and none of its content:
                # no job carries the id, so no worker will ever visit the record. Read
                # inside the work transaction, before the commit, so the job the
                # handler *did* enqueue is visible and only its real absence refuses.
                return _output_invalid(
                    ctx,
                    uow,
                    operation_id,
                    audit,
                    detail=(
                        f"{name} is long-running and queued no job for its operation "
                        f"record, which nothing would then terminalise"
                    ),
                )
            # **Inside the operation's own transaction, and last.** The row commits
            # with the effects, so an operation whose own write fails leaves neither
            # its effect nor its audit record — the property that makes the record
            # worth trusting (AC 23). Last, so that a handler that has already
            # refused, raised or returned the wrong type never reaches it: those
            # outcomes are audited from the ``except`` blocks below, after the
            # rollback, exactly once each. A ``long_running`` dispatch is audited
            # ``succeeded`` here too — what succeeded is the dispatch, which queued
            # the work; whether the *work* succeeds is the operation record's
            # question and the worker's to answer.
            if audit is not None:
                audit.record(
                    ctx, uow, outcome=AUDIT_SUCCEEDED, operation_id=operation_id
                )
            uow.commit()
        except OperationRefused as refusal:
            _rollback(uow)
            _fail_operation(
                ctx,
                operation_id,
                error_code=refusal.state,
                error_text=refusal.detail or refusal.state,
            )
            _audit_alone(ctx, audit, outcome=AUDIT_REFUSED, operation_id=operation_id)
            return _refused(refusal.state, refusal.detail, operation_id=operation_id)
        except StorageRefusal as refusal:
            _rollback(uow)
            _fail_operation(
                ctx, operation_id, error_code=refusal.state, error_text=refusal.detail
            )
            _audit_alone(ctx, audit, outcome=AUDIT_REFUSED, operation_id=operation_id)
            return _refused(refusal.state, refusal.detail, operation_id=operation_id)
        except Exception as exc:
            _rollback(uow)
            # The exception text is for the log, never the outcome: a driver error
            # renders the statement and its parameters, and ``error_text`` is
            # documented as safe to show. ``logger.exception`` implies
            # ``exc_info=True``, which would carry that same formatted traceback
            # into the log record; ``logger.error`` with only the exception's class
            # name keeps the log line as safe as the outcome already is.
            logger.error(
                "operation_failed",
                extra={
                    "operation": name,
                    "workspace_id": str(ctx.workspace_id),
                    "request_id": str(ctx.request_id),
                    "exception_type": type(exc).__name__,
                },
            )
            _fail_operation(
                ctx,
                operation_id,
                error_code=HANDLER_FAILED,
                error_text=type(exc).__name__,
            )
            # ``failed`` rather than ``refused``: the handler did not decline, it
            # broke. This branch also catches a commit that raised, so the audit row
            # written here can be the row for a dispatch whose own transaction — and
            # therefore whose in-transaction ``succeeded`` row — never landed. Still
            # exactly one row, because that one went down with the rollback.
            _audit_alone(ctx, audit, outcome=AUDIT_FAILED, operation_id=operation_id)
            return OperationOutcome(
                FAILED,
                error=OperationError(HANDLER_FAILED, type(exc).__name__),
                operation_id=operation_id,
            )
    # A handler that is not ``long_running`` but still made work due
    # (``core.work.retry`` and ``.replay`` put deliveries back ``pending``) asked for
    # the due mark through ``view.request_due_mark()``. It is written here, past the
    # ``with uow:`` block, for the reason the ``long_running`` mark below is: after the
    # commit, never before it. The ``operation_id is None`` half keeps a
    # ``long_running`` call to the one mark below.
    if operation_id is None and view.due_mark_requested:
        _mark_workspace_due(ctx)
    # ``pending`` for a ``long_running`` declaration, and this function writes no
    # other state for one: the work runs as a job and the worker that finishes that
    # job is the only thing that moves the record out of ``pending``. Returning
    # ``succeeded`` here would claim the work was done at the moment it was merely
    # scheduled.
    #
    # The ``long_running`` due-work mark is written here: past the ``with uow:`` block,
    # so the commit has happened and the job the handler queued is visible to any other
    # connection, and only on this branch, because ``operation_id is not None`` is
    # exactly "this was a ``long_running`` dispatch". Every path that reaches this line
    # committed; every path that did not raised or returned inside the block above.
    if operation_id is not None:
        _mark_workspace_due(ctx)
        return OperationOutcome(PENDING, result=output, operation_id=operation_id)
    return OperationOutcome(SUCCEEDED, result=output)
