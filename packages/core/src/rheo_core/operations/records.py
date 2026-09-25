"""Every read and write against ``core.operation``: the mint, the writes that end a
record, the unresolved marker and its clearing, and the two reads.

Shaped after ``work/jobs.py``, deliberately and in every respect that matters: each
function takes the caller's ``Connection`` and runs inside the caller's transaction,
**none of the writes below commits**, and ``now`` is always an explicit
keyword parameter so nothing here reads the process clock. That last property is what
lets a test assert a terminal instant by choosing it rather than by sleeping, and it
is what lets the worker terminalise an operation record **in the same transaction** as
the job row it belongs to — the property AC 16 rests on. A commit in here would break
that, which is why there is no equivalent of ``work.jobs.enqueue``'s documented
exception.

**The database owns "no ``succeeded`` without a terminal check", not this module.**
``work_tables.operation``'s ``operation_terminal_check_required`` check constraint
refuses a ``succeeded`` row whose ``terminal_check_kind`` or ``terminal_check_at`` is
null. :func:`finish_succeeded` therefore takes both as required keywords and supplies
them; it does not re-check them in Python. A duplicated Python check would be a second
place to keep in step, and would hide the constraint from the test that proves it.

**Every write in this module that ends a record is predicated on the record still
being open** (``pending`` or ``running``) — :func:`finish_succeeded`,
:func:`finish_failed`, :func:`finish_cancelled` and :func:`mark_unresolved`, plus
:func:`mark_running` and :func:`mark_approval_required`, neither of which is terminal
but both of which must refuse to drag a finished record back. Four writes are
predicated differently and each says so in its own docstring: :func:`resolve` on
``unresolved`` alone, and :func:`finish_held_succeeded`,
:func:`finish_held_cancelled` and :func:`finish_held_failed` on ``approval_required``
alone — a held record is exactly what ``_open`` excludes, because a call waiting for a
person is not work in flight, and those three are the three ways such a call can end:
its approval executed, a person refused it, or an execution guard stopped it. Each
returns whether it applied. A
terminal record is terminal: a second write must not reopen it, move it between
terminal states, or overwrite the outcome the first one recorded. The predicate is the
same shape as ``work/jobs.py``'s ``_held``, and for the same reason — a conditional
write that reports whether it matched leaves the decision about a zero rowcount to the
call site, which is the only place that knows what one means.

**Two functions ship with no production caller this run**, named here rather than left
to be discovered: :func:`mark_running` (release one's dispatcher returns before the
work starts and the worker's only writes are terminal, so nothing observes the
transition) and :func:`mark_unresolved` (release one has no external effect whose
outcome can go unknown). They exist because :func:`resolve` needs something to clear
and because AC 18 drives that path directly.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Connection, Row, func, insert, select, update
from sqlalchemy.sql.elements import ColumnElement

from rheo_core.refs import uuid7
from rheo_core.storage import work_tables as t

PENDING: Final = "pending"
RUNNING: Final = "running"
APPROVAL_REQUIRED: Final = "approval_required"
SUCCEEDED: Final = "succeeded"
FAILED: Final = "failed"
CANCELLED: Final = "cancelled"
UNRESOLVED: Final = "unresolved"
"""The members of the ``operation_state`` check constraint
(``work_tables.OPERATION_STATES``), named here so no predicate in this module spells
one of them by hand, and checked against that tuple at import time just below."""

# The names above are a second spelling of the DDL's own tuple, and until this check
# nothing compared them: a drift would have surfaced as a Postgres check-constraint
# violation at whatever call site happened to write the renamed state first — a driver
# error three layers from its cause, and only on the path that happened to use it.
# ``operation_ops.py`` already runs exactly this check one layer up, over its two
# ``Literal`` aliases against this module's tuples; that pair was asymmetric until now,
# with this module checking nothing against the tuple it claims to name.
if set(t.OPERATION_STATES) != {  # pragma: no cover - import-time invariant
    PENDING,
    RUNNING,
    APPROVAL_REQUIRED,
    SUCCEEDED,
    FAILED,
    CANCELLED,
    UNRESOLVED,
}:
    raise RuntimeError(
        "this module's operation-state names and work_tables.OPERATION_STATES "
        "disagree; they are the same set spelled twice and must be changed together"
    )

RECORD_EXISTS: Final = "record_exists"
PROVIDER_STATUS: Final = "provider_status"
SINK_RECORDED: Final = "sink_recorded"
HANDLER_RETURNED: Final = "handler_returned"

TERMINAL_CHECK_KINDS: Final = (
    RECORD_EXISTS,
    PROVIDER_STATUS,
    SINK_RECORDED,
    HANDLER_RETURNED,
)
"""The four ratified kinds (``docs/architecture/intake-and-events.md:465``).

``handler_returned`` is what the **worker's success path** records, because that is
exactly what it checked: the handler returned without raising. :func:`resolve` records
whichever kind the person resolving an ``unresolved`` record supplies, which is why
``provider_status`` and the rest are reachable from release one's own code and not
only from a later external-effect checker. They are named here so a caller picks from
a list rather than inventing a string, and ``operation_ops.py`` checks its own
``Literal`` alias against this tuple at import time."""

RESOLVABLE_OUTCOMES: Final = (SUCCEEDED, FAILED, CANCELLED)
"""The named outcomes :func:`resolve` may move an ``unresolved`` record to. Not
``pending``/``running`` (a resolution ends a record, it does not reopen one), not
``approval_required`` (that is a state the work reaches, not one a person resolves
to), and not ``unresolved`` itself."""

AUDIENCE_NONE: Final = "none"
"""What :func:`mint` is handed for a context carrying no audience.

``WorkspaceContext.audience`` is ``Audience | None`` — ``overview.md`` says a
scheduled job has none, and ``context_for_operator`` builds one that way — but
``operation.audience_kind`` is ``NOT NULL`` in the shipped DDL
(``work_tables.py:186``), which this chunk does not edit. So the absence is recorded
as a value rather than as a null. A recorded deviation, not an ``AudienceKind``
member: the enum is the closed ratified set of four and gains nothing from a fifth
that means "not applicable".
"""


@dataclass(frozen=True, slots=True)
class OperationRow:
    """One ``core.operation`` row as its supported reads publish it.

    Twelve fields: the identity, the state machine, and the outcome. The context
    provenance :func:`mint` writes (``actor_kind``, ``actor_id``, ``entry``,
    ``audience_kind``, ``audience_id``) is deliberately **not** here — a caller
    reading back the operation it just dispatched already knows who it was, and the
    published shape is narrower than the row on purpose so that a later run widening
    it is an additive change to a model rather than a change of meaning.
    """

    id: UUID
    name: str
    safety_class: str
    state: str
    result_ref: str | None
    error_code: str | None
    error_text: str | None
    terminal_check_kind: str | None
    terminal_check_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    terminal_at: datetime | None


@dataclass(frozen=True, slots=True)
class OperationContextProvenance:
    """The five context fields :func:`mint` copied onto a record, and nothing else.

    A separate narrow read rather than five more fields on :class:`OperationRow`,
    because that model's narrower-than-the-row shape is a documented boundary and
    widening it would change what *every* caller of ``core.operation.get`` is handed.
    What needs these is one function --
    ``boundary/factories.py:context_for_approved_execution`` -- rebuilding the caller
    an approval was held for, and it needs them as provenance, not as a published
    record.
    """

    actor_kind: str
    actor_id: UUID | None
    audience_kind: str
    audience_id: UUID | None
    entry: str


_PROVENANCE_COLUMNS: Final = (
    t.operation.c.actor_kind,
    t.operation.c.actor_id,
    t.operation.c.audience_kind,
    t.operation.c.audience_id,
    t.operation.c.entry,
)

_COLUMNS: Final = (
    t.operation.c.id,
    t.operation.c.name,
    t.operation.c.safety_class,
    t.operation.c.state,
    t.operation.c.result_ref,
    t.operation.c.error_code,
    t.operation.c.error_text,
    t.operation.c.terminal_check_kind,
    t.operation.c.terminal_check_at,
    t.operation.c.created_at,
    t.operation.c.started_at,
    t.operation.c.terminal_at,
)
"""The columns both reads select. One tuple rather than two select lists, so
:func:`get` and :func:`list_recent` cannot drift apart and :func:`_row` has exactly
one row shape to map."""


def _open(operation_id: UUID) -> ColumnElement[bool]:
    """The predicate every terminal write shares: this record, still not terminal."""
    return (t.operation.c.id == operation_id) & t.operation.c.state.in_(
        (PENDING, RUNNING)
    )


def _held(operation_id: UUID) -> ColumnElement[bool]:
    """This record, still waiting for a decision on its approval.

    Deliberately **not** part of :func:`_open`. A record in ``approval_required`` is
    not work in flight: no handler has run, no job carries it, and the only things
    that may move it are the approval's own approve and refuse. Widening ``_open`` to
    include it would let every terminal write in this module — and the dispatcher's
    own failure path — reach a record that is waiting for a person.
    """
    return (t.operation.c.id == operation_id) & (
        t.operation.c.state == APPROVAL_REQUIRED
    )


def _apply(conn: Connection, where: ColumnElement[bool], **values: object) -> bool:
    """Run one predicated ``UPDATE`` on ``core.operation`` and report whether it
    matched."""
    return conn.execute(update(t.operation).where(where).values(**values)).rowcount == 1


def _row(row: Row[Any]) -> OperationRow:
    """One selected row as an :class:`OperationRow`, field for field.

    Written out rather than splatted (``OperationRow(*row)``): the splat compiles
    under mypy only with an ignore, and it silently pairs the wrong column with the
    wrong field the moment :data:`_COLUMNS` and the dataclass drift apart, which is
    the one defect this mapping can actually have.
    """
    return OperationRow(
        id=row.id,
        name=str(row.name),
        safety_class=str(row.safety_class),
        state=str(row.state),
        result_ref=None if row.result_ref is None else str(row.result_ref),
        error_code=None if row.error_code is None else str(row.error_code),
        error_text=None if row.error_text is None else str(row.error_text),
        terminal_check_kind=(
            None if row.terminal_check_kind is None else str(row.terminal_check_kind)
        ),
        terminal_check_at=row.terminal_check_at,
        created_at=row.created_at,
        started_at=row.started_at,
        terminal_at=row.terminal_at,
    )


def mint(
    conn: Connection,
    *,
    name: str,
    safety_class: str,
    actor_kind: str,
    actor_id: UUID | None,
    entry: str,
    audience_kind: str,
    audience_id: UUID | None,
    now: datetime,
) -> UUID:
    """Write one ``pending`` record and return its id. Does not commit.

    Everything the state machine has not reached yet is null: no ``started_at``, no
    ``terminal_at``, no terminal check, no outcome. That is what makes ``pending`` the
    representable first state the DDL's own comment describes, and it is why the
    terminal-check constraint is scoped to ``succeeded`` rather than written over the
    whole table.
    """
    operation_id = uuid7()
    conn.execute(
        insert(t.operation).values(
            id=operation_id,
            name=name,
            safety_class=safety_class,
            actor_kind=actor_kind,
            actor_id=actor_id,
            entry=entry,
            audience_kind=audience_kind,
            audience_id=audience_id,
            state=PENDING,
            approval_id=None,
            progress_text=None,
            progress_fraction=None,
            result_ref=None,
            error_code=None,
            error_text=None,
            terminal_check_kind=None,
            terminal_check_at=None,
            provider_request_id=None,
            created_at=now,
            started_at=None,
            terminal_at=None,
        )
    )
    return operation_id


def mark_running(conn: Connection, *, operation_id: UUID, now: datetime) -> bool:
    """Move an open record to ``running`` and stamp ``started_at``.

    Predicated the same way the terminal writes are, so a record that has already
    finished is never dragged back to ``running`` by a late caller.
    """
    return _apply(conn, _open(operation_id), state=RUNNING, started_at=now)


def set_progress(
    conn: Connection,
    *,
    operation_id: UUID,
    progress_text: str,
    progress_fraction: float,
) -> bool:
    """Write progress onto an open record.

    Predicated the same way :func:`mark_running` is.
    """
    return _apply(
        conn,
        _open(operation_id),
        progress_text=progress_text,
        progress_fraction=progress_fraction,
    )


def mark_approval_required(
    conn: Connection, *, operation_id: UUID, approval_id: UUID
) -> bool:
    """Hold an open record for an approval, stamping the approval it waits on.

    Predicated the same way :func:`mark_running` is, and for the same reason: a
    record that has already finished must never be dragged back into a waiting state
    by a late caller. ``approval_id`` is the column ``core.operation`` has carried
    since run 0c2 with nothing to write it — this is its first writer.

    No instant, unlike :func:`mark_running`: ``approval_required`` is the record
    *not* starting, so ``started_at`` stays null and the row has no other timestamp
    this transition could honestly fill. The moment the call was held is the
    approval's own ``window_start``, and the audit row's ``occurred_at``.
    """
    return _apply(
        conn,
        _open(operation_id),
        state=APPROVAL_REQUIRED,
        approval_id=approval_id,
    )


def finish_held_succeeded(
    conn: Connection,
    *,
    operation_id: UUID,
    now: datetime,
    terminal_check_kind: str,
    terminal_check_at: datetime,
) -> bool:
    """End a held record ``succeeded`` once its approval has executed. Reports
    whether it applied.

    **The one place this module writes ``succeeded`` outside the worker's path, and
    it is not an exception to the one-terminaliser rule.** That rule is about work
    that runs as a job: the dispatcher must not report success for work it merely
    scheduled, because only the worker knows it was done. An approved destructive
    operation is the opposite case — the handler runs inside the approving
    transaction, so the thing that knows the work was done is the thing writing this
    row, and no job or worker will ever see the record.

    Both check fields are required keywords with no default, exactly as
    :func:`finish_succeeded`'s are, and for the same reason: the database's
    ``operation_terminal_check_required`` constraint refuses a ``succeeded`` row
    without them, and a signature that cannot be called without them is better than a
    driver error at the commit.
    """
    return _apply(
        conn,
        _held(operation_id),
        state=SUCCEEDED,
        terminal_check_kind=terminal_check_kind,
        terminal_check_at=terminal_check_at,
        terminal_at=now,
    )


def finish_held_cancelled(
    conn: Connection, *, operation_id: UUID, now: datetime
) -> bool:
    """End a held record ``cancelled`` because its approval was refused. Reports
    whether it applied.

    ``cancelled`` rather than ``failed``: nothing went wrong and nothing was
    attempted — a person decided the call should not happen, which is what that state
    means everywhere else in this module.
    """
    return _apply(conn, _held(operation_id), state=CANCELLED, terminal_at=now)


def finish_held_failed(
    conn: Connection,
    *,
    operation_id: UUID,
    now: datetime,
    error_code: str,
    error_text: str,
) -> bool:
    """End a held record ``failed`` because an execution guard refused it. Reports
    whether it applied.

    ``failed`` rather than ``cancelled``: nobody decided the call should not happen —
    it was released and then stopped, which is the difference
    :func:`finish_held_cancelled`'s own docstring draws. ``confirmation-and-safety.md``
    § Execution guards fixes both halves of this write: "A refused guard leaves the
    external action ``refused``, the operation ``failed`` **with the guard's code**",
    so the code and the text are where the guard is named.

    Predicated on :func:`_held` rather than on :func:`_open`, which is why
    :func:`finish_failed` could not serve: that predicate deliberately excludes a
    record waiting for an approval, so that the dispatcher's own failure path cannot
    reach one.
    """
    return _apply(
        conn,
        _held(operation_id),
        state=FAILED,
        error_code=error_code,
        error_text=error_text,
        terminal_at=now,
    )


def finish_succeeded(
    conn: Connection,
    *,
    operation_id: UUID,
    now: datetime,
    terminal_check_kind: str,
    terminal_check_at: datetime,
    result_ref: str | None = None,
) -> bool:
    """End the record ``succeeded``, recording the terminal check. Reports whether it
    applied.

    Both check fields are **required keywords with no default**. A default would let
    a call site omit them and meet the database's constraint by accident or not at
    all, and the constraint's refusal surfaces as a driver error at commit rather than
    as a signature that could not be written wrong in the first place.
    """
    return _apply(
        conn,
        _open(operation_id),
        state=SUCCEEDED,
        result_ref=result_ref,
        terminal_check_kind=terminal_check_kind,
        terminal_check_at=terminal_check_at,
        terminal_at=now,
    )


def finish_failed(
    conn: Connection,
    *,
    operation_id: UUID,
    now: datetime,
    error_code: str,
    error_text: str,
) -> bool:
    """End the record ``failed`` with a code and a showable text. Reports whether it
    applied."""
    return _apply(
        conn,
        _open(operation_id),
        state=FAILED,
        error_code=error_code,
        error_text=error_text,
        terminal_at=now,
    )


def finish_cancelled(conn: Connection, *, operation_id: UUID, now: datetime) -> bool:
    """End the record ``cancelled``. Reports whether it applied."""
    return _apply(conn, _open(operation_id), state=CANCELLED, terminal_at=now)


def mark_unresolved(conn: Connection, *, operation_id: UUID, now: datetime) -> bool:
    """End the record ``unresolved``. Reports whether it applied.

    ``unresolved`` is terminal for the record and open for the person: an external
    effect whose outcome is unknown after a timeout, where the provider offers neither
    idempotency nor a status lookup. Only :func:`resolve` clears it.
    """
    return _apply(conn, _open(operation_id), state=UNRESOLVED, terminal_at=now)


def resolve(
    conn: Connection,
    *,
    operation_id: UUID,
    outcome: str,
    note: str,
    now: datetime,
    terminal_check_kind: str | None = None,
) -> bool:
    """Move an ``unresolved`` record to a named ``outcome`` with a ``note``. Reports
    whether it applied.

    **Predicated on ``state = 'unresolved'`` and nothing else**, which is AC 18's
    "refuses a record in any other state" expressed as the write itself rather than as
    a read-then-write a concurrent caller could interleave with. The caller reads the
    record first to tell "no such record" from "wrong state" in its refusal text; the
    predicate is what makes the refusal true regardless of what happened between the
    two statements.

    **The note lands in ``error_text``, and that is a recorded deviation.**
    ``core.operation`` has no ``note`` column — the ratified column list
    (``intake-and-events.md`` § Operations) gives it none — and ``error_text`` is the
    row's one free-text field. So a record resolved to ``succeeded`` carries its note
    in a column named for errors. The alternative was a column this chunk may not add;
    the audit row written for this operation is the note's other home.

    ``terminal_check_kind`` is required when ``outcome`` is ``succeeded`` and must be
    one of :data:`TERMINAL_CHECK_KINDS` — the person resolving the record checked
    something, and which thing they checked is the whole content of AC 17 on this
    path. The caller's input model is what refuses a missing or unknown kind; this
    function supplies ``terminal_check_at`` from ``now``.
    """
    values: dict[str, object] = {
        "state": outcome,
        "error_text": note,
        "terminal_at": now,
    }
    if outcome == SUCCEEDED:
        values["terminal_check_kind"] = terminal_check_kind
        values["terminal_check_at"] = now
    return _apply(
        conn,
        (t.operation.c.id == operation_id) & (t.operation.c.state == UNRESOLVED),
        **values,
    )


def get_context_provenance(
    conn: Connection, *, operation_id: UUID
) -> OperationContextProvenance | None:
    """The context fields this record was minted with, or ``None`` for no such row.

    ``audience_kind`` comes back as the column holds it, :data:`AUDIENCE_NONE`
    included: this accessor reports what was recorded and leaves the reader to decide
    what "none" means, exactly as :func:`mint` left the decision to its caller.
    """
    row = conn.execute(
        select(*_PROVENANCE_COLUMNS).where(t.operation.c.id == operation_id)
    ).one_or_none()
    if row is None:
        return None
    return OperationContextProvenance(
        actor_kind=str(row.actor_kind),
        actor_id=row.actor_id,
        audience_kind=str(row.audience_kind),
        audience_id=row.audience_id,
        entry=str(row.entry),
    )


def get(conn: Connection, *, operation_id: UUID) -> OperationRow | None:
    """One record by id, or ``None`` when there is no such row."""
    row = conn.execute(
        select(*_COLUMNS).where(t.operation.c.id == operation_id)
    ).one_or_none()
    return None if row is None else _row(row)


def list_recent(conn: Connection, *, limit: int) -> tuple[OperationRow, ...]:
    """The most recently created records, newest ``created_at`` first, capped at
    ``limit``.

    ``id`` breaks a tie on ``created_at``, and is not decoration: the dispatcher
    stamps that column from one instant, so two records minted inside the same
    transaction share it exactly, and an unordered tie makes the page a caller reads
    depend on the plan Postgres happened to choose. The ids are uuid7, so descending
    id is the same ordering ``created_at`` intends.

    ``limit`` is bounded by the caller's input model, never here: an out-of-range
    value must be an ``input_invalid`` refusal from pydantic and must never reach this
    ``SELECT``, exactly as ``work/operations.py``'s ``FailureListInput`` already
    arranges for the failure list.
    """
    rows = conn.execute(
        select(*_COLUMNS)
        .order_by(t.operation.c.created_at.desc(), t.operation.c.id.desc())
        .limit(limit)
    )
    return tuple(_row(row) for row in rows)


def count_unresolved(conn: Connection) -> int:
    """How many records are ``unresolved``: the operation half of
    ``core.work.failure_summary``. One aggregate, no rows."""
    return int(
        conn.execute(
            select(func.count())
            .select_from(t.operation)
            .where(t.operation.c.state == UNRESOLVED)
        ).scalar_one()
    )
